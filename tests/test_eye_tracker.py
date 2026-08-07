from three_loop.eye_tracker import EyeTrackingService
from three_loop.research.bibliography import BibliographicEntry
from three_loop.research.storage import ResearchWorkspace


def test_eye_tracker_requests_help_after_confident_dwell() -> None:
    tracker = EyeTrackingService(dwell_seconds=3.0, stable_radius=0.04)

    first = tracker.observe((0.5, 0.5), 0.9, timestamp=10.0)
    assert first["state"] == "tracking"
    assert first["help_requested"] is False

    blocked = tracker.observe((0.51, 0.5), 0.9, timestamp=13.1)
    assert blocked["state"] == "blocked"
    assert blocked["help_requested"] is True
    assert blocked["event_seq"] == 1


def test_eye_tracker_drops_dwell_when_confidence_is_low() -> None:
    tracker = EyeTrackingService(dwell_seconds=1.0)
    tracker.observe((0.5, 0.5), 0.9, timestamp=10.0)
    unknown = tracker.observe(None, 0.1, timestamp=11.5)
    assert unknown["help_requested"] is False
    assert unknown["dwell_seconds"] == 0.0


def test_arxiv_identifier_import_is_idempotent(tmp_path) -> None:
    workspace = ResearchWorkspace(tmp_path)
    entry = BibliographicEntry(
        cite_key="arxiv:2401.12345",
        title="A local paper",
        year=2024,
        url="https://arxiv.org/abs/2401.12345",
        external_ids={"arXiv": "2401.12345"},
    )

    first = workspace.upsert_bibliography_entries([entry])
    second = workspace.upsert_bibliography_entries([entry])

    assert first[0]["status"] == "created"
    assert second[0]["status"] == "matched"
    assert len(workspace.list_papers()) == 1
