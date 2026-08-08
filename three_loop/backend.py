"""Interchangeable asynchronous LLM backends with one shared model object."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


class SharedLLMBackend(ABC):
    """Base class that serializes access to one in-memory model instance.

    The three role agents receive the same backend object.  Local
    ``llama-cpp-python`` inference uses a lock because one ``Llama`` context is
    not generally safe for overlapping generations. HTTP providers can opt
    out: their independent requests then run concurrently while the API
    server remains responsible for model scheduling.
    """

    def __init__(self, *, serialize_requests: bool = True) -> None:
        self._inference_locks: dict[int, asyncio.Lock] = {}
        self.serialize_requests = serialize_requests

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate one completion while preventing concurrent model calls."""

        if not self.serialize_requests:
            return await self._complete(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
            )
        async with self._lock_for_current_loop():
            return await self._complete(
                prompt,
                temperature=temperature,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
            )

    def _lock_for_current_loop(self) -> asyncio.Lock:
        """Use a lock per event loop so a backend can be reused in tests/UI."""

        loop_id = id(asyncio.get_running_loop())
        lock = self._inference_locks.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            self._inference_locks[loop_id] = lock
        return lock

    @abstractmethod
    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        """Implement a provider-specific completion call."""


class FunctionBackend(SharedLLMBackend):
    """Adapter around a sync or async callable, useful for tests and services."""

    def __init__(
        self,
        handler: Callable[..., str | Awaitable[str]],
    ) -> None:
        super().__init__()
        self.handler = handler

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "temperature": temperature,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
        }
        try:
            parameters = inspect.signature(self.handler).parameters
        except (TypeError, ValueError):
            parameters = {}
        if parameters and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs = {
                name: value for name, value in kwargs.items() if name in parameters
            }
        result = self.handler(prompt, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return str(result)


def _physical_cores() -> int:
    """Physical (not logical) core count.

    CPU inference is memory-bandwidth-bound, so SMT/hyperthread siblings add
    scheduling overhead without adding bandwidth. Measured on a Ryzen 7 5825U
    (8 physical / 16 logical) with Qwen2.5-Coder-3B Q4_K_M: throughput is flat
    from 4 to 8 threads and then *drops* - 61.3 tok/s at 8 threads versus
    55.4 tok/s at 16, i.e. using every logical core costs ~10%.
    """

    try:
        import psutil

        physical = psutil.cpu_count(logical=False)
        if physical:
            return int(physical)
    except Exception:
        pass
    logical = os.cpu_count() or 8
    # Most x86 parts ship 2-way SMT; halving is the safe approximation.
    return max(1, logical // 2)


class LlamaCppBackend(SharedLLMBackend):
    """Local backend using one shared model and llama.cpp's own KV prefix reuse.

    llama.cpp already keeps the previous sequence's KV cache in its context
    and reuses the longest matching token prefix on the next call, so a
    request that only *appends* to the previous prompt skips prefill almost
    entirely. Measured with a ~724-token prefix: 9.54 s cold, 1.02 s when the
    new prompt merely appends to it - a 9.4x saving.

    This only pays off if prompts are built append-only. Inserting new text
    (history, sources) ahead of otherwise-identical content invalidates the
    prefix from that point on and costs the full prefill again - measured at
    9.45 s, i.e. no reuse at all. See ``latent.py`` for the prompt layout
    that keeps the stable part first.

    ``LlamaRAMCache`` is deliberately *not* installed: it duplicates the
    built-in reuse while adding a state copy per call, and measured slightly
    slower (8.8x versus 9.5x on the same prefix).
    """

    def __init__(
        self,
        model_path: str,
        *,
        model_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "llama-cpp-python is required for LlamaCppBackend; "
                "install 3loop[llama]"
            ) from exc

        self.model_path = model_path
        # Decode and prefill are bound by different resources, so they get
        # different thread counts. Decode re-reads the whole model per token
        # and saturates memory bandwidth by ~4 threads - extra SMT siblings
        # only add contention (61.3 tok/s at 8 threads, 55.4 at 16). Prefill
        # is matrix-matrix work and does keep scaling: measured 48.1 tok/s at
        # 4 threads, 57.7 at 8, 68.4 at 12 on the same prompt.
        physical = _physical_cores()
        defaults: dict[str, Any] = {
            "n_ctx": 8192,
            "n_threads": physical,
            "n_threads_batch": max(physical, int(physical * 1.5)),
            "flash_attn": True,
            # Keep the quantized weights memory-mapped from the GGUF file. This
            # keeps the model on disk-backed pages instead of copying a second
            # full weight buffer into RAM; the OS still needs enough RAM/VRAM
            # for the pages actually used and for the KV cache.
            "use_mmap": True,
            "use_mlock": False,
            "verbose": False,
        }
        defaults.update(dict(model_kwargs or {}))
        try:
            self._llama = Llama(model_path=model_path, **defaults)
        except (TypeError, ValueError):
            # Older llama-cpp-python builds may reject one of the optional
            # performance flags. Keep mmap when possible, but never make it a
            # hard requirement for an otherwise compatible GGUF build.
            defaults.pop("flash_attn", None)
            try:
                self._llama = Llama(model_path=model_path, **defaults)
            except (TypeError, ValueError):
                defaults.pop("use_mmap", None)
                defaults.pop("use_mlock", None)
                self._llama = Llama(model_path=model_path, **defaults)
        self._n_ctx = int(defaults["n_ctx"])

    @staticmethod
    def discover_local_gguf(
        roots: Sequence[str | os.PathLike[str]] | None = None,
    ) -> list[tuple[str, str]]:
        """Find standalone GGUF files in the app and user model folders.

        The installer puts downloads beside the frozen app, while source
        checkouts and manually copied models use ``models/`` or
        ``~/.3loop/models``. An explicit ``LOUPE_MODELS_DIR`` takes priority
        so advanced users can keep multi-gigabyte weights on another disk.
        """

        if roots is None:
            configured = os.environ.get("LOUPE_MODELS_DIR", "").strip()
            roots = [Path(configured)] if configured else []
            if getattr(sys, "frozen", False):
                executable_dir = Path(sys.executable).resolve().parent
                roots.extend((executable_dir / "models", executable_dir.parent / "models"))
            else:
                roots.append(Path(__file__).resolve().parent.parent / "models")
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                roots.append(Path(local_app_data) / "LOUPe" / "models")
            roots.append(Path.home() / ".3loop" / "models")

        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw_root in roots:
            root = Path(raw_root).expanduser()
            if not root.is_dir():
                continue
            for path in root.rglob("*.gguf"):
                if not path.is_file():
                    continue
                try:
                    resolved = str(path.resolve())
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append((f"{path.stem} · GGUF local", resolved))
        return sorted(found, key=lambda item: item[0].casefold())

    @staticmethod
    def discover_ollama_models() -> list[tuple[str, str]]:
        """Return ``(label, gguf_path)`` for models Ollama already pulled.

        Loading those GGUF blobs directly measures about twice as fast as
        driving the same weights through the Ollama HTTP server, and skips
        the per-request round trip entirely.
        """

        root = Path.home() / ".ollama" / "models"
        manifests = root / "manifests"
        blobs = root / "blobs"
        if not manifests.is_dir() or not blobs.is_dir():
            return []

        found: list[tuple[str, str]] = []
        for manifest_path in manifests.rglob("*"):
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for layer in manifest.get("layers", []):
                if layer.get("mediaType") != "application/vnd.ollama.image.model":
                    continue
                digest = str(layer.get("digest", "")).replace(":", "-")
                blob = blobs / f"sha256-{digest.split('sha256-')[-1]}"
                if not blob.is_file():
                    continue
                label = f"{manifest_path.parent.name}:{manifest_path.name}"
                found.append((label, str(blob)))
        return sorted(set(found))

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        messages = _messages(prompt, system_prompt)

        def invoke() -> str:
            # llama.cpp does not reliably raise a catchable Python error when
            # prompt_tokens + max_tokens exceeds n_ctx - some versions instead
            # overrun the KV cache buffer and crash the whole process with a
            # native access violation. Counting tokens ourselves and clamping
            # max_tokens (or bailing out with a normal exception) keeps that
            # failure mode inside Python instead of taking down the app.
            prompt_tokens = self._count_tokens(messages)
            safety_margin = 32
            available = self._n_ctx - prompt_tokens - safety_margin
            if available <= 0:
                raise ValueError(
                    f"prompt trop long ({prompt_tokens} tokens) pour la fenetre "
                    f"de contexte du modele ({self._n_ctx} tokens); raccourcis "
                    "la question ou l'historique."
                )
            capped_tokens = min(max_tokens, available) if max_tokens else available
            response = self._llama.create_chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=capped_tokens,
            )
            return _extract_content(response)

        return await asyncio.to_thread(invoke)

    def _count_tokens(self, messages: list[dict[str, str]]) -> int:
        # A plain sum of per-message token counts is a slight overestimate
        # (ignores shared chat-template overhead) but that only makes the
        # safety margin more conservative, never less.
        joined = "\n".join(m.get("content", "") for m in messages)
        try:
            return len(self._llama.tokenize(joined.encode("utf-8"), add_bos=True))
        except Exception:
            # Rough fallback (~4 chars/token) if tokenize() itself misbehaves.
            return len(joined) // 4


class AirLLMBackend(SharedLLMBackend):
    """Optional layer-streamed backend for very large Hugging Face models.

    AirLLM keeps only the active layer on the accelerator and supports CPU
    inference. It is a memory-saving fallback, not automatically a latency
    optimization: a quantized GGUF in llama.cpp is usually faster when the
    whole model fits in the available RAM. ``use_cache=True`` here reuses the
    transformer KV cache *within one generation*; it does not transfer hidden
    states between independently sampled agents.
    """

    def __init__(
        self,
        model: str,
        *,
        device: str = "cpu",
        max_input_tokens: int = 2048,
        compression: str | None = None,
        layer_shards_saving_path: str | None = None,
        model_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        try:
            from airllm import AutoModel
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "airllm est requis pour AirLLMBackend; installez 3loop[airllm]"
            ) from exc

        kwargs = dict(model_kwargs or {})
        if compression is not None:
            kwargs["compression"] = compression
        if layer_shards_saving_path is not None:
            kwargs["layer_shards_saving_path"] = layer_shards_saving_path
        self.model_name = model
        self.device = device
        self.max_input_tokens = max_input_tokens
        self._model = AutoModel.from_pretrained(model, **kwargs)
        self._tokenizer = self._model.tokenizer

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        del temperature
        full_prompt = (
            f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        )
        output_limit = max(1, max_tokens or 512)

        def invoke() -> str:
            import torch

            inputs = self._tokenizer(
                [full_prompt],
                return_tensors="pt",
                return_attention_mask=False,
                truncation=True,
                max_length=self.max_input_tokens,
                padding=False,
            )
            if self.device != "cpu":
                inputs = {
                    key: value.to(self.device) for key, value in inputs.items()
                }
            input_length = int(inputs["input_ids"].shape[-1])
            with torch.inference_mode():
                generated = self._model.generate(
                    inputs["input_ids"],
                    max_new_tokens=output_limit,
                    use_cache=True,
                    return_dict_in_generate=True,
                )
            new_tokens = generated.sequences[0][input_length:]
            return str(self._tokenizer.decode(new_tokens, skip_special_tokens=True))

        return await asyncio.to_thread(invoke)


class LiteLLMBackend(SharedLLMBackend):
    """Async LiteLLM adapter for local servers or hosted providers.

    LiteLLM may route to a remote model or to a separately managed local
    server.  The single backend object still gives all three agents one
    shared provider configuration and one serialized request stream.
    """

    def __init__(self, model: str, **completion_kwargs: Any) -> None:
        super().__init__(serialize_requests=False)
        self.model = model
        self.completion_kwargs = completion_kwargs

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        try:
            from litellm import acompletion
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "litellm is required for LiteLLMBackend; install 3loop[litellm]"
            ) from exc

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(prompt, system_prompt),
            "temperature": temperature,
            **self.completion_kwargs,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await acompletion(**kwargs)
        return _extract_content(response)


