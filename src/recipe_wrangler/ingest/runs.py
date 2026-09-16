"""Source imports, as runs somebody can watch.

A source import is not a request. Profiling one recipe runs weight estimation
and a nutrition chain; eight hundred of them is hours. So this is a run: it is
started, it reports where it has got to, and it survives being looked at by
somebody who arrived after it began.

State lives in Postgres rather than in the process, for the reason that makes
runs worth having at all — the interesting question about an import that has
been going for two hours is *what has it done so far*, and a dict in a worker
that a restart empties cannot answer it.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from recipe_wrangler.utils.nutrition_postgres import _get_config, get_engine

logger = logging.getLogger(__name__)


def _qualified() -> str:
    """The table, in the schema everything else in this service uses.

    `NUTRITION_SCHEMA` is configurable and every other query here qualifies
    its table with it. An unqualified name would land wherever `search_path`
    happens to point — which is `public`, so it agrees with the default and
    would have quietly disagreed with any deployment that set the schema to
    something else.
    """
    schema = _get_config().get("schema") or "public"
    return f'"{schema}"."recipe_source_import"'


TABLE = _qualified()

#: A run whose heartbeat stopped this long ago is not working, whatever its
#: status column says. Generous, because a single recipe can take a while.
STALL_AFTER_SECONDS = 900

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id              TEXT PRIMARY KEY,
    source_slug     TEXT,
    location        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    stage           TEXT,
    discovered      INTEGER NOT NULL DEFAULT 0,
    attempted       INTEGER NOT NULL DEFAULT 0,
    imported        INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    dry_run         BOOLEAN NOT NULL DEFAULT FALSE,
    error           TEXT,
    detail          JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    started_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at    TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS recipe_source_import_created_idx
    ON {TABLE} (created_at DESC);
"""

_ensured = False


def ensure_table() -> None:
    """Create the table on first use. Idempotent, and cheap after the first."""
    global _ensured
    if _ensured:
        return
    with get_engine().begin() as conn:
        for statement in _DDL.strip().split(";\n"):
            if statement.strip():
                conn.execute(text(statement))
    _ensured = True


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class RunState:
    """What a run has done, kept in the worker and flushed to Postgres."""

    id: str
    location: str
    source_slug: str | None = None
    status: str = "queued"
    stage: str | None = None
    discovered: int = 0
    attempted: int = 0
    imported: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = False
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    started_by: str | None = None


def create(state: RunState) -> None:
    ensure_table()
    with get_engine().begin() as conn:
        conn.execute(text(f"""
            INSERT INTO {TABLE} (id, source_slug, location, status, stage,
                                 dry_run, started_by, heartbeat_at)
            VALUES (:id, :source_slug, :location, :status, :stage,
                    :dry_run, :started_by, now())
        """), {
            "id": state.id, "source_slug": state.source_slug,
            "location": state.location, "status": state.status,
            "stage": state.stage, "dry_run": state.dry_run,
            "started_by": state.started_by,
        })


def save(state: RunState, *, finished: bool = False) -> None:
    """Flush progress. Never raises — reporting must not kill an import."""
    try:
        ensure_table()
        with get_engine().begin() as conn:
            conn.execute(text(f"""
                UPDATE {TABLE} SET
                    status = :status, stage = :stage, discovered = :discovered,
                    attempted = :attempted, imported = :imported,
                    failed = :failed, skipped = :skipped, error = :error,
                    detail = CAST(:detail AS JSONB), heartbeat_at = now(),
                    finished_at = CASE WHEN :finished THEN now() ELSE finished_at END
                WHERE id = :id
            """), {
                "id": state.id, "status": state.status, "stage": state.stage,
                "discovered": state.discovered, "attempted": state.attempted,
                "imported": state.imported, "failed": state.failed,
                "skipped": state.skipped, "error": state.error,
                "detail": json.dumps(state.detail, default=str),
                "finished": finished,
            })
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning("source import: progress not persisted", exc_info=True)


def _row_to_dict(row) -> dict[str, Any]:
    data = dict(row._mapping)
    for key in ("created_at", "heartbeat_at", "finished_at"):
        value = data.get(key)
        data[key] = value.isoformat() if value else None
    status = data.get("status")
    if status in ("queued", "running") and data.get("heartbeat_at"):
        beat = datetime.fromisoformat(data["heartbeat_at"])
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - beat).total_seconds()
        if age > STALL_AFTER_SECONDS:
            # Reported, not written back: the worker may still return, and one
            # reader's timeout is not a fact about the run.
            data["status"] = "stalled"
    return data


def get(run_id: str) -> dict[str, Any] | None:
    ensure_table()
    with get_engine().connect() as conn:
        row = conn.execute(
            text(f"SELECT * FROM {TABLE} WHERE id = :id"), {"id": run_id}
        ).first()
    return _row_to_dict(row) if row else None


def recent(limit: int = 20) -> list[dict[str, Any]]:
    ensure_table()
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            f"SELECT * FROM {TABLE} ORDER BY created_at DESC LIMIT :limit"
        ), {"limit": max(1, min(int(limit), 100))}).fetchall()
    return [_row_to_dict(row) for row in rows]


def active_count() -> int:
    """Runs genuinely in flight, ignoring ones whose worker went away."""
    return sum(1 for run in recent(limit=100)
               if run["status"] in ("queued", "running"))
