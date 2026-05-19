"""
curation/csv_import.py : Stage 1 generic-CSV ingest.

Operators land in surplus with their own audience already in a spreadsheet:
alumni rosters, member lists, past attendees, nominees, sponsor target
lists, RSVP exports. The shape is wildly inconsistent : we accept it and
let the operator confirm a column mapping before we commit.

Two-step flow the route handler exposes:

  1. POST .../preview-mapping
        Parse the CSV header + first N rows, propose a column mapping
        (canonical_field -> source_column), return it for the UI to render
        a confirmation step. No DB writes.

  2. POST .../import
        Apply the (possibly operator-edited) mapping, dedupe against
        already-imported Attendees for the event, persist new rows.

Dedupe key (in priority order):
  - email (case-insensitive, stripped) when present
  - else: normalized "name|company" (lowercased, punctuation collapsed)

Canonical fields the rest of curation/ expects on an Attendee row are
listed in CANONICAL_FIELDS. Everything else in the CSV is preserved in
the row's `raw` JSON dict so the scoring step can read the operator's
custom columns ("Years in industry", "Portfolio size", ...).
"""
from __future__ import annotations
import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import IO


CANONICAL_FIELDS: tuple[str, ...] = (
    "name", "email", "role", "company", "seniority", "linkedin_url",
    "rsvp_status",
)


# Header variants → canonical. Lower-cased, light punctuation already
# stripped before lookup. Substring fallback below catches the long-tail.
_HEADER_HINTS: dict[str, str] = {
    # name
    "name": "name", "full name": "name", "first and last name": "name",
    "attendee name": "name", "guest name": "name", "member name": "name",
    # email
    "email": "email", "email address": "email", "contact email": "email",
    "primary email": "email", "work email": "email",
    # role
    "role": "role", "title": "role", "job title": "role", "position": "role",
    "current role": "role",
    # company
    "company": "company", "organization": "company", "company name": "company",
    "employer": "company", "org": "company", "current company": "company",
    # seniority
    "seniority": "seniority", "level": "seniority", "career level": "seniority",
    # linkedin
    "linkedin": "linkedin_url", "linkedin url": "linkedin_url",
    "linkedin profile": "linkedin_url", "linkedin link": "linkedin_url",
    # rsvp
    "rsvp": "rsvp_status", "rsvp status": "rsvp_status",
    "attending": "rsvp_status", "registration status": "rsvp_status",
    "status": "rsvp_status",
}

_HEADER_HINTS_SORTED = sorted(_HEADER_HINTS.items(), key=lambda kv: -len(kv[0]))


def _normalize(h: str) -> str:
    h = (h or "").strip().lower()
    h = re.sub(r"[_:?!.\-/\\]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def suggest_field(header: str) -> str | None:
    """Best-guess canonical field for a CSV header, or None if unknown."""
    norm = _normalize(header)
    if not norm:
        return None
    if norm in _HEADER_HINTS:
        return _HEADER_HINTS[norm]
    for key, canonical in _HEADER_HINTS_SORTED:
        if key in norm:
            return canonical
    return None


def _read_rows(content: str | bytes) -> tuple[list[str], list[dict]]:
    """Parse a CSV into (headers, rows). Handles UTF-8 BOM."""
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = content
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], []
    headers = [h for h in reader.fieldnames if h is not None]
    rows = [dict(r) for r in reader]
    return headers, rows


def propose_mapping(content: str | bytes, sample_size: int = 5) -> dict:
    """Return {'columns': [...], 'mapping': {field: column}, 'sample': [...]}.

    The mapping is a STARTING POINT for the UI : operators can override it
    before calling import_csv(). `sample` echoes back the first sample_size
    rows so the UI can render a confirmation table.
    """
    headers, rows = _read_rows(content)
    mapping: dict[str, str] = {}
    for h in headers:
        guess = suggest_field(h)
        if guess and guess not in mapping:
            mapping[guess] = h
    return {
        "columns": headers,
        "mapping": mapping,
        "sample": rows[:sample_size],
        "row_count": len(rows),
    }