class OllamaBackend(SharedLLMBackend):
    """Local backend calling a running Ollama server's chat API directly.

    Uses only the standard library so it works inside a frozen executable
    without bundling any extra HTTP client. Ollama must already be running
    locally (``ollama serve``, or the desktop app) with the model pulled.
    """

    def __init__(
        self,
        model: str,
        *,
        host: str = "http://localhost:11434",
        timeout: float = 120.0,
        keep_alive: str = "30m",
        thinking: bool | None = None,
    ) -> None:
        super().__init__(serialize_requests=False)
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.keep_alive = keep_alive
        # ``None`` preserves Ollama's model default. A bool is sent at the
        # top-level API field, which is where Ollama reads this capability.
        self.thinking = thinking
        self._num_thread = os.cpu_count() or 4

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        import urllib.error
        import urllib.request

        # keep_alive avoids paying the ~5-10s model (re)load cost on every
        # call; num_thread pins Ollama to all detected CPU cores instead of
        # a conservative default; num_ctx is capped to what our prompts
        # actually need instead of the model's much larger native context
        # (e.g. 32k), which otherwise inflates the KV-cache allocation and
        # prefill cost on every single call.
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(prompt, system_prompt),
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "num_thread": self._num_thread,
                "num_ctx": 4096,
            },
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if self.thinking is not None:
            payload["think"] = self.thinking
        action = _marker_value(prompt, "3LOOP_ACTION") or _marker_value(
            system_prompt or "", "3LOOP_ACTION"
        )
        if action in {"vote", "latent_debate", "gmail_batch"}:
            # Force valid, minimal JSON instead of letting a small model ramble
            # in prose before (or instead of) the JSON payload.
            if action == "vote":
                payload["format"] = {
                    "type": "object",
                    "properties": {
                        "resolved": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["resolved", "confidence", "rationale"],
                }
            elif action == "gmail_batch":
                payload["format"] = {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "summary": {"type": "string"},
                            "category": {"type": "string", "enum": ["publicité", "travail", "autre"]},
                        },
                        "required": ["index", "summary", "category"],
                    },
                }
            else:
                payload["format"] = "json"

        def invoke() -> str:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{self.host}/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"Impossible de joindre Ollama sur {self.host}; "
                    "verifiez que `ollama serve` tourne et que le modele est "
                    f"disponible (`ollama pull {self.model}`)."
                ) from exc
            return str(body.get("message", {}).get("content", ""))

        return await asyncio.to_thread(invoke)

    @staticmethod
    def list_models(host: str = "http://localhost:11434", *, timeout: float = 3.0) -> list[str]:
        """Return installed model names sorted smallest (fastest) first."""

        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError):
            return []
        entries = [entry for entry in body.get("models", []) if "name" in entry]
        entries.sort(key=lambda entry: entry.get("size", 0))
        return [entry["name"] for entry in entries]


