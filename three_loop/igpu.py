"""Detect whether the shared-memory iGPU is available to the local runtime.

The Ryzen 5000U series pairs the CPU with a Radeon Vega iGPU on the *same*
DDR4 controller. That unified memory removes the usual reason iGPU offload
does not pay - there is no host-to-device copy, the weights are already
where the GPU can read them.

What it buys follows the roofline directly. Measured on this node with
Qwen2.5-Coder-3B Q4_K_M, same runtime and prompt, iGPU toggled:

    prefill (compute-bound, ~100-1600 FLOP/byte)   86.8 -> 114.5 tok/s  (+32%)
    decode  (memory-bound,  ~3.2 FLOP/byte)        13.8 ->  15.0 tok/s  (+9%)

The compute-bound phase gains 3.6x more than the memory-bound one, because
the iGPU adds FLOPs but not bandwidth - it shares the very same DRAM.

Whether that is a net win depends on the shape of the workload. Against the
CPU path 3loop uses by default (llama-cpp-python: 68.7 tok/s prefill,
18.8 tok/s decode), the iGPU is faster at prefill but *slower* at decode, so
it only pays when

    prompt_tokens / generated_tokens > 2.3

which the measured 3loop profile (~1500 prompt, ~334 generated, ratio 4.5)
comfortably satisfies. Cutting generated tokens makes it pay more, not less.

Ollama ships the Vulkan backend but drops integrated GPUs unless
``OLLAMA_IGPU_ENABLE=1`` is set, logging "dropping integrated GPU" and
falling back to CPU silently as far as the user can tell.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

#: Environment the Ollama server must be started with for the iGPU to be used.
REQUIRED_ENV = {"OLLAMA_VULKAN": "1", "OLLAMA_IGPU_ENABLE": "1"}

#: 3loop runs its own Ollama server on a private port rather than asking the
#: user to reconfigure the system-wide service. The iGPU is only usable with
#: environment variables set *at server start*, and the desktop Ollama runs
#: as a background service nobody wants 3loop editing.
IGPU_PORT = 11719

#: Windows-only: start the child without allocating a console window.
_CREATE_NO_WINDOW = 0x08000000

_server: subprocess.Popen | None = None


def _ollama_host() -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    if not host.startswith("http"):
        host = f"http://{host}"
    return host.rstrip("/")


def probe(timeout: float = 3.0) -> dict[str, Any]:
    """Report whether a loaded model is actually running on the iGPU.

    Uses ``/api/ps``, which lists resident models with their size split
    across CPU and GPU. A model reported entirely on CPU while the machine
    has an iGPU means the server was started without ``OLLAMA_IGPU_ENABLE``.
    """

    result: dict[str, Any] = {
        "reachable": False,
        "using_gpu": False,
        "loaded_models": [],
        "how_to_enable": _instructions(),
    }
    try:
        with urllib.request.urlopen(f"{_ollama_host()}/api/ps", timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return result

    result["reachable"] = True
    for model in payload.get("models", []):
        total = model.get("size") or 0
        vram = model.get("size_vram") or 0
        on_gpu = bool(total) and vram > 0
        result["loaded_models"].append(
            {
                "name": model.get("name", "?"),
                "gpu_fraction": (vram / total) if total else 0.0,
            }
        )
        result["using_gpu"] = result["using_gpu"] or on_gpu
    return result


#: Prompt-to-generation ratio above which the iGPU is faster than the CPU.
#: Derived from the measured rates: CPU 68.7 tok/s prefill / 18.8 tok/s
#: decode, iGPU 114.5 / 15.0. Solving for equal wall time gives 2.31.
BREAK_EVEN_RATIO = 2.3

#: Models stop well before ``max_tokens``. Measured on the compact debate:
#: ~334 generated for a 900 cap, i.e. ~0.37. Using the raw cap instead would
#: systematically over-estimate generation and never route anything to the
#: iGPU.
_GENERATION_FILL = 0.37

#: Rough chars-per-token for this tokenizer family; only used to turn a
#: prompt length into a token estimate without paying for a real tokenize().
_CHARS_PER_TOKEN = 3.6


def should_use_igpu(prompt_chars: int, max_tokens: int) -> bool:
    """Whether this request's shape favours the iGPU over the CPU.

    The iGPU adds compute but no bandwidth, so it wins on prefill-heavy
    work and loses on generation-heavy work. Measured confirmation of the
    unfavourable side: six mathematical proofs (ratio 0.10) ran at
    14.44 tok/s on CPU against 13.89 on the iGPU - a 3.8% *loss*, with
    identical quality since it is the same model and weights.

    The estimate is deliberately coarse; it only has to get the side of a
    2.3 threshold right, not the exact ratio.
    """

    prompt_tokens = max(1.0, prompt_chars / _CHARS_PER_TOKEN)
    generated_tokens = max(1.0, max_tokens * _GENERATION_FILL)
    return (prompt_tokens / generated_tokens) > BREAK_EVEN_RATIO


def igpu_host() -> str:
    """Base URL of the 3loop-managed, iGPU-enabled Ollama server."""

    return f"http://127.0.0.1:{IGPU_PORT}"


def _is_up(host: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def ensure_server(startup_timeout: float = 45.0) -> str | None:
    """Start (once) an Ollama server with the iGPU enabled; return its host.

    Returns ``None`` when Ollama is not installed or the server refuses to
    come up, so callers fall back to the CPU path rather than failing.

    A dedicated server is used instead of reconfiguring the system-wide one
    because ``OLLAMA_IGPU_ENABLE`` is only read at start-up: there is no way
    to enable the iGPU on an already-running instance, and silently
    restarting the user's Ollama service would disrupt anything else using
    it.
    """

    global _server

    host = igpu_host()
    if _is_up(host):
        return host

    executable = shutil.which("ollama")
    if executable is None:
        return None

    env = dict(os.environ)
    env.update(REQUIRED_ENV)
    env["OLLAMA_HOST"] = f"127.0.0.1:{IGPU_PORT}"
    try:
        _server = subprocess.Popen(
            [executable, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError:
        return None

    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        if _is_up(host):
            return host
        if _server.poll() is not None:  # exited early
            return None
        time.sleep(0.5)
    return None


def stop_server() -> None:
    """Terminate the managed server, if 3loop started one."""

    global _server
    if _server is not None and _server.poll() is None:
        _server.terminate()
        try:
            _server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server.kill()
    _server = None


def _instructions() -> str:
    """Exact command to restart Ollama with the iGPU enabled."""

    return (
        "Arreter le serveur Ollama, puis le relancer avec "
        "OLLAMA_VULKAN=1 et OLLAMA_IGPU_ENABLE=1. "
        "Sans OLLAMA_IGPU_ENABLE, Ollama ecarte les GPU integres "
        "(journal: 'dropping integrated GPU') et bascule sur le CPU "
        "sans que rien ne l'indique dans l'interface."
    )
