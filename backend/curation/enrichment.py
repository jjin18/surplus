"""
curation/enrichment.py : Stage 1 per-person + per-company enrichment via Claude.

Operator-supplied CSVs are usually sparse : a name, an email, maybe a
company. To score and match meaningfully we need firmographic + role
+ seniority signal on each row. Claude reads the row plus any free-form
raw_application_data and emits a structured enrichment payload that we
cache on Attendee.enrichment.

Cache semantics:
  - Cached forever once written. The operator can force a refresh via
    POST .../enrich?refresh=true, which deletes the cache and re-runs.
  - Lives on the Attendee row (not a separate table) so we don't have to
    join on every read.

Audit trail:
  - Every Claude call writes an LLMCall row via claude_log.log_call().
  - If ANTHROPIC_API_KEY is unset the function returns a deterministic
    empty-enrichment shape and logs a "disabled" audit row : keeps the
    pipeline running offline without claiming AI did the work.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import claude_log


ENRICHMENT_MODEL = os.environ.get("CURATION_ENRICH_MODEL", "claude-haiku-4-5-20251001")
ENRICHMENT_TIMEOUT_S = float(os.environ.get("CURATION_ENRICH_TIMEOUT", "12"))


_ENRICH_SYSTEM = """You enrich a single event attendee's record. You receive
the operator-supplied CSV fields plus any free-form raw fields.

Return ONLY a JSON object with this schema. Use null for unknown fields;
NEVER invent specifics.

{
  "firmographic": {
    "company_industry": "string|null",
    "company_size_bucket": "1-10|11-50|51-200|201-1000|1001-5000|5001+|null",
    "company_stage": "Pre-seed|Seed|Series A|Series B|Series C+|Public|null",
    "company_summary": "1 sentence describing what the company does, or null"
  },
  "role": {
    "function": "Engineering|Product|Design|Sales|Marketing|Operations|Finance|Founder|Investor|Other|null",
    "specialty": "1-3 word specialty within the function, or null",
    "ic_or_management": "ic|management|founder|null"
  },
  "seniority": {
    "level": "Student|New grad|Junior|Mid|Senior|Staff+|Leadership|null",
    "years_experience_estimate": "integer or null"
  },
  "confidence": "low|medium|high"
}

Set confidence based on how much signal the row contains. Sparse rows
(just a name + email) should be "low" and most fields null."""


def _api_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _build_user_prompt(attendee: models.Attendee) -> str:
    raw = {}
    try:
        raw = json.loads(attendee.raw or "{}")
    except json.JSONDecodeError:
        raw = {}
    canonical = {
        "name": attendee.name,
        "email": attendee.email,
        "role": attendee.role,
        "company": attendee.company,
        "seniority": attendee.seniority,
        "linkedin_url": attendee.linkedin_url,
        "list_source": attendee.list_source,
    }
    parts = [
        "Canonical CSV fields:",
        json.dumps(canonical, indent=2),
        "",
        "Free-form / custom CSV fields the operator captured:",
        json.dumps(raw, indent=2) if raw else "(none)",
        "",
        "Emit the JSON enrichment now.",
    ]
    return "\n".join(parts)


def empty_enrichment() -> dict:
    """Schema-matching empty enrichment. Used when LLM is unavailable so
    downstream code can read the fields without None-guarding everywhere."""
    return {
        "firmographic": {
            "company_industry": None, "company_size_bucket": None,
            "company_stage": None, "company_summary": None,
        },
        "role": {"function": None, "specialty": None, "ic_or_management": None},
        "seniority": {"level": None, "years_experience_estimate": None},
        "confidence": "low",
    }


def enrich_attendee(
    db: Session,
    attendee: models.Attendee,
    *,
    refresh: bool = False,
) -> dict:
    """Enrich one attendee. Returns the parsed enrichment dict (and writes
    it to attendee.enrichment as JSON).

    If `refresh` is False and enrichment is already cached, returns the
    cached value without re-calling Claude.
    """
    cached = (attendee.enrichment or "").strip()
    if cached and not refresh:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass  # malformed cache : just re-enrich

    if not _api_key():
        # Log that we didn't call Claude so the auditor can tell this
        # apart from a successful call.
        claude_log.log_disabled(
            db, purpose="enrichment",
            event_id=attendee.event_id, attendee_id=attendee.id,
        )
        out = empty_enrichment()
        attendee.enrichment = json.dumps(out)
        attendee.enriched_at = datetime.now(timezone.utc)
        return out

    prompt = _build_user_prompt(attendee)

    with claude_log.log_call(
        db, purpose="enrichment", model=ENRICHMENT_MODEL,
        event_id=attendee.event_id, attendee_id=attendee.id,
        prompt=f"SYSTEM:\n{_ENRICH_SYSTEM}\n\nUSER:\n{prompt}",
    ) as call:
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=_api_key())
            resp = client.messages.create(
                model=ENRICHMENT_MODEL,
                max_tokens=800,
                timeout=ENRICHMENT_TIMEOUT_S,
                system=[{"type": "text", "text": _ENRICH_SYSTEM,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "{"},
                ],
            )
        except Exception as exc:  # noqa: BLE001
            call.status = "error"
            call.error = f"{type(exc).__name__}: {exc}"
            call.output = ""
            attendee.enrichment = json.dumps(empty_enrichment())
            attendee.enriched_at = datetime.now(timezone.utc)
            return empty_enrichment()

        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        full = "{" + text
        call.output = full

        from ..jsonx import extract_json
        parsed = extract_json(full)
        if not parsed:
            call.status = "parse_error"
            attendee.enrichment = json.dumps(empty_enrichment())
            attendee.enriched_at = datetime.now(timezone.utc)
            return empty_enrichment()

        # Light validation : keep only known top-level keys, merge in
        # the empty shape for safety.
        merged = empty_enrichment()
        for key in ("firmographic", "role", "seniority"):
            if isinstance(parsed.get(key), dict):
                merged[key].update({
                    k: v for k, v in parsed[key].items()
                    if k in merged[key]
                })
        if parsed.get("confidence") in ("low", "medium", "high"):
            merged["confidence"] = parsed["confidence"]

        attendee.enrichment = json.dumps(merged)
        attendee.enriched_at = datetime.now(timezone.utc)
        return merged


def get_enrichment(attendee: models.Attendee) -> dict:
    """Read attendee.enrichment as a dict. Always returns the schema shape
    (falls through to empty_enrichment() on missing / malformed JSON)."""
    raw = (attendee.enrichment or "").strip()
    if not raw:
        return empty_enrichment()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return empty_enrichment()
    out = empty_enrichment()
    if isinstance(parsed, dict):
        for key in ("firmographic", "role", "seniority"):
            if isinstance(parsed.get(key), dict):
                out[key].update({
                    k: v for k, v in parsed[key].items() if k in out[key]
                })
        if parsed.get("confidence") in ("low", "medium", "high"):
            out["confidence"] = parsed["confidence"]
    return out