def _dedupe_key(name: str, email: str | None, company: str | None) -> str:
    if email and email.strip():
        return f"email:{email.strip().lower()}"
    nm = re.sub(r"\s+", " ", (name or "").strip().lower())
    co = re.sub(r"\s+", " ", (company or "").strip().lower())
    return f"nc:{nm}|{co}"


_RSVP_NORMALIZE = {
    "yes": "rsvp_yes", "going": "rsvp_yes", "attending": "rsvp_yes",
    "registered": "rsvp_yes", "confirmed": "rsvp_yes",
    "no": "rsvp_no", "declined": "rsvp_no", "not attending": "rsvp_no",
    "maybe": "waitlist", "waitlist": "waitlist", "tentative": "waitlist",
    "invited": "invited",
    "attended": "attended",
}


def _normalize_rsvp(raw: str | None) -> str | None:
    if not raw:
        return None
    return _RSVP_NORMALIZE.get(raw.strip().lower(), raw.strip())


def import_csv(
    db,
    event_id: int,
    content: str | bytes,
    *,
    mapping: dict[str, str],
    list_source: str = "other",
    default_rsvp: str | None = None,
):
    """Apply `mapping` to the CSV, dedupe, persist new Attendee rows.

    Returns (inserted_rows, skipped_count, applied_mapping). Caller is
    responsible for calling db.commit() once it's also written any audit
    rows it wants to attach.

    `mapping` is {canonical_field: source_column}. Unknown canonical
    fields are ignored, missing canonical fields are blank. Unmapped CSV
    columns land in Attendee.raw.

    `default_rsvp` is applied when the mapping doesn't include `rsvp_status`
    : useful for "this whole CSV is the RSVP yes list" imports.
    """
    from .. import models

    headers, rows = _read_rows(content)
    header_set = set(headers)
    # Filter the mapping down to fields that point at real headers, so a
    # stale UI guess for a missing column doesn't crash the row read.
    applied = {field: src for field, src in mapping.items()
               if field in CANONICAL_FIELDS and src in header_set}
    mapped_headers = set(applied.values())

    # Build a dedupe index from existing attendees on this event.
    existing = db.query(models.Attendee).filter(
        models.Attendee.event_id == event_id
    ).all()
    seen: set[str] = {
        _dedupe_key(a.name, a.email, a.company) for a in existing
    }

    now = datetime.now(timezone.utc)
    inserted: list[models.Attendee] = []
    skipped = 0
    for row in rows:
        record: dict[str, str] = {}
        for field, src in applied.items():
            record[field] = (row.get(src) or "").strip()
        raw_extras: dict[str, str] = {}
        for col, val in row.items():
            if col is None or col in mapped_headers:
                continue
            val = (val or "").strip()
            if val:
                raw_extras[col] = val

        name = record.get("name") or ""
        email = record.get("email") or None
        # Skip obviously empty rows (no name AND no email).
        if not name and not email:
            skipped += 1
            continue

        key = _dedupe_key(name, email, record.get("company"))
        if key in seen:
            skipped += 1
            continue
        seen.add(key)

        rsvp_raw = record.get("rsvp_status") or default_rsvp
        attendee = models.Attendee(
            event_id=event_id,
            name=name,
            email=email,
            role=record.get("role") or None,
            company=record.get("company") or None,
            seniority=record.get("seniority") or None,
            linkedin_url=record.get("linkedin_url") or None,
            list_source=list_source,
            rsvp_status=_normalize_rsvp(rsvp_raw),
            raw=json.dumps(raw_extras),
            created_at=now,
            updated_at=now,
        )
        db.add(attendee)
        inserted.append(attendee)

    db.flush()  # populate ids for the response, but let the route own commit
    return inserted, skipped, applied
