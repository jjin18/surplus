"""
curation/attribution.py : Stage 5 Claude-driven outcome attribution.

Reads:
  - one Attendee (with their full enrichment)
  - that attendee's follow-up history (AttendeeFollowUp rows)
  - operator-supplied free-form notes about what happened post-event

Asks Claude: did this event drive an outcome (meeting / hire / partnership /
pipeline / revenue), with what confidence, and what evidence supports it.

Persists the result as an AttendeeAttribution row : rationale stays
auditable, every call writes an LLMCall.

The brief: "Outcome attribution via Claude: map event to outcomes
(meetings, hires, partnerships, pipeline)." Distinct from the older
agents/roi.py settlement which keys off fit_score for Prospects; this
flow reads VERIFIED follow-up data for curated Attendees.
"""
from __future__ import annotations
import json
import os
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import claude_log


_ATTRIBUTION_MODEL = os.environ.get("CURATION_ATTRIBUTION_MODEL", "claude-haiku-4-5-20251001")
_ATTRIBUTION_TIMEOUT_S = float(os.environ.get("CURATION_ATTRIBUTION_TIMEOUT", "12"))


VALID_OUTCOMES = {"meeting", "hire", "partnership", "pipeline", "revenue",
                  "other", "none"}


_ATTRIBUTION_SYSTEM = """You attribute an event's contribution to a real
post-event outcome on a specific attendee.

You receive:
  - the event profile (goal, format, city)
  - the attendee profile and enrichment
  - the operator's free-form notes about what happened after the event
  - the logged follow-up touchpoints (kind + notes + date)

Return ONLY this JSON object:

{
  "outcome": "meeting|hire|partnership|pipeline|revenue|other|none",
  "confidence": 0.0-1.0,
  "value": integer dollars (0 if not monetary or unknown),
  "rationale": "1-3 sentences explaining your decision",
  "evidence": ["string", ...]   # specific quotes / facts from the input
}

Rules:
  - `outcome: "none"` is the right answer when the inputs don't support an
    attribution claim. Don't manufacture outcomes.
  - `confidence` reflects how strongly the inputs tie the outcome to THIS
    event vs other plausible causes. Low when correlation only.
  - `value` is 0 unless the operator's notes explicitly mention a number
    OR the outcome is a hire (use the goal table's value as a hint if
    provided, otherwise 0).
  - Evidence must quote or closely paraphrase from the inputs : never invent."""


def _api_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def attribute_attendee(
    db: Session,
    attendee: models.Attendee,
    event: models.Event,
    *,
    operator_notes: str = "",
) -> models.AttendeeAttribution:
    """Run attribution for one attendee. Idempotent : if a row already exists,
    we overwrite it so the operator can re-run after adding follow-ups."""

    # Wipe any prior attribution row for this attendee+event.
    db.query(models.AttendeeAttribution).filter(
        models.AttendeeAttribution.attendee_id == attendee.id,
        models.AttendeeAttribution.event_id == event.id,
    ).delete(synchronize_session=False)

    followups = (db.query(models.AttendeeFollowUp)
                   .filter(models.AttendeeFollowUp.attendee_id == attendee.id,
                           models.AttendeeFollowUp.event_id == event.id)
                   .order_by(models.AttendeeFollowUp.created_at.asc())
                   .all())

    inputs = {
        "event": {
            "id": event.id, "goal": event.goal, "format": event.format,
            "city": event.city, "headcount": event.headcount,
        },
        "attendee": {
            "id": attendee.id, "name": attendee.name, "role": attendee.role,
            "company": attendee.company,
            "list_source": attendee.list_source,
            "enrichment": _safe_json(attendee.enrichment),
        },
        "follow_ups": [
            {
                "kind": f.kind, "notes": f.notes,
                "occurred_at": f.occurred_at.isoformat() if f.occurred_at else None,
                "logged_at": f.created_at.isoformat(),
            } for f in followups
        ],
        "operator_notes": operator_notes.strip(),
    }

    if not _api_key():
        claude_log.log_disabled(
            db, purpose="attribution",
            event_id=event.id, attendee_id=attendee.id,
        )
        # Fallback heuristic : if no signal, "none".
        return _persist_attribution(
            db, attendee, event,
            outcome="none", confidence=0.0, value=0,
            rationale="Claude unavailable (no API key); no attribution made.",
            evidence=[],
        )

    user_prompt = json.dumps(inputs, indent=2, default=str)

    with claude_log.log_call(
        db, purpose="attribution", model=_ATTRIBUTION_MODEL,
        event_id=event.id, attendee_id=attendee.id,
        prompt=f"SYSTEM:\n{_ATTRIBUTION_SYSTEM}\n\nUSER:\n{user_prompt}",
    ) as call:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=_api_key())
            resp = client.messages.create(
                model=_ATTRIBUTION_MODEL,
                max_tokens=800,
                timeout=_ATTRIBUTION_TIMEOUT_S,
                system=[{"type": "text", "text": _ATTRIBUTION_SYSTEM,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": "{"},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            call.status = "error"
            call.error = f"{type(exc).__name__}: {exc}"
            return _persist_attribution(
                db, attendee, event,
                outcome="none", confidence=0.0, value=0,
                rationale=f"Attribution call failed: {type(exc).__name__}",
                evidence=[],
            )

        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        full = "{" + text
        call.output = full

        from ..jsonx import extract_json
        parsed = extract_json(full)
        if not parsed:
            call.status = "parse_error"
            return _persist_attribution(
                db, attendee, event,
                outcome="none", confidence=0.0, value=0,
                rationale="Couldn't parse attribution JSON.",
                evidence=[],
            )

        outcome = parsed.get("outcome", "none")
        if outcome not in VALID_OUTCOMES:
            outcome = "other"
        confidence = float(parsed.get("confidence") or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        try:
            value = int(parsed.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        rationale = (parsed.get("rationale") or "").strip()
        evidence = parsed.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = []

        return _persist_attribution(
            db, attendee, event,
            outcome=outcome, confidence=confidence, value=value,
            rationale=rationale, evidence=[str(e) for e in evidence],
        )


def _persist_attribution(
    db: Session,
    attendee: models.Attendee,
    event: models.Event,
    *,
    outcome: str,
    confidence: float,
    value: int,
    rationale: str,
    evidence: list[str],
) -> models.AttendeeAttribution:
    row = models.AttendeeAttribution(
        attendee_id=attendee.id, event_id=event.id,
        outcome=outcome, confidence=confidence, value=value,
        rationale=rationale, evidence=json.dumps(evidence),
    )
    db.add(row)
    db.flush()
    return row


def _safe_json(s: str | None) -> dict:
    if not s:
        return {}
    try:
        out = json.loads(s)
    except json.JSONDecodeError:
        return {}
    return out if isinstance(out, dict) else {}