class CloudApiBackend(SharedLLMBackend):
    """Free-tier cloud backend for any OpenAI-compatible chat completions API.

    Both Groq and NVIDIA's build.nvidia.com catalog expose a free API key
    (no credit card) behind the same OpenAI-style ``/chat/completions``
    shape, so one small client covers both. Uses only the standard library
    so it works unmodified inside a frozen executable.
    """

    #: name -> (base_url, recommended models fastest/lightest first, signup hint)
    PROVIDERS: dict[str, tuple[str, tuple[str, ...], str]] = {
        "groq": (
            "https://api.groq.com/openai/v1",
            ("llama-3.1-8b-instant", "gemma2-9b-it", "llama-3.3-70b-versatile"),
            "https://console.groq.com/keys",
        ),
        "nvidia": (
            "https://integrate.api.nvidia.com/v1",
            (
                "nvidia/llama-3.1-nemotron-nano-8b-v1",
                "nvidia/llama-3.3-nemotron-super-49b-v1",
                "nvidia/llama-3.1-nemotron-70b-instruct",
            ),
            "https://build.nvidia.com",
        ),
    }

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(serialize_requests=False)
        if not api_key.strip():
            raise ValueError("une cle API gratuite est requise pour ce backend cloud")
        self.model = model
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def for_provider(cls, provider: str, model: str, api_key: str) -> "CloudApiBackend":
        """Build a backend for a known provider key (``groq`` or ``nvidia``)."""

        base_url, _, _ = cls.PROVIDERS[provider]
        return cls(model, api_key, base_url=base_url)

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        import urllib.error
        import urllib.request

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(prompt, system_prompt),
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        def invoke() -> str:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    # Cloudflare (in front of the Groq API) blocks the
                    # default urllib User-Agent; a browser-like one passes.
                    "User-Agent": "Mozilla/5.0 (3loop)",
                },
                method="POST",
            )
            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        body = json.loads(response.read().decode("utf-8"))
                    return _extract_content(body)
                except urllib.error.HTTPError as exc:
                    if exc.code == 429 and attempt < max_attempts:
                        wait_s = _retry_after_seconds(exc.headers.get("Retry-After"), attempt)
                        time.sleep(wait_s)
                        continue
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                    if exc.code == 429:
                        raise RuntimeError(
                            f"Limite de requetes/minute atteinte sur {self.base_url} "
                            "apres plusieurs tentatives. Reduis les cycles ou "
                            "reessaie plus tard."
                        ) from exc
                    raise RuntimeError(f"Erreur API cloud ({exc.code}): {detail}") from exc
                except urllib.error.URLError as exc:
                    raise RuntimeError(f"Impossible de joindre l'API cloud ({self.base_url}): {exc}") from exc
            raise RuntimeError(f"Echec apres {max_attempts} tentatives sur {self.base_url}")

        return await asyncio.to_thread(invoke)


