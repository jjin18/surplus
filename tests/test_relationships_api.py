"""
Route tests for the relationship read API (routes/relationships.py) and the
CRM capture-row enrichment in routes/inperson.py.

Repo convention : call route functions directly with an in-memory SQLAlchemy
session + real ORM rows. No TestClient / auth cookies; UNIPILE_DRY_RUN on.
"""
from __future__ import annotations
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes import relationships as rel_route
from backend.routes.inperson import _capture_row


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("UNIPILE_DRY_RUN", "true")
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _user(db, **kw):
    u = models.User(name=kw.get("name", "Op"), email=kw.get("email", "op@x.com"),
                    unipile_account_id=kw.get("acct", "acct1"))
    db.add(u); db.commit()
    return u


def _captured_prospect(db, user, **kw):
    ev = models.Event(user_id=user.id, kind="in_person", label="Mixer", city="SF")
    db.add(ev); db.commit()
    p = models.Prospect(
        event_id=ev.id, identity="maya", name="Maya Rodriguez",
        role="Staff Infra", company="Lo91r",
        linkedin_url="https://linkedin.com/in/maya",
        status="pending", source="scan",
        captured_at=datetime.now(timezone.utc),
        note=kw.get("note"), private_note=kw.get("private_note"),
        contact_type=kw.get("contact_type"), next_step=kw.get("next_step"),
    )
    db.add(p); db.commit()
    return ev, p


# ── timeline endpoint ───────────────────────────────────────────────────

def test_timeline_endpoint_shape(db):
    u = _user(db)
    ev, p = _captured_prospect(db, u, note="talked KV-cache", contact_type="sponsor")
    db.add(models.OutreachLog(prospect_id=p.id, channel="linkedin",
                              state="invite_sent")); db.commit()

    out = rel_route.prospect_timeline(p.id, db, u)
    assert set(out) == {"prospect", "relationship_summary", "timeline"}
    assert out["prospect"]["prospect_id"] == p.id
    assert out["prospect"]["name"] == "Maya Rodriguez"
    assert out["relationship_summary"]["relationship_stage"] == "contacted"
    assert out["relationship_summary"]["contact_type"] == "sponsor"
    # timeline carries capture + note + outreach
    kinds = {it["source_type"] for it in out["timeline"]}
    assert {"in_person_capture", "manual_note", "linkedin_outreach"} <= kinds


def test_timeline_unauthorized_prospect_blocked(db):
    owner = _user(db, email="owner@x.com", acct="owner_acct")
    other = _user(db, email="other@x.com", acct="other_acct")
    ev, p = _captured_prospect(db, owner)
    # A different user must not be able to read the owner's prospect.
    with pytest.raises(HTTPException) as ei:
        rel_route.prospect_timeline(p.id, db, other)
    assert ei.value.status_code == 404


def test_timeline_missing_prospect_404(db):
    u = _user(db)
    with pytest.raises(HTTPException) as ei:
        rel_route.prospect_timeline(99999, db, u)
    assert ei.value.status_code == 404


def test_timeline_prospect_brief_does_not_leak_private_note(db):
    u = _user(db)
    ev, p = _captured_prospect(db, u, private_note="budget approver")
    out = rel_route.prospect_timeline(p.id, db, u)
    # The compact header subset must not carry the private note text.
    assert "budget approver" not in str(out["prospect"])


# ── /captures enrichment (backward compatibility) ─────────────────────────

def test_capture_row_keeps_existing_fields_and_adds_summary(db):
    u = _user(db)
    ev, p = _captured_prospect(db, u, next_step="send deck")
    row = _capture_row(p)
    # Existing fields preserved (not renamed/removed).
    for key in ("prospect_id", "name", "role", "company", "linkedin_url",
                "status", "connection_status", "source", "captured_at",
                "note", "private_note", "contact_type", "next_step",
                "resolve_failed", "last_outreach", "conversion"):
        assert key in row
    # New additive field.
    assert "relationship_summary" in row
    assert row["relationship_summary"]["next_step"] == "send deck"
    assert row["relationship_summary"]["relationship_stage"] == "captured"
