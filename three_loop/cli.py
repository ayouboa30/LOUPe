"""Command-line entry point for an offline or configured 3loop run."""

from __future__ import annotations

import argparse
import asyncio
import os

from .backend import DemoBackend, LlamaCppBackend, LiteLLMBackend, SharedLLMBackend
from .models import EventType, RunResult, TaskKind
from .pipeline import PipelineConfig, ThreeLoopPipeline
from .web import DuckDuckGoSearchProvider


def main() -> None:
    """Parse CLI arguments and stream the debate to standard output."""

    parser = argparse.ArgumentParser(prog="3loop")
    parser.add_argument("task", nargs="?", help="Problem to solve")
    parser.add_argument("--kind", choices=[kind.value for kind in TaskKind])
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--research", action="store_true")
    parser.add_argument(
        "--backend",
        choices=["demo", "llama", "litellm"],
        default="demo",
    )
    parser.add_argument("--model-path", default=os.getenv("THREELOOP_MODEL_PATH"))
    parser.add_argument(
        "--model",
        default=os.getenv("THREELOOP_LITELLM_MODEL", "ollama/qwen2.5-coder:7b"),
    )
    args = parser.parse_args()
    task = args.task or input("Probleme a resoudre: ").strip()
    backend = _build_backend(args)
    config = PipelineConfig(
        max_cycles=args.cycles,
        research_enabled=args.research,
    )
    provider = DuckDuckGoSearchProvider() if args.research else None
    pipeline = ThreeLoopPipeline(
        backend,
        config=config,
        search_provider=provider,
    )
    result = asyncio.run(_run(pipeline, task, args.kind, args.research))
    print("\n=== Solution finale ===\n")
    print(result.final_solution)
    print(
        f"\nConsensus: {'oui' if result.consensus_reached else 'non'} "
        f"({result.completed_cycles} cycle(s))"
    )


async def _run(
    pipeline: ThreeLoopPipeline,
    task: str,
    kind: str | None,
    research: bool,
) -> RunResult:
    async for event in pipeline.stream(task, kind=kind, research=research):
        if event.event_type is EventType.AGENT_OUTPUT:
            print(f"\n[{event.role.label}]\n{event.content}")
        elif event.event_type is EventType.VOTE:
            print(f"[{event.message}] {event.content}")
        elif event.event_type in {
            EventType.CYCLE_COMPLETED,
            EventType.RESEARCH_SOURCES,
        }:
            print(event.message)
        elif event.event_type is EventType.ERROR:
            print(event.message)
        if event.result is not None:
            return event.result
    raise RuntimeError("3loop did not emit a terminal result")


def _build_backend(args: argparse.Namespace) -> SharedLLMBackend:
    if args.backend == "demo":
        return DemoBackend()
    if args.backend == "llama":
        if not args.model_path:
            raise SystemExit("--model-path or THREELOOP_MODEL_PATH is required for llama")
        return LlamaCppBackend(args.model_path)
    return LiteLLMBackend(args.model)