class DemoBackend(SharedLLMBackend):
    """Deterministic offline backend for the CLI, UI smoke tests, and examples."""

    def __init__(self, *, resolved_after: int = 1) -> None:
        super().__init__()
        if resolved_after < 1:
            raise ValueError("resolved_after must be at least one")
        self.resolved_after = resolved_after
        self.calls = 0

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        del temperature, max_tokens
        self.calls += 1
        action = _marker_value(prompt, "3LOOP_ACTION") or _marker_value(
            system_prompt or "", "3LOOP_ACTION"
        )
        role = _marker_value(prompt, "3LOOP_ROLE") or _marker_value(
            system_prompt or "", "3LOOP_ROLE"
        )
        cycle = int(_marker_value(prompt, "3LOOP_CYCLE") or "1")
        task_kind = _marker_value(prompt, "3LOOP_KIND") or "general"
        task = _section_after(prompt, "TASK:")
        task_excerpt = task[:80] or "la demande"

        if action == "search_query":
            return f"{task[:120]} reliable documentation proof implementation"
        if action == "vote":
            resolved = cycle >= self.resolved_after
            agent_name = role or "agent"
            confidence = min(0.95, 0.55 + 0.15 * cycle)
            rationale = (
                f"Cycle {cycle} ({agent_name}): la reponse a \"{task_excerpt}\" "
                + (
                    "couvre les cas verifies, aucune faille bloquante restante."
                    if resolved
                    else "laisse encore des cas limites non traites."
                )
            )
            return (
                f'{{"resolved": {str(resolved).lower()}, '
                f'"confidence": {confidence:.2f}, "rationale": {json.dumps(rationale)}}}'
            )
        if action == "latent_debate":
            resolved = cycle >= self.resolved_after
            if task_kind == "code":
                solution = (
                    "```python\n"
                    "def solve(value):\n"
                    "    if value is None:\n"
                    '        raise ValueError("value must not be None")\n'
                    "    return value\n"
                    "```"
                )
            elif task_kind == "math":
                solution = "\\[\\text{Solution compacte: hypotheses, derivation et verification.}\\]"
            else:
                solution = f"Solution finale pour \"{task_excerpt}\": verifiee et structuree."
            vote = {
                "resolved": resolved,
                "confidence": min(0.95, 0.55 + 0.15 * cycle),
                "rationale": f"Vote compact de {role or 'agent'} au cycle {cycle}.",
            }
            return json.dumps(
                {
                    "heuristic": f"Plan compact pour {task_excerpt}.",
                    "critique": f"Verifier les cas limites de {task_excerpt}.",
                    "final_solution": solution,
                    "votes": [
                        {"role": role_name, **vote}
                        for role_name in ("heuristic", "critic", "writer")
                    ],
                }
            )
        if role == "heuristic":
            return (
                f'Plan initial pour "{task_excerpt}": formaliser les hypotheses, '
                "esquisser une premiere approche et enumerer les cas limites "
                "(entree vide, valeurs extremes, types invalides) a verifier."
            )
        if role == "critic":
            remaining = "Plusieurs points restent a corriger avant la version finale."
            if cycle > 1:
                remaining = "Les points souleves au cycle precedent sont desormais corriges."
            return (
                f'Revue critique du plan pour "{task_excerpt}": verifier les '
                "preconditions, la terminaison, la complexite et les cas limites. "
                f"{remaining}"
            )
        if role == "writer":
            if task_kind == "code":
                return (
                    "```python\n"
                    "def solve(value):\n"
                    f'    """Solution pour: {task_excerpt}"""\n'
                    "    if value is None:\n"
                    '        raise ValueError("value must not be None")\n'
                    "    return value\n"
                    "```"
                )
            if task_kind == "math":
                return (
                    f"\\[\\text{{Probleme: {task_excerpt}.}} \\quad "
                    "\\text{Hypotheses posees, derivation effectuee, resultat "
                    "verifie dans chaque cas limite.}\\]"
                )
            return (
                f'Solution finale pour "{task_excerpt}": hypotheses, derivation, '
                "verification et limites."
            )
        return "Reponse de demonstration."


