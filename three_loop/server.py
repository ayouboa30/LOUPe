"""Stdlib-only local HTTP+SSE server exposing the 3loop engine to the desktop UI.

No web framework dependency: a threaded ``http.server`` handler serves the
static frontend and streams pipeline events as Server-Sent Events, so the
whole desktop build stays small and dependency-free.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .assistant_actions import save_last_run_config
from .backend import AirLLMBackend, CloudApiBackend, DemoBackend, LlamaCppBackend, LiteLLMBackend, OllamaBackend, SharedLLMBackend
from .compact import compact_text
from .igpu import ensure_server as ensure_igpu_server, probe as igpu_probe
from .models import AGENT_ROLES, AgentRole, EventType, PipelineEvent, TaskKind
from .opencode_backend import (
    DEFAULT_MODEL as OPENCODE_DEFAULT_MODEL,
    OpenCodeBackend,
    find_opencode,
    list_models as _opencode_models,
)
from .pipeline import PipelineConfig, ThreeLoopPipeline
from .temperature import TemperatureOptimizer
from .scrape import fetch_page
from .web import DuckDuckGoSearchProvider

#: One optimizer per client session id, so the temperature prior keeps
#: learning across turns of the same conversation instead of resetting.
_SESSIONS: dict[str, TemperatureOptimizer] = {}

#: Loading a 3B GGUF costs several seconds and hundreds of MB, so the model
#: is kept resident across requests instead of being rebuilt per message.
#: Its RAM prefix cache also only pays off when the instance survives.
_LLAMA_BACKENDS: dict[str, LlamaCppBackend] = {}


def _shared_llama_cpp(model_path: str) -> LlamaCppBackend:
    """Return a process-wide backend for ``model_path``, loading it once."""

    backend = _LLAMA_BACKENDS.get(model_path)
    if backend is None:
        backend = LlamaCppBackend(model_path)
        _LLAMA_BACKENDS[model_path] = backend
    return backend


#: Listing OpenCode's models spawns a subprocess that can take seconds, and
#: /api/config is hit on every page load. The answer only changes when the
#: user installs something, so it is resolved once per process.
_OPENCODE_CONFIG: dict[str, Any] | None = None


def _opencode_config() -> dict[str, Any]:
    """Report whether OpenCode is usable and which models it offers."""

    global _OPENCODE_CONFIG
    if _OPENCODE_CONFIG is None:
        executable = find_opencode()
        if executable is None:
            _OPENCODE_CONFIG = {"available": False, "models": [], "default": ""}
        else:
            models = _opencode_models()
            _OPENCODE_CONFIG = {
                "available": True,
                "models": models,
                "default": (
                    OPENCODE_DEFAULT_MODEL
                    if OPENCODE_DEFAULT_MODEL in models
                    else (models[0] if models else OPENCODE_DEFAULT_MODEL)
                ),
                "log_path": str(Path.home() / ".3loop" / "opencode.log"),
            }
    return _OPENCODE_CONFIG


def _support_backend(main_backend: SharedLLMBackend) -> SharedLLMBackend:
    """Pick the smallest local GGUF to run the summarising support roles.

    Those roles do not reason about the task, so model size buys nothing
    there while costing a lot: measured on a Ryzen 7 5825U, Qwen2.5-Coder
    0.5B runs at 260 tok/s prefill / 58 tok/s decode against 69 / 19 for the
    3B. Falls back to the main backend when no smaller model is installed,
    or when the main backend is not a local one (a cloud API has no such
    cost asymmetry).
    """

    if not isinstance(main_backend, LlamaCppBackend):
        return main_backend
    candidates = []
    for _label, path in _discover_local_gguf():
        try:
            candidates.append((Path(path).stat().st_size, path))
        except OSError:
            continue
    if not candidates:
        return main_backend
    smallest_size, smallest_path = min(candidates)
    if smallest_path == main_backend.model_path:
        return main_backend
    try:
        main_size = Path(main_backend.model_path).stat().st_size
    except OSError:
        return main_backend
    # Only worth a second resident model if it is materially smaller.
    if smallest_size > main_size * 0.6:
        return main_backend
    try:
        return _shared_llama_cpp(smallest_path)
    except Exception:
        return main_backend


def _discover_local_gguf() -> list[tuple[str, str]]:
    """List local GGUF weights, or nothing when llama-cpp-python is absent."""

    try:
        return LlamaCppBackend.discover_ollama_models()
    except Exception:
        return []


def _web_dir() -> Path:
    """Resolve the static frontend directory in both normal and frozen modes."""

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / "web"


def _build_backend(payload: dict[str, Any]) -> SharedLLMBackend:
    backend_name = payload.get("backend", "demo")
    if backend_name == "opencode":
        model = (payload.get("model") or "").strip()
        return OpenCodeBackend(model or OPENCODE_DEFAULT_MODEL)
    if backend_name == "groq":
        return CloudApiBackend.for_provider("groq", payload["model"], payload.get("api_key", ""))
    if backend_name == "nvidia":
        return CloudApiBackend.for_provider("nvidia", payload["model"], payload.get("api_key", ""))
    if backend_name == "igpu":
        model = (payload.get("model") or "").strip()
        if not model:
            raise ValueError("aucun modele Ollama disponible pour le mode iGPU")
        host = ensure_igpu_server()
        if host is None:
            raise ValueError(
                "Impossible de demarrer le serveur iGPU (Ollama introuvable "
                "ou demarrage echoue). Bascule sur un autre backend."
            )
        return OllamaBackend(model, host=host)
    if backend_name == "ollama":
        model = payload.get("model", "").strip()
        if not model:
            raise ValueError("aucun modele Ollama disponible; lancez `ollama serve` et `ollama pull <modele>`")
        return OllamaBackend(model)
    if backend_name == "llama_cpp":
        # The UI sends the GGUF path itself as the "model" value for this
        # backend, since discovery already resolved it to a real blob path.
        model_path = (payload.get("model_path") or payload.get("model") or "").strip()
        if not model_path:
            raise ValueError("un chemin GGUF est requis")
        return _shared_llama_cpp(model_path)
    if backend_name == "airllm":
        model = payload.get("model", "").strip()
        if not model:
            raise ValueError("un identifiant ou chemin de modele AirLLM est requis")
        return AirLLMBackend(
            model,
            device=str(payload.get("device", "cpu")),
            compression=payload.get("compression") or None,
            layer_shards_saving_path=payload.get("layer_shards_saving_path") or None,
        )
    if backend_name == "litellm":
        return LiteLLMBackend(payload.get("model", "").strip())
    return DemoBackend(resolved_after=2)


def _serialize_event(event: PipelineEvent) -> dict[str, Any]:
    """Flatten one pipeline event into a small JSON-safe payload."""

    data: dict[str, Any] = {"message": event.message, "cycle": event.cycle}
    if event.role is not None:
        data["role"] = event.role.value
        data["role_label"] = event.role.label
    if event.content is not None:
        data["content"] = event.content

    if event.event_type is EventType.CYCLE_STARTED:
        data["temperatures"] = {
            role.value: temp for role, temp in event.data.get("temperatures", {}).items()
        }
    elif event.event_type is EventType.VOTE:
        vote = event.data.get("vote")
        if vote is not None:
            data["resolved"] = vote.resolved
            data["confidence"] = vote.confidence
            data["rationale"] = vote.rationale
    elif event.event_type is EventType.RESEARCH_SOURCES:
        research = event.data.get("research")
        data["sources"] = (
            [
                {"title": s.title or s.domain, "url": s.url, "domain": s.domain}
                for s in research.sources
            ]
            if research is not None
            else []
        )
    elif event.event_type is EventType.PRIOR_UPDATED:
        posterior = event.data.get("posterior", {})
        # AGENT_ROLES, not every AgentRole: only the debating roles carry a
        # temperature prior, so support roles would plot a flat zero.
        data["posterior"] = {
            role.value: posterior.get(role.value, {}).get("mean_temperature", 0.0)
            for role in AGENT_ROLES
        }
    elif event.event_type is EventType.CYCLE_COMPLETED:
        cycle_result = event.data.get("cycle_result")
        if cycle_result is not None:
            data["reward"] = cycle_result.reward
            data["votes_approved"] = cycle_result.consensus.approved_count
    elif event.event_type is EventType.RUN_COMPLETED and event.result is not None:
        result = event.result
        data["final_solution"] = result.final_solution
        data["consensus_reached"] = result.consensus_reached
        data["completed_cycles"] = result.completed_cycles
        data["kind"] = result.kind.value
    return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/api/config":
            self._send_json(
                {
                    "ollama_models": OllamaBackend.list_models(),
                    "local_gguf": [
                        {"label": label, "path": path}
                        for label, path in _discover_local_gguf()
                    ],
                    "cloud_providers": {
                        name: {"models": list(models), "signup_url": signup}
                        for name, (_, models, signup) in CloudApiBackend.PROVIDERS.items()
                    },
                    "opencode": _opencode_config(),
                    "igpu": igpu_probe(),
                }
            )
            return
        self._serve_static()

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/api/run":
            self._handle_run()
        elif self.path == "/api/scrape":
            self._handle_scrape()
        else:
            self.send_error(404)

    def _handle_scrape(self) -> None:
        """Fetch a URL, strip it to readable text, and return it compacted.

        Runs synchronously (a single page fetch, not the streamed debate) so
        the composer's "attach a link" affordance gets an answer back before
        the request it's building even starts.
        """

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400)
            return
        url = str(payload.get("url", "")).strip()
        if not url:
            self._send_json({"error": "URL manquante."})
            return
        try:
            title, text = fetch_page(url)
        except ValueError as exc:
            self._send_json({"error": str(exc)})
            return
        compacted = compact_text(text, max_tokens=1200)
        self._send_json({"title": title, "url": url, "text": compacted})

    def _serve_static(self) -> None:
        rel_path = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        if ".." in rel_path:
            self.send_error(403)
            return
        target = _web_dir() / rel_path
        if not target.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_run(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # Without an explicit close, the client's fetch reader never sees
        # EOF after the last SSE event (no Content-Length, no chunked
        # terminator), so it hangs forever waiting for more data.
        self.close_connection = True

        def emit(event_name: str, data: dict[str, Any]) -> None:
            chunk = f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                raise StopIteration

        save_last_run_config(payload)
        try:
            backend = _build_backend(payload)
        except Exception as exc:  # configuration error before any run starts
            try:
                emit("error", {"message": str(exc)})
            except StopIteration:
                pass
            return

        session_id = str(payload.get("session_id", "default"))
        optimizer = _SESSIONS.setdefault(session_id, TemperatureOptimizer(seed=7))
        research = bool(payload.get("research", False))
        provider = DuckDuckGoSearchProvider() if research else None
        pipeline = ThreeLoopPipeline(
            backend,
            optimizer=optimizer,
            config=PipelineConfig(
                max_cycles=int(payload.get("max_cycles", 2)),
                research_enabled=research,
                max_tokens=int(payload.get("max_tokens", 256)),
                compact_debate=bool(payload.get("compact_debate", True)),
                lazy_debate_fields=bool(payload.get("lazy_debate_fields", True)),
            ),
            search_provider=provider,
            support_backend=_support_backend(backend),
        )
        kind_raw = payload.get("task_kind")
        explicit_kind = None if not kind_raw or kind_raw == "auto" else TaskKind(kind_raw)
        prompt = str(payload.get("prompt", ""))

        async def run() -> None:
            async for event in pipeline.stream(prompt, kind=explicit_kind, research=research):
                emit(event.event_type.value, _serialize_event(event))

        try:
            asyncio.run(run())
        except StopIteration:
            pass
        except Exception as exc:  # keep the SSE stream informative on failure
            try:
                emit("error", {"message": str(exc)})
            except StopIteration:
                pass


def run_server(port: int) -> None:
    """Start the blocking threaded HTTP server on ``127.0.0.1:port``."""

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()
