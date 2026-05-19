"""
routes/triage.py : Applicant Triage HTTP surface.

Three endpoints in PR B (scoring comes in PR C, review UI in PR D,
decisions+export in PR E):

  POST  /events/{id}/triage/config       : set / update triage_config JSON
  POST  /events/{id}/triage/upload       : multipart CSV, parses + persists Applicants
  GET   /events/{id}/triage/applicants   : list parsed applicants (no scores yet)

All three are auth-gated via current_user and scoped to events the
signed-in user owns. Same get_owned_event pattern as the outbound routes.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..auth import current_user, get_owned_event
from ..db import get_db
from ..triage.csv_parser import parse_csv_file


router = APIRouter(prefix="/events", tags=["06 · triage"])


# ── Schemas ─────────────────────────────────────────────────────────────

class TriageConfig(BaseModel):
    """Operator-defined sponsor + event criteria. All fields optional so the
    operator can fill the form incrementally without 400-ing the API.

    These flow into the scoring rubric (PR C) which generates per-event
    weighted axes; nothing here is interpreted directly by the scorer."""
    event_type: Optional[str] = None
    sponsor_name: Optional[str] = None
    event_goal: Optional[str] = None
    ideal_attendee_profile: Optional[str] = None
    hard_filters: list[str] = []
    nice_to_have_signals: list[str] = []
    anti_fit_examples: list[str] = []
    capacity: Optional[int] = None
    notes: Optional[str] = None


class ApplicantOut(BaseModel):
    """One applicant row as returned by GET /applicants. Evaluation +
    decision fields are deferred to PR C / PR E."""
    id: int
    name: str
    email: Optional[str]
    role: Optional[str]
    company: Optional[str]
    website: Optional[str]
    linkedin_url: Optional[str]
    raw_application_data: dict
    created_at: datetime


class UploadResult(BaseModel):
    event_id: int
    parsed: int       # how many rows the CSV had after the empty-row filter
    inserted: int     # how many we actually wrote (excludes duplicates etc.)
    applicants: list[ApplicantOut]


# ── Endpoints ──────────────────────────────────────────────────────────

@router.post("/{event_id}/triage/config", response_model=TriageConfig)
def set_triage_config(
    event_id: int,
    body: TriageConfig,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Set the sponsor / event criteria that the scoring rubric will key off.
    Idempotent : POSTing again replaces the whole config."""
    ev = get_owned_event(event_id, user, db)
    ev.triage_config = body.model_dump_json()
    db.commit()
    return body


@router.get("/{event_id}/triage/config", response_model=TriageConfig)
def get_triage_config(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    ev = get_owned_event(event_id, user, db)
    if not ev.triage_config:
        return TriageConfig()
    try:
        return TriageConfig(**json.loads(ev.triage_config))
    except (json.JSONDecodeError, ValueError):
        # Bad JSON shouldn't 500 the UI; return empty config and let the
        # operator re-save.
        return TriageConfig()


@router.post("/{event_id}/triage/upload", response_model=UploadResult)
def upload_applicants(
    event_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """Upload a Luma CSV. Parses with flexible field mapping, persists each
    row as an Applicant. Returns the parsed + inserted counts plus the new
    applicants so the UI can update without a follow-up GET.

    Doesn't deduplicate by email yet : re-uploading the same CSV creates
    duplicate Applicant rows. PR C / D may add a dedup pass on top.
    """
    ev = get_owned_event(event_id, user, db)
    if not (file.content_type or "").lower().startswith(
        ("text/csv", "application/csv", "application/vnd.ms-excel", "text/plain")
    ) and not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, f"expected a CSV file, got {file.content_type!r}")

    try:
        parsed_rows = parse_csv_file(file.file)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not parse CSV: {type(exc).__name__}: {exc}")

    now = datetime.now(timezone.utc)
    new_applicants: list[models.Applicant] = []
    for row in parsed_rows:
        a = models.Applicant(
            event_id=ev.id,
            name=row.get("name") or "",
            email=row.get("email") or None,
            role=row.get("role") or None,
            company=row.get("company") or None,
            website=row.get("website") or None,
            linkedin_url=row.get("linkedin_url") or None,
            raw_application_data=json.dumps(row.get("raw_application_data") or {}),
            created_at=now,
            updated_at=now,
        )
        db.add(a)
        new_applicants.append(a)
    db.commit()
    for a in new_applicants:
        db.refresh(a)

    return UploadResult(
        event_id=ev.id,
        parsed=len(parsed_rows),
        inserted=len(new_applicants),
        applicants=[_applicant_out(a) for a in new_applicants],
    )


@router.get("/{event_id}/triage/applicants", response_model=list[ApplicantOut])
def list_applicants(
    event_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_user),
):
    """All applicants for this event. Sorted by created_at ascending so the
    review queue order matches the original CSV order."""
    ev = get_owned_event(event_id, user, db)
    rows = sorted(ev.applicants, key=lambda a: a.created_at)
    return [_applicant_out(a) for a in rows]


def _applicant_out(a: models.Applicant) -> ApplicantOut:
    try:
        raw = json.loads(a.raw_application_data or "{}")
        if not isinstance(raw, dict):
            raw = {}
    except json.JSONDecodeError:
        raw = {}
    return ApplicantOut(
        id=a.id, name=a.name, email=a.email, role=a.role, company=a.company,
        website=a.website, linkedin_url=a.linkedin_url,
        raw_application_data=raw,
        created_at=a.created_at,
    )