def _retry_after_seconds(header_value: str | None, attempt: int) -> float:
    """Honor a Retry-After header when present, else back off exponentially."""

    if header_value:
        try:
            return min(20.0, max(0.5, float(header_value)))
        except ValueError:
            pass
    return min(20.0, 1.5 * (2 ** (attempt - 1)))


def _messages(prompt: str, system_prompt: str | None) -> list[dict[str, str]]:
    """Build the common chat message shape expected by local and hosted APIs."""

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _extract_content(response: Any) -> str:
    """Extract text from common OpenAI-compatible response shapes."""

    if isinstance(response, Mapping):
        choices = response.get("choices", [])
        if choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message", first)
                if isinstance(message, Mapping):
                    return str(message.get("content", ""))
                return str(message)
    choices = getattr(response, "choices", None)
    if choices:
        first = choices[0]
        message = getattr(first, "message", first)
        content = getattr(message, "content", message)
        return str(content)
    return str(response)


def _marker_value(text: str, marker: str) -> str | None:
    """Read a simple ``MARKER=value`` line used by the offline backend."""

    prefix = f"{marker}="
    for line in text.splitlines():
        if line.strip().startswith(prefix):
            return line.strip()[len(prefix) :].strip()
    return None


def _section_after(text: str, marker: str) -> str:
    """Return the first line after a prompt section marker."""

    if marker not in text:
        return text.strip()
    tail = text.split(marker, 1)[1].strip()
    return tail.splitlines()[0] if tail else ""
