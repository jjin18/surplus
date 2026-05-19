"""
curation/claude_log.py : audit-log every Claude call the curation flow makes.

The brief requires: "All Claude calls: log prompt + output, make rationale
auditable." This helper writes one LLMCall row per call, capped at 16k of
prompt/output text so the DB stays reasonable.

Usage:

    with log_call(db, event_id=ev.id, attendee_id=a.id,
                  purpose="score_rationale", model=MODEL,
                  prompt=full_prompt) as call:
        text, raw_output = invoke_claude(...)
        call.output = raw_output
        # call.status auto-set to "ok" on clean exit

The context manager handles latency timing, error capture, and commit.
On exception the row lands with status="error" and the exception text in
.error.
"""
from __future__ import annotations
import time
from contextlib import contextmanager
from typing import Optional

from sqlalchemy.orm import Session

from .. import models


MAX_TEXT = 16_000


def _clip(s: str | None) -> str:
    if not s:
        return ""
    if len(s) <= MAX_TEXT:
        return s
    return s[: MAX_TEXT - len(" [truncated]")] + " [truncated]"


@contextmanager
def log_call(
    db: Session,
    *,
    purpose: str,
    model: str = "",
    prompt: str = "",
    event_id: Optional[int] = None,
    attendee_id: Optional[int] = None,
):
    """Open an audit row, yield it for the caller to fill `output`, then commit.

    The yielded LLMCall is a real ORM row : the caller mutates `.output`
    (and optionally `.status`) before the context exits. On exception the
    row is still persisted with status="error" so failed Claude calls are
    just as auditable as successful ones.
    """
    row = models.LLMCall(
        event_id=event_id,
        attendee_id=attendee_id,
        purpose=purpose,
        model=model,
        prompt=_clip(prompt),
        output="",
        status="ok",
    )
    t0 = time.time()
    try:
        yield row
    except Exception as exc:  # noqa: BLE001
        row.status = "error"
        row.error = f"{type(exc).__name__}: {exc}"
        row.latency_ms = int((time.time() - t0) * 1000)
        row.output = _clip(row.output)
        db.add(row)
        # Flush so the audit row survives even if the surrounding handler
        # rolls back. We deliberately don't commit here : the route handler
        # owns commit boundaries for its own writes.
        db.flush()
        raise
    else:
        row.output = _clip(row.output)
        row.latency_ms = int((time.time() - t0) * 1000)
        db.add(row)
        db.flush()


def log_disabled(
    db: Session,
    *,
    purpose: str,
    event_id: Optional[int] = None,
    attendee_id: Optional[int] = None,
    reason: str = "ANTHROPIC_API_KEY not set",
) -> None:
    """Log a Claude call that we *didn't* make (no API key, dry-run mode).

    Keeping these in the audit table makes it easy to tell "we never called
    Claude here" apart from "we called Claude and it failed" when reviewing
    a low-quality score later.
    """
    db.add(models.LLMCall(
        event_id=event_id, attendee_id=attendee_id,
        purpose=purpose, model="", prompt="", output="",
        status="disabled", error=reason, latency_ms=0,
    ))
    db.flush()
