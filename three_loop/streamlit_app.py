"""Streamlit chat interface for the 3loop debate and temperature posterior."""

from __future__ import annotations

import asyncio
from typing import Any

from .backend import (
    CloudApiBackend,
    DemoBackend,
    LlamaCppBackend,
    LiteLLMBackend,
    OllamaBackend,
    SharedLLMBackend,
)
from .models import AgentRole, EventType, RunResult, SourceMatch, TaskKind, Vote
from .pipeline import PipelineConfig, ThreeLoopPipeline
from .temperature import TemperatureOptimizer
from .web import DuckDuckGoSearchProvider

ROLE_EMOJI: dict[AgentRole, str] = {
    AgentRole.HEURISTIC: "🧠",
    AgentRole.CRITIC: "🔍",
    AgentRole.WRITER: "✍️",
}

_CUSTOM_CSS = """
<style>
[data-testid="stChatMessage"] {
    border: 1px solid rgba(128,128,128,0.15);
    border-radius: 0.6rem;
    padding: 0.4rem 0.2rem;
}
.tl-panel-card {
    border: 1px solid rgba(128,128,128,0.18);
    border-radius: 0.5rem;
    padding: 0.6rem 0.75rem;
    margin-bottom: 0.6rem;
}
.tl-panel-card h4 { margin: 0 0 0.4rem 0; font-size: 0.85rem; opacity: 0.75; text-transform: uppercase; letter-spacing: 0.04em; }
.tl-badge {
    display: inline-block;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    font-size: 0.75rem;
    border: 1px solid rgba(128,128,128,0.35);
    margin-right: 0.3rem;
}
.tl-badge.ok { border-color: #34d399; color: #34d399; }
.tl-badge.warn { border-color: #fbbf24; color: #fbbf24; }
.tl-debate-line { font-size: 0.85rem; margin-bottom: 0.5rem; }
.tl-debate-role { font-weight: 600; }
.tl-muted { opacity: 0.6; font-size: 0.85rem; }
</style>
"""


