"""SQLite, content-addressed blobs, and provenance-aware document storage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..compact import compact_text
from ..documents import ExtractedPage, extract_pages
from .analysis import execute_recipe, profile_csv
from .bibliography import BibliographicEntry

_SCHEMA_VERSION = 1
_LATEST_SCHEMA_VERSION = 3
_SCHEMA_NAME = "scientific-workspace-core"
_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '',
    year INTEGER,
    status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identifiers (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    scheme TEXT NOT NULL,
    value_normalized TEXT NOT NULL,
    source_id TEXT,
    UNIQUE(scheme, value_normalized)
);
CREATE TABLE IF NOT EXISTS authors (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    orcid TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    author_id TEXT NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'author',
    PRIMARY KEY (paper_id, author_id, role)
);
CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    issn TEXT,
    kind TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    canonical_uri TEXT NOT NULL DEFAULT '',
    accessed_at TEXT NOT NULL,
    license TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    uri TEXT NOT NULL DEFAULT '',
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    media_type TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version_label TEXT NOT NULL,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    acquired_at TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id),
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(artifact_id, blob_hash)
);
CREATE INDEX IF NOT EXISTS idx_document_versions_blob ON document_versions(blob_hash);
CREATE TABLE IF NOT EXISTS transforms (
    id TEXT PRIMARY KEY,
    transform_type TEXT NOT NULL,
    version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    engine TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transform_inputs (
    transform_id TEXT NOT NULL REFERENCES transforms(id) ON DELETE CASCADE,
    input_type TEXT NOT NULL,
    input_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    PRIMARY KEY(transform_id, input_type, input_id)
);
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    physical_index INTEGER NOT NULL,
    logical_label TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    method TEXT NOT NULL,
    extraction_error TEXT NOT NULL DEFAULT '',
    transform_id TEXT NOT NULL REFERENCES transforms(id),
    UNIQUE(version_id, physical_index)
);
CREATE INDEX IF NOT EXISTS idx_pages_version ON pages(version_id, physical_index);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    method TEXT NOT NULL,
    transform_id TEXT NOT NULL REFERENCES transforms(id),
    UNIQUE(page_id, ordinal),
    CHECK(start_offset >= 0 AND end_offset >= start_offset)
);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id, ordinal);
CREATE VIRTUAL TABLE IF NOT EXISTS scientific_fts USING fts5(
    entity_type UNINDEXED,
    entity_id UNINDEXED,
    title,
    body,
    tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('supported', 'conflicting', 'unverified')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citations (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_id TEXT REFERENCES sources(id),
    version_id TEXT REFERENCES document_versions(id),
    page_id TEXT REFERENCES pages(id),
    chunk_id TEXT REFERENCES chunks(id),
    start_offset INTEGER,
    end_offset INTEGER,
    excerpt TEXT NOT NULL DEFAULT '',
    locator_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provenance_edges (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    transform_id TEXT REFERENCES transforms(id),
    source_id TEXT REFERENCES sources(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled','interrupted')),
    progress REAL NOT NULL DEFAULT 0,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sequence)
);
CREATE TABLE IF NOT EXISTS message_traces (
    id TEXT PRIMARY KEY,
    message_id TEXT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

_MIGRATION_2_SQL = r"""
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    parent_id TEXT REFERENCES collections(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY(collection_id, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS entity_tags (
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(tag_id, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    author TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    note_id TEXT REFERENCES notes(id) ON DELETE SET NULL,
    version_id TEXT REFERENCES document_versions(id) ON DELETE CASCADE,
    page_id TEXT REFERENCES pages(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE CASCADE,
    start_offset INTEGER,
    end_offset INTEGER,
    selected_text TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT 'yellow',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary_terms (
    id TEXT PRIMARY KEY,
    term TEXT NOT NULL UNIQUE,
    definition TEXT NOT NULL DEFAULT '',
    source_id TEXT REFERENCES sources(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_runs (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    profile TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS search_run_sources (
    run_id TEXT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY(run_id, provider, external_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT 'scientific',
    criteria_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
    review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    decision TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(review_id, paper_id)
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    blob_hash TEXT NOT NULL REFERENCES blobs(hash),
    schema_json TEXT NOT NULL DEFAULT '{}',
    row_count INTEGER,
    acquired_at TEXT NOT NULL,
    UNIQUE(dataset_id, blob_hash)
);
CREATE TABLE IF NOT EXISTS analysis_runs (
    id TEXT PRIMARY KEY,
    dataset_version_id TEXT REFERENCES dataset_versions(id),
    name TEXT NOT NULL,
    recipe_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATION_3_SQL = r"""
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    saved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    compacted INTEGER NOT NULL DEFAULT 0,
    compact_summary TEXT NOT NULL DEFAULT '',
    compact_mode TEXT NOT NULL DEFAULT '',
    compacted_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    messages_json TEXT NOT NULL DEFAULT '[]',
    conversation_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    backend TEXT NOT NULL CHECK(backend IN ('llama_cpp', 'igpu', 'ollama')),
    model TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
    prompt_hash TEXT NOT NULL DEFAULT '',
    response_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_events_created ON feedback_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_events_session ON feedback_events(session_id, created_at DESC);
"""

_workspace: "ResearchWorkspace | None" = None
_workspace_lock = threading.Lock()
_LOCAL_FEEDBACK_BACKENDS = frozenset({"llama_cpp", "igpu", "ollama"})
_MAX_FEEDBACK_EVENTS = 2048
_MAX_SAVED_CONVERSATION_MESSAGES = 200
_MAX_SAVED_CONVERSATION_CHARS = 1_000_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uuid() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _media_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".csv": "text/csv",
        ".log": "text/plain",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


def _chunks(text: str, *, limit: int = 1800) -> Iterable[tuple[int, int, str]]:
    """Yield deterministic character ranges without losing source offsets."""

    start = 0
    size = len(text)
    while start < size:
        end = min(size, start + limit)
        if end < size:
            boundary = text.rfind("\n", start + limit // 2, end)
            if boundary < 0:
                boundary = text.rfind(" ", start + limit // 2, end)
            if boundary > start:
                end = boundary + 1
        value = text[start:end]
        if value:
            yield start, end, value
        start = end


class ResearchWorkspace:
    """Thread-safe facade over per-operation SQLite connections."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        configured = data_dir or os.environ.get("THREE_LOOP_DATA_DIR")
        self.data_dir = Path(configured) if configured else Path.home() / ".3loop" / "research"
        self.data_dir = self.data_dir.expanduser().resolve()
        self.db_path = self.data_dir / "research.sqlite3"
        self.blob_dir = self.data_dir / "blobs"
        self._migration_lock = threading.Lock()
        self._initialized = False
        self._ensure_initialized()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=8.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=8000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterable[sqlite3.Connection]:
        """Open, commit/rollback, and close one SQLite connection.

        ``sqlite3.Connection``'s context manager commits transactions but does
        not close the connection. That is mostly invisible on Unix, but on
        Windows it can keep the database file open and cause intermittent
        ``database is locked`` errors or prevent a temporary workspace from
        being removed. Every operation uses this wrapper instead.
        """

        connection = self._connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._migration_lock:
            if self._initialized:
                return
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.blob_dir.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=8.0) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
                    "applied_at TEXT NOT NULL)"
                )
                migrations = (
                    (_SCHEMA_VERSION, _SCHEMA_NAME, _SCHEMA_SQL),
                    (2, "scientific-workspace-library-and-analysis", _MIGRATION_2_SQL),
                    (3, "local-conversations-and-feedback", _MIGRATION_3_SQL),
                )
                for version, name, sql in migrations:
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    row = connection.execute(
                        "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
                    ).fetchone()
                    if row is not None and row[0] != checksum:
                        raise RuntimeError(
                            f"Le checksum de la migration scientifique v{version} est inattendu."
                        )
                    if row is None:
                        connection.executescript(sql)
                        connection.execute(
                            "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                            (version, name, checksum, _utc_now()),
                        )
            connection.close()
            self._initialized = True

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            papers = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        return {
            "status": "ok",
            "local_first": True,
            "schema_version": int(version or 0),
            "papers": int(papers),
            "data_dir": str(self.data_dir),
            "capabilities": {
                "library": True,
                "page_provenance": True,
                "persistent_jobs": True,
                "persistent_trace": True,
                "machine_learning_profile": True,
                "network_required": False,
            },
        }

    def record_feedback(
        self,
        *,
        session_id: str,
        backend: str,
        model: str = "",
        rating: int,
        prompt_hash: str = "",
        response_hash: str = "",
    ) -> dict[str, Any]:
        """Store one bounded local preference signal, never raw chat text."""

        session_id = str(session_id).strip()
        backend = str(backend).strip()
        model = str(model).strip()
        prompt_hash = str(prompt_hash).strip().lower()
        response_hash = str(response_hash).strip().lower()
        if not session_id or len(session_id) > 128:
            raise ValueError("Référence de session invalide.")
        if backend not in _LOCAL_FEEDBACK_BACKENDS:
            raise ValueError("Le feedback est réservé aux modèles locaux.")
        if len(model) > 256:
            raise ValueError("Identifiant de modèle trop long.")
        if isinstance(rating, bool) or rating not in {-1, 1}:
            raise ValueError("Le vote doit être +1 ou -1.")
        for name, value in (("prompt_hash", prompt_hash), ("response_hash", response_hash)):
            if value and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
                raise ValueError(f"{name} doit être un hash SHA-256.")

        event = {
            "id": _uuid(),
            "session_id": session_id,
            "backend": backend,
            "model": model,
            "rating": int(rating),
            "prompt_hash": prompt_hash,
            "response_hash": response_hash,
            "created_at": _utc_now(),
        }
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO feedback_events(id,session_id,backend,model,rating,prompt_hash,response_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                tuple(event[key] for key in (
                    "id", "session_id", "backend", "model", "rating",
                    "prompt_hash", "response_hash", "created_at",
                )),
            )
            old_rows = connection.execute(
                "SELECT id FROM feedback_events ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
                (_MAX_FEEDBACK_EVENTS,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM feedback_events WHERE id=?",
                ((row["id"],) for row in old_rows),
            )
        return event

    def save_conversation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert a complete conversation in the local notebook database."""

        conversation_id = str(payload.get("id", "")).strip()
        title = " ".join(str(payload.get("title", "Discussion")).split()).strip()
        if not conversation_id or len(conversation_id) > 128:
            raise ValueError("Identifiant de conversation invalide.")
        if not title:
            title = "Discussion"
        if len(title) > 200:
            title = title[:200].rstrip()

        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list) or len(raw_messages) > _MAX_SAVED_CONVERSATION_MESSAGES:
            raise ValueError("Conversation trop longue.")
        messages: list[dict[str, Any]] = []
        total_chars = 0
        for raw in raw_messages:
            if not isinstance(raw, Mapping):
                continue
            role = str(raw.get("role", "")).strip().lower()
            text = str(raw.get("text", ""))
            if role not in {"user", "assistant"} or not text.strip():
                continue
            total_chars += len(text)
            if total_chars > _MAX_SAVED_CONVERSATION_CHARS:
                raise ValueError("Conversation trop volumineuse.")
            message: dict[str, Any] = {"role": role, "text": text}
            if raw.get("jobId"):
                message["jobId"] = str(raw["jobId"])[:128]
            if raw.get("backend"):
                message["backend"] = str(raw["backend"])[:64]
            if raw.get("model"):
                message["model"] = str(raw["model"])[:256]
            raw_trace = raw.get("trace", [])
            if isinstance(raw_trace, list):
                trace: list[dict[str, Any]] = []
                for event in raw_trace[:200]:
                    if not isinstance(event, Mapping):
                        continue
                    trace.append({
                        "type": str(event.get("type", "decision"))[:32],
                        "text": str(event.get("text", ""))[:1000],
                        "details": event.get("details", {}) if isinstance(event.get("details", {}), Mapping) else {},
                        "at": str(event.get("at", ""))[:64],
                    })
                message["trace"] = trace
            messages.append(message)

        raw_conversation = payload.get("conversation", [])
        if not isinstance(raw_conversation, list) or len(raw_conversation) > _MAX_SAVED_CONVERSATION_MESSAGES:
            raise ValueError("Contexte de conversation invalide.")
        turns: list[dict[str, str]] = []
        turn_chars = 0
        for raw in raw_conversation:
            if not isinstance(raw, Mapping):
                continue
            role = str(raw.get("role", "")).strip().lower()
            text = str(raw.get("text", raw.get("content", "")))
            if role not in {"user", "assistant"} or not text.strip():
                continue
            turn_chars += len(text)
            if turn_chars > _MAX_SAVED_CONVERSATION_CHARS:
                raise ValueError("Contexte de conversation trop volumineux.")
            turns.append({"role": role, "text": text})

        now = _utc_now()
        saved_at = str(payload.get("saved_at", payload.get("savedAt", now))).strip()[:64] or now
        compact_summary = str(payload.get("compact_summary", payload.get("compactSummary", "")))[:20_000]
        compact_mode = str(payload.get("compact_mode", payload.get("compactMode", "")))[:32]
        compacted_at = str(payload.get("compacted_at", payload.get("compactedAt", ""))).strip()[:64] or None
        compacted = bool(payload.get("compacted", False))
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO conversations(id,title,saved_at,updated_at,compacted,compact_summary,compact_mode,compacted_at,message_count,messages_json,conversation_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at,"
                "compacted=excluded.compacted,compact_summary=excluded.compact_summary,compact_mode=excluded.compact_mode,"
                "compacted_at=excluded.compacted_at,message_count=excluded.message_count,messages_json=excluded.messages_json,"
                "conversation_json=excluded.conversation_json",
                (
                    conversation_id, title, saved_at, now, int(compacted), compact_summary,
                    compact_mode, compacted_at, len(messages), _json(messages), _json(turns),
                ),
            )
        return {
            "id": conversation_id,
            "title": title,
            "saved_at": saved_at,
            "updated_at": now,
            "compacted": compacted,
            "compact_summary": compact_summary,
            "compact_mode": compact_mode,
            "compacted_at": compacted_at,
            "message_count": len(messages),
            "messages": messages,
            "conversation": turns,
        }

    def list_conversations(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id,title,saved_at,updated_at,compacted,compact_mode,compacted_at,message_count "
                "FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                **dict(row),
                "compacted": bool(row["compacted"]),
            }
            for row in rows
        ]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (str(conversation_id).strip(),)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["compacted"] = bool(value["compacted"])
        value["messages"] = json.loads(value.pop("messages_json") or "[]")
        value["conversation"] = json.loads(value.pop("conversation_json") or "[]")
        return value

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id=?", (str(conversation_id).strip(),)
            )
        return cursor.rowcount > 0

    def put_blob(self, data: bytes, *, media_type: str) -> tuple[str, int]:
        digest = hashlib.sha256(data).hexdigest()
        destination = self.blob_dir / digest[:2] / digest[2:4] / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            handle, temporary = tempfile.mkstemp(prefix=".blob-", dir=destination.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != digest:
                    raise OSError("Le hash du blob écrit ne correspond pas à la source.")
                os.replace(temporary, destination)
            finally:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO blobs(hash,size,media_type,created_at) VALUES(?,?,?,?)",
                (digest, len(data), media_type, now),
            )
        return digest, len(data)

    def create_job(self, job_type: str, params: dict[str, Any] | None = None) -> str:
        job_id = _uuid()
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO jobs(id,job_type,status,params_json,created_at) VALUES(?,?,?,?,?)",
                (job_id, job_type, "queued", _json(params or {}), now),
            )
        self.append_job_event(job_id, "queued", "Opération ajoutée à la file locale.")
        return job_id

    def update_job(
        self,
        job_id: str,
        status: str,
        *,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        now = _utc_now()
        fields = ["status=?", "error=?"]
        values: list[Any] = [status, error]
        if progress is not None:
            fields.append("progress=?")
            values.append(max(0.0, min(1.0, float(progress))))
        if result is not None:
            fields.append("result_json=?")
            values.append(_json(result))
        if status == "running":
            fields.extend(["started_at=COALESCE(started_at, ?)", "attempts=attempts+1"])
            values.append(now)
        if status in {"succeeded", "failed", "cancelled", "interrupted"}:
            fields.append("finished_at=?")
            values.append(now)
        values.append(job_id)
        with self._connection() as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)

    def append_job_event(
        self,
        job_id: str,
        event_type: str,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM job_events WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO job_events(job_id,sequence,event_type,summary,details_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, sequence, event_type, summary, _json(details or {}), _utc_now()),
            )
        return sequence

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["params"] = json.loads(value.pop("params_json"))
        value["result"] = json.loads(value.pop("result_json"))
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence,event_type,summary,details_json,created_at "
                "FROM job_events WHERE job_id=? ORDER BY sequence", (job_id,)
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "type": row["event_type"],
                "summary": row["summary"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def import_document(self, name: str, data: bytes, *, job_id: str | None = None) -> dict[str, Any]:
        media_type = _media_type(name)
        job_id = job_id or self.create_job("document_import", {"name": name, "bytes": len(data)})
        self.update_job(job_id, "running", progress=0.05)
        self.append_job_event(job_id, "blob_started", "Calcul du hash et stockage du document.")
        try:
            blob_hash, blob_size = self.put_blob(data, media_type=media_type)
            self.update_job(job_id, "running", progress=0.2)
            self.append_job_event(
                job_id, "blob_stored", "Document stocké par hash SHA-256.",
                {"sha256": blob_hash, "bytes": blob_size},
            )
            with self._connection() as connection:
                duplicate = connection.execute(
                    "SELECT p.id paper_id,p.title,v.id version_id,a.original_name "
                    "FROM document_versions v JOIN artifacts a ON a.id=v.artifact_id "
                    "JOIN papers p ON p.id=a.paper_id WHERE v.blob_hash=? LIMIT 1",
                    (blob_hash,),
                ).fetchone()
            if duplicate is not None:
                result = self._import_result(duplicate["paper_id"], duplicate["version_id"], duplicate=True)
                self.update_job(job_id, "succeeded", progress=1.0, result=result)
                self.append_job_event(job_id, "completed", "Document identique déjà présent ; blob réutilisé.")
                return {**result, "job_id": job_id}

            self.append_job_event(job_id, "extraction_started", "Extraction paginée du document.")
            extracted = extract_pages(name, data)
            self.update_job(job_id, "running", progress=0.45)
            now = _utc_now()
            paper_id, source_id, artifact_id, version_id, transform_id = (
                _uuid(), _uuid(), _uuid(), _uuid(), _uuid()
            )
            title = Path(name).stem.strip() or name
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO sources(id,provider,canonical_uri,accessed_at,payload_hash) VALUES(?,?,?,?,?)",
                    (source_id, "local_upload", name, now, blob_hash),
                )
                connection.execute(
                    "INSERT INTO papers(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                    (paper_id, title, now, now),
                )
                connection.execute(
                    "INSERT INTO artifacts(id,paper_id,kind,original_name,blob_hash,media_type,source_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (artifact_id, paper_id, "document", name, blob_hash, media_type, source_id, now),
                )
                connection.execute(
                    "INSERT INTO document_versions(id,artifact_id,version_label,blob_hash,acquired_at,source_id,extraction_status) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (version_id, artifact_id, "v1", blob_hash, now, source_id, "complete"),
                )
                connection.execute(
                    "INSERT INTO transforms(id,transform_type,version,parameters_json,engine,created_at) VALUES(?,?,?,?,?,?)",
                    (transform_id, "page_text_extraction", "1", "{}", "3loop.documents", now),
                )
                connection.execute(
                    "INSERT INTO transform_inputs(transform_id,input_type,input_id,input_hash) VALUES(?,?,?,?)",
                    (transform_id, "document_version", version_id, blob_hash),
                )
                connection.execute(
                    "INSERT INTO scientific_fts(entity_type,entity_id,title,body) VALUES(?,?,?,?)",
                    ("paper", paper_id, title, ""),
                )
                for page in extracted:
                    self._insert_page(connection, version_id, transform_id, page)
            result = self._import_result(paper_id, version_id, duplicate=False)
            self.update_job(job_id, "succeeded", progress=1.0, result=result)
            self.append_job_event(
                job_id, "completed", "Document importé avec provenance par page.",
                {"paper_id": paper_id, "version_id": version_id, "pages": len(extracted)},
            )
            return {**result, "job_id": job_id}
        except Exception as exc:
            self.update_job(job_id, "failed", error=str(exc))
            self.append_job_event(job_id, "error", "Échec de l’import documentaire.", {"error": str(exc)})
            raise

    def _insert_page(
        self,
        connection: sqlite3.Connection,
        version_id: str,
        transform_id: str,
        page: ExtractedPage,
    ) -> None:
        page_id = _uuid()
        text_hash = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO pages(id,version_id,physical_index,logical_label,text,text_hash,method,extraction_error,transform_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                page_id, version_id, page.index, page.label, page.text, text_hash,
                page.method, page.error, transform_id,
            ),
        )
        for ordinal, (start, end, value) in enumerate(_chunks(page.text)):
            chunk_id = _uuid()
            connection.execute(
                "INSERT INTO chunks(id,page_id,ordinal,start_offset,end_offset,text,text_hash,method,transform_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    chunk_id, page_id, ordinal, start, end, value,
                    hashlib.sha256(value.encode("utf-8")).hexdigest(), page.method, transform_id,
                ),
            )
            connection.execute(
                "INSERT INTO scientific_fts(entity_type,entity_id,title,body) VALUES(?,?,?,?)",
                ("chunk", chunk_id, f"Page {page.label}", value),
            )

    def _import_result(self, paper_id: str, version_id: str, *, duplicate: bool) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT p.title,a.original_name,v.blob_hash,COUNT(pg.id) page_count "
                "FROM papers p JOIN artifacts a ON a.paper_id=p.id "
                "JOIN document_versions v ON v.artifact_id=a.id "
                "LEFT JOIN pages pg ON pg.version_id=v.id "
                "WHERE p.id=? AND v.id=? GROUP BY p.id,a.id,v.id",
                (paper_id, version_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Le document importé est introuvable après transaction.")
        return {
            "paper_id": paper_id,
            "version_id": version_id,
            "title": row["title"],
            "name": row["original_name"],
            "sha256": row["blob_hash"],
            "page_count": int(row["page_count"]),
            "duplicate": duplicate,
        }

    def list_papers(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT p.id,p.title,p.status,p.created_at,p.updated_at,a.original_name,a.media_type,"
                "v.id version_id,v.blob_hash,COUNT(DISTINCT pg.id) page_count,"
                "COALESCE(SUM(LENGTH(pg.text)),0) text_chars "
                "FROM papers p LEFT JOIN artifacts a ON a.paper_id=p.id "
                "LEFT JOIN document_versions v ON v.artifact_id=a.id "
                "LEFT JOIN pages pg ON pg.version_id=v.id "
                "GROUP BY p.id,a.id,v.id ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            paper = connection.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if paper is None:
                return None
            versions = connection.execute(
                "SELECT v.*,a.original_name,a.media_type FROM document_versions v "
                "JOIN artifacts a ON a.id=v.artifact_id WHERE a.paper_id=? ORDER BY v.acquired_at DESC",
                (paper_id,),
            ).fetchall()
        value = dict(paper)
        value["versions"] = [dict(row) for row in versions]
        return value

    def delete_paper(self, paper_id: str) -> dict[str, Any] | None:
        """Remove one library paper and its index relations atomically.

        The content-addressed blob files are intentionally retained: deleting a
        library record must not silently destroy a user's uploaded document.
        Notes linked to the paper are retained but their association is cleared.
        """

        paper_id = str(paper_id).strip()
        if not paper_id or len(paper_id) > 128:
            raise ValueError("Identifiant de publication invalide.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            paper = connection.execute(
                "SELECT id,title FROM papers WHERE id=?", (paper_id,)
            ).fetchone()
            if paper is None:
                return None
            artifact_ids = tuple(
                row["id"] for row in connection.execute(
                    "SELECT id FROM artifacts WHERE paper_id=?", (paper_id,)
                ).fetchall()
            )
            version_ids = tuple(
                row["id"] for row in connection.execute(
                    "SELECT v.id FROM document_versions v JOIN artifacts a ON a.id=v.artifact_id "
                    "WHERE a.paper_id=?", (paper_id,)
                ).fetchall()
            )
            page_ids = tuple(
                row["id"] for row in connection.execute(
                    "SELECT pg.id FROM pages pg JOIN document_versions v ON v.id=pg.version_id "
                    "JOIN artifacts a ON a.id=v.artifact_id WHERE a.paper_id=?", (paper_id,)
                ).fetchall()
            )
            chunk_ids = tuple(
                row["id"] for row in connection.execute(
                    "SELECT c.id FROM chunks c JOIN pages pg ON pg.id=c.page_id "
                    "JOIN document_versions v ON v.id=pg.version_id "
                    "JOIN artifacts a ON a.id=v.artifact_id WHERE a.paper_id=?", (paper_id,)
                ).fetchall()
            )
            if version_ids or page_ids or chunk_ids:
                refs = version_ids + page_ids + chunk_ids
                placeholders = ",".join("?" for _ in refs)
                connection.execute(
                    f"DELETE FROM citations WHERE version_id IN ({placeholders}) "
                    f"OR page_id IN ({placeholders}) OR chunk_id IN ({placeholders})",
                    refs * 3,
                )
            connection.execute(
                "DELETE FROM scientific_fts WHERE entity_type='paper' AND entity_id=?",
                (paper_id,),
            )
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                connection.execute(
                    f"DELETE FROM scientific_fts WHERE entity_type='chunk' AND entity_id IN ({placeholders})",
                    chunk_ids,
                )
            connection.execute(
                "DELETE FROM collection_items WHERE entity_type='paper' AND entity_id=?",
                (paper_id,),
            )
            connection.execute(
                "DELETE FROM entity_tags WHERE entity_type='paper' AND entity_id=?",
                (paper_id,),
            )
            connection.execute(
                "UPDATE notes SET entity_type=NULL,entity_id=NULL WHERE entity_type='paper' AND entity_id=?",
                (paper_id,),
            )
            related_ids = (paper_id,) + artifact_ids + version_ids + page_ids + chunk_ids
            placeholders = ",".join("?" for _ in related_ids)
            connection.execute(
                f"DELETE FROM provenance_edges WHERE subject_id IN ({placeholders}) "
                f"OR object_id IN ({placeholders})",
                related_ids * 2,
            )
            connection.execute("DELETE FROM papers WHERE id=?", (paper_id,))
            connection.execute(
                "INSERT OR REPLACE INTO tombstones(entity_type,entity_id,summary,deleted_at,reason) "
                "VALUES(?,?,?,?,?)",
                ("paper", paper_id, str(paper["title"])[:500], _utc_now(), "user_deleted"),
            )
        return {"id": paper_id, "title": paper["title"], "deleted": True}

    def pages(self, version_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id,version_id,physical_index,logical_label,text_hash,method,extraction_error,LENGTH(text) text_chars "
                "FROM pages WHERE version_id=? ORDER BY physical_index", (version_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def chunks(self, page_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id,page_id,ordinal,start_offset,end_offset,text,text_hash,method "
                "FROM chunks WHERE page_id=? ORDER BY ordinal", (page_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_bibliography_entries(
        self,
        entries: Iterable[BibliographicEntry],
        *,
        source_name: str = "bibliography_import",
    ) -> list[dict[str, Any]]:
        """Insert references idempotently and return an audit per entry."""

        results: list[dict[str, Any]] = []
        now = _utc_now()
        for entry in entries:
            title = " ".join(entry.title.split())
            if not title and not entry.doi:
                results.append({"cite_key": entry.cite_key, "status": "rejected", "reason": "titre ou DOI manquant"})
                continue
            doi = _normalize_identifier(entry.doi)
            with self._connection() as connection:
                existing = None
                if doi:
                    existing = connection.execute(
                        "SELECT paper_id FROM identifiers WHERE scheme='doi' AND value_normalized=?",
                        (doi,),
                    ).fetchone()
                if existing is None:
                    for scheme, value in entry.external_ids.items():
                        normalized_value = _normalize_identifier(value)
                        if not normalized_value:
                            continue
                        existing = connection.execute(
                            "SELECT paper_id FROM identifiers WHERE scheme=? AND value_normalized=? LIMIT 1",
                            (str(scheme).lower(), normalized_value),
                        ).fetchone()
                        if existing is not None:
                            break
                if existing is None and title:
                    existing = connection.execute(
                        "SELECT id AS paper_id FROM papers WHERE lower(title)=lower(?) LIMIT 1", (title,)
                    ).fetchone()
                source_id = _uuid()
                payload_hash = hashlib.sha256(_json(entry.as_dict()).encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO sources(id,provider,canonical_uri,accessed_at,payload_hash) VALUES(?,?,?,?,?)",
                    (source_id, source_name, entry.url, now, payload_hash),
                )
                if existing is None:
                    paper_id = _uuid()
                    connection.execute(
                        "INSERT INTO papers(id,title,abstract,year,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (paper_id, title or entry.doi, entry.abstract, entry.year, now, now),
                    )
                    status = "created"
                else:
                    paper_id = str(existing["paper_id"])
                    connection.execute(
                        "UPDATE papers SET abstract=CASE WHEN abstract='' THEN ? ELSE abstract END, "
                        "year=COALESCE(year,?), updated_at=? WHERE id=?",
                        (entry.abstract, entry.year, now, paper_id),
                    )
                    status = "matched"
                identifiers = []
                if doi:
                    identifiers.append(("doi", doi))
                identifiers.extend(
                    (str(scheme).lower(), _normalize_identifier(value))
                    for scheme, value in entry.external_ids.items()
                    if value
                )
                for scheme, value in identifiers:
                    connection.execute(
                        "INSERT OR IGNORE INTO identifiers(id,paper_id,scheme,value_normalized,source_id) VALUES(?,?,?,?,?)",
                        (_uuid(), paper_id, scheme, value, source_id),
                    )
                for position, author_name in enumerate(entry.authors):
                    author_name = " ".join(author_name.split())
                    if not author_name:
                        continue
                    author = connection.execute(
                        "SELECT id FROM authors WHERE lower(display_name)=lower(?) LIMIT 1", (author_name,)
                    ).fetchone()
                    author_id = str(author["id"]) if author else _uuid()
                    if author is None:
                        connection.execute(
                            "INSERT INTO authors(id,display_name) VALUES(?,?)", (author_id, author_name)
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO paper_authors(paper_id,author_id,position) VALUES(?,?,?)",
                        (paper_id, author_id, position),
                    )
                if entry.journal:
                    venue = connection.execute(
                        "SELECT id FROM venues WHERE lower(name)=lower(?) LIMIT 1", (entry.journal,)
                    ).fetchone()
                    if venue is None:
                        connection.execute(
                            "INSERT INTO venues(id,name) VALUES(?,?)", (_uuid(), entry.journal)
                        )
                connection.execute("DELETE FROM scientific_fts WHERE entity_type='paper' AND entity_id=?", (paper_id,))
                connection.execute(
                    "INSERT INTO scientific_fts(entity_type,entity_id,title,body) VALUES(?,?,?,?)",
                    ("paper", paper_id, title, entry.abstract),
                )
            results.append({"cite_key": entry.cite_key, "paper_id": paper_id, "status": status, "doi": doi})
        return results

    def bibliography_entries(self, *, paper_ids: Iterable[str] | None = None) -> list[BibliographicEntry]:
        """Read normalized papers for loss-aware bibliography export."""

        selected = tuple(str(value) for value in (paper_ids or ()))
        with self._connection() as connection:
            if selected:
                placeholders = ",".join("?" for _ in selected)
                papers = connection.execute(
                    f"SELECT * FROM papers WHERE id IN ({placeholders}) ORDER BY title", selected
                ).fetchall()
            else:
                papers = connection.execute("SELECT * FROM papers ORDER BY title").fetchall()
            values: list[BibliographicEntry] = []
            for paper in papers:
                authors = connection.execute(
                    "SELECT a.display_name FROM authors a JOIN paper_authors pa ON pa.author_id=a.id "
                    "WHERE pa.paper_id=? ORDER BY pa.position", (paper["id"],)
                ).fetchall()
                identifiers = connection.execute(
                    "SELECT scheme,value_normalized FROM identifiers WHERE paper_id=?", (paper["id"],)
                ).fetchall()
                by_scheme = {row["scheme"]: row["value_normalized"] for row in identifiers}
                doi = by_scheme.pop("doi", "")
                values.append(
                    BibliographicEntry(
                        cite_key=_bibliography_key(paper["title"], paper["year"], paper["id"]),
                        title=paper["title"],
                        authors=tuple(row["display_name"] for row in authors),
                        year=paper["year"],
                        abstract=paper["abstract"],
                        doi=doi,
                        external_ids=by_scheme,
                    )
                )
        return values

    def create_collection(self, name: str, *, description: str = "", parent_id: str | None = None) -> dict[str, Any]:
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("Le nom de collection est obligatoire.")
        collection = {"id": _uuid(), "name": name, "description": description.strip(), "parent_id": parent_id}
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO collections(id,name,description,parent_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (collection["id"], name, collection["description"], parent_id, now, now),
            )
        return {**collection, "created_at": now, "updated_at": now, "item_count": 0}

    def list_collections(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT c.*,COUNT(ci.entity_id) item_count FROM collections c "
                "LEFT JOIN collection_items ci ON ci.collection_id=c.id GROUP BY c.id ORDER BY c.name"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_collection_items(self, collection_id: str, items: Iterable[Mapping[str, Any]]) -> int:
        now = _utc_now()
        count = 0
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM collections WHERE id=?", (collection_id,)).fetchone() is None:
                raise ValueError("Collection introuvable.")
            for item in items:
                entity_type = str(item.get("entity_type", "paper")).strip()
                entity_id = str(item.get("entity_id", "")).strip()
                if not entity_id or entity_type not in {"paper", "source", "dataset", "note"}:
                    continue
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO collection_items(collection_id,entity_type,entity_id,added_at) VALUES(?,?,?,?)",
                    (collection_id, entity_type, entity_id, now),
                )
                count += cursor.rowcount
        return count

    def create_note(
        self,
        body: str,
        *,
        title: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        if not body.strip():
            raise ValueError("Le contenu de la note est obligatoire.")
        now = _utc_now()
        note = {
            "id": _uuid(), "title": title.strip(), "body": body.strip(),
            "entity_type": entity_type, "entity_id": entity_id,
            "created_at": now, "updated_at": now,
        }
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO notes(id,title,body,entity_type,entity_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                tuple(note[key] for key in ("id", "title", "body", "entity_type", "entity_id", "created_at", "updated_at")),
            )
            connection.execute(
                "INSERT INTO scientific_fts(entity_type,entity_id,title,body) VALUES(?,?,?,?)",
                ("note", note["id"], note["title"], note["body"]),
            )
        return note

    def list_notes(self, *, entity_type: str | None = None, entity_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if entity_type and entity_id:
                rows = connection.execute(
                    "SELECT * FROM notes WHERE entity_type=? AND entity_id=? ORDER BY updated_at DESC",
                    (entity_type, entity_id),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM notes ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_note(self, note_id: str) -> dict[str, Any] | None:
        """Delete one notebook note while retaining independent annotations."""

        note_id = str(note_id).strip()
        if not note_id or len(note_id) > 128:
            raise ValueError("Identifiant de note invalide.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            note = connection.execute(
                "SELECT id,title FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if note is None:
                return None
            connection.execute(
                "DELETE FROM scientific_fts WHERE entity_type='note' AND entity_id=?",
                (note_id,),
            )
            connection.execute(
                "DELETE FROM collection_items WHERE entity_type='note' AND entity_id=?",
                (note_id,),
            )
            connection.execute(
                "DELETE FROM entity_tags WHERE entity_type='note' AND entity_id=?",
                (note_id,),
            )
            connection.execute(
                "DELETE FROM provenance_edges WHERE "
                "(subject_type='note' AND subject_id=?) OR "
                "(object_type='note' AND object_id=?)",
                (note_id, note_id),
            )
            # annotations.note_id uses ON DELETE SET NULL, so annotations
            # remain available instead of being silently destroyed with a note.
            connection.execute("DELETE FROM notes WHERE id=?", (note_id,))
            connection.execute(
                "INSERT OR REPLACE INTO tombstones(entity_type,entity_id,summary,deleted_at,reason) "
                "VALUES(?,?,?,?,?)",
                ("note", note_id, str(note["title"])[:500], _utc_now(), "user_deleted"),
            )
        return {"id": note_id, "title": note["title"], "deleted": True}

    def create_annotation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        selected_text = str(payload.get("selected_text", "")).strip()
        version_id = str(payload.get("version_id", "")).strip() or None
        page_id = str(payload.get("page_id", "")).strip() or None
        if not selected_text or not version_id:
            raise ValueError("Une annotation doit conserver un texte sélectionné et une version de document.")
        value = {
            "id": _uuid(), "note_id": payload.get("note_id"), "version_id": version_id,
            "page_id": page_id, "chunk_id": str(payload.get("chunk_id", "")).strip() or None,
            "start_offset": payload.get("start_offset"), "end_offset": payload.get("end_offset"),
            "selected_text": selected_text, "color": str(payload.get("color", "yellow")),
            "created_at": _utc_now(),
        }
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM document_versions WHERE id=?", (version_id,)).fetchone() is None:
                raise ValueError("Version de document introuvable.")
            connection.execute(
                "INSERT INTO annotations(id,note_id,version_id,page_id,chunk_id,start_offset,end_offset,selected_text,color,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(value[key] for key in ("id", "note_id", "version_id", "page_id", "chunk_id", "start_offset", "end_offset", "selected_text", "color", "created_at")),
            )
        return value

    def list_annotations(self, *, version_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if version_id:
                rows = connection.execute(
                    "SELECT * FROM annotations WHERE version_id=? ORDER BY created_at", (version_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM annotations ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def save_search_run(
        self,
        question: str,
        profile: str,
        plan: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> str:
        """Persist a federated search plan/result for reproducible reviews."""

        run_id = _uuid()
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO search_runs(id,question,profile,plan_json,result_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, question, profile, _json(plan), _json(result), now, now),
            )
            for rank, item in enumerate(result.get("results", []) if isinstance(result, Mapping) else [], start=1):
                if not isinstance(item, Mapping):
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO search_run_sources(run_id,provider,external_id,rank) VALUES(?,?,?,?)",
                    (run_id, str(item.get("provider", "")), str(item.get("external_id", "")), rank),
                )
        return run_id

    def create_review(
        self,
        title: str,
        *,
        question: str = "",
        profile: str = "scientific",
        criteria: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        title = " ".join(title.split())
        if not title:
            raise ValueError("Le titre de la revue est obligatoire.")
        now = _utc_now()
        value = {
            "id": _uuid(), "title": title, "question": question.strip(),
            "profile": profile.strip() or "scientific", "criteria": dict(criteria or {}),
            "status": "draft", "created_at": now, "updated_at": now,
        }
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO reviews(id,title,question,profile,criteria_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (value["id"], value["title"], value["question"], value["profile"], _json(value["criteria"]), value["status"], now, now),
            )
        return value

    def add_review_items(self, review_id: str, paper_ids: Iterable[str]) -> int:
        count = 0
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM reviews WHERE id=?", (review_id,)).fetchone() is None:
                raise ValueError("Revue introuvable.")
            for paper_id in paper_ids:
                paper_id = str(paper_id).strip()
                if not paper_id or connection.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone() is None:
                    continue
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO review_items(review_id,paper_id) VALUES(?,?)", (review_id, paper_id)
                )
                count += cursor.rowcount
        return count

    def update_review_item(self, review_id: str, paper_id: str, *, decision: str, reason: str = "", notes: str = "") -> dict[str, Any]:
        if decision not in {"pending", "include", "exclude", "maybe"}:
            raise ValueError("Décision de screening invalide.")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO review_items(review_id,paper_id,decision,reason,notes) VALUES(?,?,?,?,?) "
                "ON CONFLICT(review_id,paper_id) DO UPDATE SET decision=excluded.decision,reason=excluded.reason,notes=excluded.notes",
                (review_id, paper_id, decision, reason.strip(), notes.strip()),
            )
            connection.execute(
                "UPDATE reviews SET updated_at=?,status=CASE WHEN ?='include' THEN 'screening' ELSE status END WHERE id=?",
                (_utc_now(), decision, review_id),
            )
        return {"review_id": review_id, "paper_id": paper_id, "decision": decision, "reason": reason.strip(), "notes": notes.strip()}

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            review = connection.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if review is None:
                return None
            items = connection.execute(
                "SELECT ri.*,p.title,p.abstract,p.year FROM review_items ri JOIN papers p ON p.id=ri.paper_id "
                "WHERE ri.review_id=? ORDER BY p.title", (review_id,)
            ).fetchall()
        value = dict(review)
        value["criteria"] = json.loads(value.pop("criteria_json"))
        value["items"] = [dict(item) for item in items]
        return value

    def compare_papers(self, paper_ids: Iterable[str]) -> dict[str, Any]:
        """Build a conservative comparison matrix with explicit unknowns."""

        ids = tuple(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        papers: list[dict[str, Any]] = []
        with self._connection() as connection:
            for paper_id in ids:
                paper = connection.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if paper is None:
                    continue
                authors = connection.execute(
                    "SELECT a.display_name FROM authors a JOIN paper_authors pa ON pa.author_id=a.id "
                    "WHERE pa.paper_id=? ORDER BY pa.position", (paper_id,)
                ).fetchall()
                identifiers = connection.execute(
                    "SELECT scheme,value_normalized FROM identifiers WHERE paper_id=?", (paper_id,)
                ).fetchall()
                papers.append(
                    {
                        "paper_id": paper_id,
                        "title": paper["title"],
                        "year": paper["year"],
                        "abstract": paper["abstract"],
                        "authors": [row["display_name"] for row in authors],
                        "identifiers": {row["scheme"]: row["value_normalized"] for row in identifiers},
                    }
                )
        dimensions = ("question", "method", "dataset", "benchmark", "metric", "baseline", "ablation", "hardware", "license", "reproducibility")
        return {
            "paper_ids": [paper["paper_id"] for paper in papers],
            "papers": papers,
            "dimensions": list(dimensions),
            "matrix": [
                {"paper_id": paper["paper_id"], **{dimension: None for dimension in dimensions}}
                for paper in papers
            ],
            "unknown_policy": "Les dimensions absentes sont null plutôt qu’inventées.",
        }

    def create_dataset(self, name: str, data: bytes, *, filename: str = "dataset.csv", description: str = "") -> dict[str, Any]:
        if not name.strip() or not data:
            raise ValueError("Nom et contenu du dataset obligatoires.")
        profile = profile_csv(data, filename=filename)
        blob_hash, size = self.put_blob(data, media_type="text/csv")
        now = _utc_now()
        dataset_id = _uuid()
        version_id = _uuid()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO datasets(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)",
                (dataset_id, name.strip(), description.strip(), now, now),
            )
            connection.execute(
                "INSERT INTO dataset_versions(id,dataset_id,blob_hash,schema_json,row_count,acquired_at) VALUES(?,?,?,?,?,?)",
                (version_id, dataset_id, blob_hash, _json(profile.as_dict()), profile.row_count, now),
            )
        return {
            "dataset_id": dataset_id, "version_id": version_id, "name": name.strip(),
            "filename": filename, "sha256": blob_hash, "bytes": size, "profile": profile.as_dict(),
        }

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT d.id dataset_id,d.name,d.description,d.created_at,d.updated_at,"
                "v.id version_id,v.blob_hash,v.schema_json,v.row_count,v.acquired_at "
                "FROM datasets d LEFT JOIN dataset_versions v ON v.dataset_id=d.id "
                "ORDER BY d.updated_at DESC,v.acquired_at DESC"
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["profile"] = json.loads(value.pop("schema_json")) if value.get("schema_json") else {}
            values.append(value)
        return values

    def run_analysis(self, version_id: str, name: str, recipe: Mapping[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            version = connection.execute(
                "SELECT v.*,d.name dataset_name FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE v.id=?",
                (version_id,),
            ).fetchone()
        if version is None:
            raise ValueError("Version de dataset introuvable.")
        blob_path = self.blob_dir / version["blob_hash"][:2] / version["blob_hash"][2:4] / version["blob_hash"]
        try:
            data = blob_path.read_bytes()
        except OSError as exc:
            raise ValueError("Blob du dataset introuvable ou illisible.") from exc
        analysis = execute_recipe(data, recipe, filename=version["dataset_name"])
        now = _utc_now()
        run_id = _uuid()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO analysis_runs(id,dataset_version_id,name,recipe_json,result_json,status,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, version_id, name.strip() or "Analyse", _json(recipe), _json(analysis.as_dict()), "succeeded", now, now),
            )
        return {"run_id": run_id, "dataset_version_id": version_id, "name": name.strip() or "Analyse", **analysis.as_dict()}

    def document_context(
        self,
        version_ids: Iterable[str],
        question: str,
        *,
        max_tokens: int = 600,
    ) -> dict[str, Any]:
        """Select relevant local document excerpts within one shared budget.

        Uploaded files stay complete in SQLite; only this answer-time view is
        bounded.  Ranking is deliberately local and deterministic: lexical
        matches in the user's question outrank unrelated chunks, with document
        order as a stable tie-breaker.  This keeps attached documents useful
        without requiring an embedding model or an Internet connection.
        """

        try:
            token_budget = int(max_tokens)
        except (TypeError, ValueError):
            token_budget = 600
        token_budget = max(32, min(1_200, token_budget))
        char_budget = int(token_budget * 3.6)
        ids = tuple(
            dict.fromkeys(
                str(value).strip() for value in version_ids if str(value).strip()
            )
        )[:32]
        if not ids:
            return {"text": "", "excerpts": [], "version_ids": [], "max_tokens": token_budget}

        # Keep terms meaningful enough to avoid ranking every chunk by common
        # French/English glue words.  This is intentionally not a search
        # engine: it is a fast, private selection pass over already attached
        # local files.
        terms = tuple(
            dict.fromkeys(
                word.strip(".,;:!?()[]{}\\\"'`*_-/\\\\").casefold()
                for word in str(question).split()
                if len(word.strip(".,;:!?()[]{}\\\"'`*_-/\\\\")) >= 3
            )
        )
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT c.id chunk_id,c.text,p.physical_index,p.logical_label,"
                "v.id version_id,pa.id paper_id,pa.title,a.original_name "
                "FROM chunks c JOIN pages p ON p.id=c.page_id "
                "JOIN document_versions v ON v.id=p.version_id "
                "JOIN artifacts a ON a.id=v.artifact_id "
                "JOIN papers pa ON pa.id=a.paper_id "
                f"WHERE v.id IN ({placeholders}) "
                "ORDER BY p.physical_index,c.ordinal",
                ids,
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        for order, row in enumerate(rows):
            raw_text = compact_text(str(row["text"]))
            if not raw_text:
                continue
            folded = raw_text.casefold()
            score = sum(folded.count(term) for term in terms)
            # A query with no lexical match still gets a deterministic sample
            # of its attached documents, rather than an empty offline answer.
            candidates.append({
                "order": order,
                "score": score,
                "text": raw_text,
                "version_id": str(row["version_id"]),
                "paper_id": str(row["paper_id"]),
                "title": str(row["title"] or row["original_name"] or "Document local"),
                "page": str(row["logical_label"] or row["physical_index"]),
            })
        candidates.sort(key=lambda item: (-int(item["score"]), int(item["order"])))

        parts: list[str] = []
        excerpts: list[dict[str, Any]] = []
        used_chunks: set[str] = set()
        used = 0
        for candidate in candidates:
            chunk_key = f"{candidate['version_id']}:{candidate['order']}"
            if chunk_key in used_chunks:
                continue
            header = f"[Source locale : {candidate['title']} · page {candidate['page']}]\n"
            separator = "\n\n" if parts else ""
            remaining = char_budget - used - len(separator) - len(header)
            if remaining < 48:
                break
            excerpt = str(candidate["text"])
            # Centre a long excerpt around the first matching query term, so
            # a relevant sentence late in a chunk is not discarded merely
            # because the chunk begins with boilerplate.
            match_positions = [excerpt.casefold().find(term) for term in terms]
            match_positions = [position for position in match_positions if position >= 0]
            if match_positions and len(excerpt) > remaining:
                start = max(0, min(match_positions) - remaining // 3)
                prefix = "[… ]" if start else ""
                excerpt = prefix + excerpt[start : start + remaining - len(prefix)]
            if len(excerpt) > remaining:
                excerpt = excerpt[:remaining]
            if len(excerpt) > 80 and not excerpt.endswith((".", "!", "?", "…")):
                boundary = excerpt.rfind(" ", max(32, len(excerpt) - 120))
                if boundary > 32:
                    excerpt = excerpt[:boundary].rstrip() + "…"
            if not excerpt.strip():
                continue
            part = f"{header}{excerpt.strip()}"
            parts.append(part)
            used += len(separator) + len(part)
            used_chunks.add(chunk_key)
            excerpts.append({
                "version_id": candidate["version_id"],
                "paper_id": candidate["paper_id"],
                "title": candidate["title"],
                "page": candidate["page"],
                "text": excerpt.strip(),
            })

        return {
            "text": "\n\n".join(parts),
            "excerpts": excerpts,
            "version_ids": list(ids),
            "max_tokens": token_budget,
            "truncated": len(excerpts) < len(candidates),
        }

    def document_text(self, version_id: str, *, max_tokens: int = 2000) -> str:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT text FROM pages WHERE version_id=? ORDER BY physical_index", (version_id,)
            ).fetchall()
        return compact_text("\n\n".join(row["text"] for row in rows), max_tokens=max_tokens)


def _normalize_identifier(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split()).removeprefix("doi:").removeprefix("https://doi.org/").strip(".,;)")


def _bibliography_key(title: str, year: Any, fallback: str) -> str:
    words = [word for word in "".join(char if char.isalnum() else " " for char in title).split() if word]
    return ("".join(words[:3]) or "paper") + str(year or "") + fallback[:6]


def get_workspace() -> ResearchWorkspace:
    """Return the process-wide facade; SQLite connections remain per call."""

    global _workspace
    if _workspace is None:
        with _workspace_lock:
            if _workspace is None:
                _workspace = ResearchWorkspace()
    return _workspace
