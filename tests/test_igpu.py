"""Guards on the iGPU path.

The measured asymmetry is the whole reason this backend exists: on a
Ryzen 5000U the iGPU shares the CPU's DDR4 controller, so it adds FLOPs but
no bandwidth. Prefill (compute-bound) gained +32%, decode (memory-bound)
+9%. Enabling it must therefore stay a deliberate choice, and must never
break the CPU path when the hardware or Ollama is missing.
"""

from three_loop.igpu import IGPU_PORT, REQUIRED_ENV, igpu_host, probe


def test_required_env_names_both_switches() -> None:
    """Ollama needs OLLAMA_VULKAN *and* OLLAMA_IGPU_ENABLE.

    With only the first, it detects the device then logs "dropping
    integrated GPU" and silently falls back to CPU - which is exactly the
    failure that made an earlier measurement look like the iGPU was useless.
    """

    assert REQUIRED_ENV["OLLAMA_VULKAN"] == "1"
    assert REQUIRED_ENV["OLLAMA_IGPU_ENABLE"] == "1"


def test_managed_server_uses_a_private_port() -> None:
    """3loop must not restart or reconfigure the user's system-wide Ollama."""

    assert IGPU_PORT != 11434  # the default Ollama port
    assert igpu_host() == f"http://127.0.0.1:{IGPU_PORT}"


def test_probe_degrades_gracefully_when_no_server_answers(monkeypatch) -> None:
    """A missing server must report, not raise: callers fall back to CPU."""

    import three_loop.igpu as igpu

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(igpu.urllib.request, "urlopen", boom)
    result = probe(timeout=0.1)

    assert result["reachable"] is False
    assert result["using_gpu"] is False
    assert result["loaded_models"] == []
    assert "OLLAMA_IGPU_ENABLE" in result["how_to_enable"]


def test_ensure_server_returns_none_when_ollama_is_absent(monkeypatch) -> None:
    """No Ollama on the machine must not be an error - just no iGPU path."""

    import three_loop.igpu as igpu

    monkeypatch.setattr(igpu, "_is_up", lambda host, timeout=1.5: False)
    monkeypatch.setattr(igpu.shutil, "which", lambda name: None)

    assert igpu.ensure_server(startup_timeout=0.1) is None


def test_ensure_server_reuses_an_already_running_instance(monkeypatch) -> None:
    """Starting a second server on the same port would just fail to bind."""

    import three_loop.igpu as igpu

    started = []
    monkeypatch.setattr(igpu, "_is_up", lambda host, timeout=1.5: True)
    monkeypatch.setattr(igpu.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or None)

    assert igpu.ensure_server() == igpu_host()
    assert started == []


def test_routing_sends_generation_heavy_work_to_the_cpu() -> None:
    """Math proofs measured 3.8% *slower* on the iGPU (ratio 0.10 << 2.3)."""

    from three_loop.igpu import should_use_igpu

    # Short question, long proof: the shape of the measured math eval.
    assert should_use_igpu(prompt_chars=170, max_tokens=700) is False


def test_routing_sends_prompt_heavy_work_to_the_igpu() -> None:
    """Long context, short answer: prefill dominates and is compute-bound."""

    from three_loop.igpu import should_use_igpu

    # ~5400 chars of context (~1500 tokens) for a brief answer.
    assert should_use_igpu(prompt_chars=5400, max_tokens=300) is True


def test_routing_threshold_matches_the_measured_rates() -> None:
    """68.7/18.8 (CPU) vs 114.5/15.0 (iGPU) solve to 2.31."""

    from three_loop.igpu import BREAK_EVEN_RATIO

    cpu_prefill, cpu_decode = 68.7, 18.8
    gpu_prefill, gpu_decode = 114.5, 15.0
    # iGPU wins when  P/gpu_pre + G/gpu_dec  <  P/cpu_pre + G/cpu_dec.
    # The iGPU is faster at prefill but slower at decode, so both bracketed
    # terms below are positive and the inequality solves to P/G > ratio.
    decode_penalty = 1 / gpu_decode - 1 / cpu_decode
    prefill_saving = 1 / cpu_prefill - 1 / gpu_prefill
    expected = decode_penalty / prefill_saving

    assert abs(BREAK_EVEN_RATIO - expected) < 0.1