def main() -> None:
    """Render the interactive chat application with a ChatGPT-style layout."""

    try:
        import streamlit as st
    except ImportError as exc:  # pragma: no cover - optional UI dependency
        raise RuntimeError("Install 3loop[ui] to run the Streamlit application") from exc

    st.set_page_config(page_title="3loop", page_icon="🔁", layout="wide")
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)
    top_error = st.empty()

    ollama_models = OllamaBackend.list_models()

    cloud_providers = {
        "☁️ Groq (cloud gratuit)": "groq",
        "☁️ NVIDIA Nemotron (cloud gratuit)": "nvidia",
    }

    with st.sidebar:
        st.header("🔁 3loop")
        backend_options = [
            *cloud_providers.keys(),
            "Ollama (local)",
            "Demo hors-ligne",
            "Llama.cpp",
            "LiteLLM",
        ]
        stored_keys = st.session_state.setdefault("tl_cloud_keys", {})
        default_index = (
            0 if any(stored_keys.values()) else (len(cloud_providers) if ollama_models else len(cloud_providers) + 1)
        )
        backend_name = st.selectbox(
            "Backend LLM",
            backend_options,
            index=default_index,
            help=(
                "Groq / NVIDIA: API cloud gratuite (cle sans carte bancaire), tres rapide. "
                "Ollama (local): modele deja installe, aucune dependance mais limite par "
                "ton CPU. Demo hors-ligne: reponses simulees. Llama.cpp / LiteLLM: paquets "
                "Python additionnels requis."
            ),
        )

        ollama_model = ""
        cloud_provider = ""
        cloud_key = ""
        cloud_model = ""
        if backend_name in cloud_providers:
            cloud_provider = cloud_providers[backend_name]
            base_url, models, signup_url = CloudApiBackend.PROVIDERS[cloud_provider]
            cloud_key = st.text_input(
                "Cle API (gratuite)",
                value=stored_keys.get(cloud_provider, ""),
                type="password",
                help=f"Creer une cle gratuite sur {signup_url} (aucune carte bancaire requise).",
            )
            stored_keys[cloud_provider] = cloud_key
            cloud_model = st.selectbox("Modele", list(models), index=0)
            if not cloud_key.strip():
                st.caption(f"⚠️ Colle ta cle API gratuite ({signup_url}) ci-dessus pour activer ce backend.")
        elif backend_name == "Ollama (local)":
            if ollama_models:
                ollama_model = st.selectbox(
                    "Modele", ollama_models, index=0, help="Tries du plus leger au plus lourd."
                )
                st.caption("ℹ️ Inference sur CPU (pas de GPU detecte): comptez plusieurs secondes par reponse.")
            else:
                st.caption("⚠️ Aucun serveur Ollama detecte sur localhost:11434.")
        elif backend_name == "Llama.cpp":
            st.caption("⚠️ Necessite `pip install llama-cpp-python` + un fichier .gguf.")
        elif backend_name == "LiteLLM":
            st.caption("⚠️ Necessite `pip install litellm` + un serveur/API accessible.")

        research = st.checkbox("Recherche web triangulee", value=False)

        with st.expander("Parametres avances"):
            max_cycles = st.slider("Cycles maximum", min_value=1, max_value=12, value=2)
            task_kind = st.selectbox("Type de sortie", ["auto", *[kind.value for kind in TaskKind]])
            max_tokens = st.slider(
                "Longueur max par reponse (tokens)",
                min_value=64,
                max_value=2048,
                value=256,
                step=64,
                help="Plus bas = reponses plus rapides avec un petit modele local sur CPU.",
            )
            seed = st.number_input("Seed du prior", min_value=0, value=7, step=1)
            model_path = st.text_input("Chemin modele GGUF (Llama.cpp)", value="")
            litellm_model = st.text_input("Modele LiteLLM", value="ollama/qwen2.5-coder:7b")

        if st.button("🗑️ Nouvelle conversation", use_container_width=True):
            st.session_state["tl_messages"] = []
            st.session_state["tl_last_debate"] = ""
            st.session_state["tl_last_sources"] = ()
            st.rerun()

    if "tl_messages" not in st.session_state:
        st.session_state["tl_messages"] = []
    if "tl_temp_rows" not in st.session_state:
        st.session_state["tl_temp_rows"] = []
    if "tl_optimizer" not in st.session_state:
        st.session_state["tl_optimizer"] = TemperatureOptimizer(seed=int(seed))
    if "tl_last_debate" not in st.session_state:
        st.session_state["tl_last_debate"] = ""
    if "tl_last_sources" not in st.session_state:
        st.session_state["tl_last_sources"] = ()
    if "tl_last_status" not in st.session_state:
        st.session_state["tl_last_status"] = None

    chat_col, side_col = st.columns([3, 1], gap="medium")

    with chat_col:
        for message in st.session_state["tl_messages"]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message.get("meta"):
                    st.caption(message["meta"])

        prompt = st.chat_input("Pose ta question (code, maths, recherche...)")

    with side_col:
        _render_side_panel(st)

    if not prompt:
        return

    st.session_state["tl_messages"].append({"role": "user", "content": prompt})

    try:
        backend = _make_backend(
            backend_name,
            model_path,
            litellm_model,
            ollama_model,
            cloud_providers,
            cloud_provider,
            cloud_key,
            cloud_model,
        )
        provider = DuckDuckGoSearchProvider() if research else None
        pipeline = ThreeLoopPipeline(
            backend,
            optimizer=st.session_state["tl_optimizer"],
            config=PipelineConfig(
                max_cycles=max_cycles,
                research_enabled=research,
                max_tokens=int(max_tokens),
            ),
            search_provider=provider,
        )
    except Exception as exc:
        top_error.error(f"⚠️ Configuration impossible: {exc}")
        return

    with chat_col:
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            answer_box = st.empty()
            answer_box.markdown("_Les agents se concertent…_")

            try:
                result, debate_lines, sources = asyncio.run(
                    _run_and_collect(pipeline, prompt, task_kind, research)
                )
            except Exception as exc:
                answer_box.empty()
                top_error.error(f"⚠️ Erreur d'execution: {exc}")
                return

            answer_box.markdown(result.final_solution)
            meta = (
                f"{result.completed_cycles} cycle(s) · "
                f"{'consensus' if result.consensus_reached else 'pas de consensus'} · "
                f"{backend_name}"
            )
            st.caption(meta)

    for observation in result.temperature_history:
        st.session_state["tl_temp_rows"].append(
            {
                "cycle": float(observation.cycle),
                "heuristique": st.session_state["tl_optimizer"].mean_temperature(AgentRole.HEURISTIC),
                "critique": st.session_state["tl_optimizer"].mean_temperature(AgentRole.CRITIC),
                "redacteur": st.session_state["tl_optimizer"].mean_temperature(AgentRole.WRITER),
            }
        )

    st.session_state["tl_messages"].append(
        {"role": "assistant", "content": result.final_solution, "meta": meta}
    )
    st.session_state["tl_last_debate"] = "\n\n".join(debate_lines)
    st.session_state["tl_last_sources"] = sources
    st.session_state["tl_last_status"] = result.consensus_reached
    st.rerun()


def _render_side_panel(st: Any) -> None:
    """Draw the ChatGPT-style right rail: run status, sources, debate detail."""

    status = st.session_state.get("tl_last_status")
    st.markdown("<div class='tl-panel-card'><h4>Sorties</h4>", unsafe_allow_html=True)
    if status is None:
        st.markdown("<span class='tl-muted'>Aucune execution pour l'instant.</span>", unsafe_allow_html=True)
    else:
        badge_class = "ok" if status else "warn"
        badge_text = "Consensus atteint" if status else "Pas de consensus"
        st.markdown(f"<span class='tl-badge {badge_class}'>{badge_text}</span>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='tl-panel-card'><h4>Sources</h4>", unsafe_allow_html=True)
    sources: tuple[SourceMatch, ...] = st.session_state.get("tl_last_sources", ())
    if not sources:
        st.markdown("<span class='tl-muted'>Aucune source pour l'instant.</span>", unsafe_allow_html=True)
    else:
        for source in sources:
            st.markdown(f"- [{source.title or source.domain}]({source.url})")
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='tl-panel-card'><h4>Detail du debat</h4>", unsafe_allow_html=True)
    debate = st.session_state.get("tl_last_debate", "")
    if not debate:
        st.markdown("<span class='tl-muted'>Rien a afficher pour l'instant.</span>", unsafe_allow_html=True)
    else:
        with st.expander("Voir les echanges", expanded=False):
            st.markdown(debate)
    st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state.get("tl_temp_rows"):
        st.markdown("<div class='tl-panel-card'><h4>Prior de temperature</h4>", unsafe_allow_html=True)
        st.line_chart(
            st.session_state["tl_temp_rows"],
            x="cycle",
            y=["heuristique", "critique", "redacteur"],
            height=160,
        )
        st.markdown("</div>", unsafe_allow_html=True)


async def _run_and_collect(
    pipeline: ThreeLoopPipeline,
    prompt: str,
    task_kind: str,
    research: bool,
) -> tuple[RunResult, list[str], tuple[SourceMatch, ...]]:
    """Run one full pipeline call and collect a debate transcript plus sources."""

    explicit_kind = None if task_kind == "auto" else TaskKind(task_kind)
    debate_lines: list[str] = []
    sources: tuple[SourceMatch, ...] = ()

    async for event in pipeline.stream(prompt, kind=explicit_kind, research=research):
        if event.event_type is EventType.AGENT_OUTPUT and event.role is not None:
            debate_lines.append(
                f"**{ROLE_EMOJI[event.role]} {event.role.label}** (cycle {event.cycle})\n\n{event.content}"
            )
        elif event.event_type is EventType.VOTE and event.role is not None:
            vote = event.data.get("vote")
            if vote is not None:
                debate_lines.append(
                    f"*Vote {event.role.label}: {'resolu' if vote.resolved else 'a revoir'} "
                    f"({vote.confidence:.0%})*"
                )
        elif event.event_type is EventType.RESEARCH_SOURCES:
            research_result = event.data.get("research")
            if research_result is not None and research_result.sources:
                sources = research_result.sources
        elif event.event_type is EventType.ERROR:
            debate_lines.append(f"**Erreur:** {event.message}")

        if event.result is not None:
            return event.result, debate_lines, sources

    raise RuntimeError("3loop n'a pas produit de resultat final")


def _make_backend(
    name: str,
    model_path: str,
    litellm_model: str,
    ollama_model: str,
    cloud_providers: dict[str, str],
    cloud_provider: str,
    cloud_key: str,
    cloud_model: str,
) -> SharedLLMBackend:
    if name in cloud_providers:
        return CloudApiBackend.for_provider(cloud_provider, cloud_model, cloud_key)
    if name == "Ollama (local)":
        if not ollama_model.strip():
            raise ValueError("aucun modele Ollama disponible; lancez `ollama serve` et `ollama pull <modele>`")
        return OllamaBackend(ollama_model.strip())
    if name == "Demo hors-ligne":
        return DemoBackend(resolved_after=2)
    if name == "Llama.cpp":
        if not model_path.strip():
            raise ValueError("un chemin GGUF est requis")
        return LlamaCppBackend(model_path.strip())
    return LiteLLMBackend(litellm_model.strip())


if __name__ == "__main__":
    main()
