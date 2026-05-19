"""
Tests for triage routes (POST config / POST upload / GET applicants).

Exercises the route functions directly with an in-memory SQLAlchemy
session : same workaround test_followups.py uses to avoid the Python
3.9 / str|None evaluation issue when importing FastAPI's app.
"""
from __future__ import annotations
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def user_and_event(db):
    """Real User + Event rows : routes need ownership to pass get_owned_event."""
    user = models.User(name="Operator", email="op@example.com", unipile_account_id=None)
    db.add(user); db.flush()
    ev = models.Event(
        user_id=user.id,
        role="(triage event)", seniority="", co_stage="",
        headcount=40, format="Sit-down dinner", city="NYC",
        goal="", budget=0, sources="linkedin",
    )
    db.add(ev); db.commit()
    return user, ev


def _upload_file(content: str | bytes, filename="luma.csv",
                 content_type="text/csv") -> UploadFile:
    """Build a FastAPI UploadFile around an in-memory CSV string."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    f = io.BytesIO(content)
    return UploadFile(filename=filename, file=f,
                      headers={"content-type": content_type})


# ── config ─────────────────────────────────────────────────────────────

def test_set_and_get_config_roundtrips(db, user_and_event):
    from backend.routes.triage import set_triage_config, get_triage_config, TriageConfig
    user, ev = user_and_event
    body = TriageConfig(
        event_type="sponsor_cafe", sponsor_name="Stripe x ElevenLabs",
        event_goal="builders with high-transaction products",
        ideal_attendee_profile="founders shipping consumer AI",
        hard_filters=["Must be in NYC"],
        nice_to_have_signals=["High monthly transactions"],
        anti_fit_examples=["Photography businesses"],
        capacity=30, notes="this is a test",
    )
    set_triage_config(ev.id, body, db, user)
    db.refresh(ev)
    # Round-trip through GET
    got = get_triage_config(ev.id, db, user)
    assert got.sponsor_name == "Stripe x ElevenLabs"
    assert got.hard_filters == ["Must be in NYC"]
    assert got.capacity == 30


def test_get_config_returns_empty_for_unset_event(db, user_and_event):
    from backend.routes.triage import get_triage_config
    user, ev = user_and_event
    got = get_triage_config(ev.id, db, user)
    assert got.sponsor_name is None
    assert got.hard_filters == []


def test_get_config_returns_empty_on_corrupted_json(db, user_and_event):
    """A bad triage_config string shouldn't 500 the UI : return empty,
    operator can re-save."""
    from backend.routes.triage import get_triage_config
    user, ev = user_and_event
    ev.triage_config = "not valid json {"
    db.commit()
    got = get_triage_config(ev.id, db, user)
    assert got.sponsor_name is None


# ── upload ─────────────────────────────────────────────────────────────

def test_upload_persists_applicants(db, user_and_event):
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    csv = (
        "Name,Email,Job Title,Company,LinkedIn URL,Are you a creator?\n"
        "Maya,m@x.com,Staff Infra,Lo91r,https://linkedin.com/in/maya,no\n"
        "Theo,t@x.com,Distrib Sys,Fly.io,https://linkedin.com/in/theo,yes\n"
    )
    result = upload_applicants(ev.id, _upload_file(csv), db, user)
    assert result.parsed == 2
    assert result.inserted == 2
    db.refresh(ev)
    assert len(ev.applicants) == 2
    maya = next(a for a in ev.applicants if a.name == "Maya")
    assert maya.email == "m@x.com"
    assert maya.linkedin_url == "https://linkedin.com/in/maya"
    raw = json.loads(maya.raw_application_data)
    assert raw["Are you a creator?"] == "no"


def test_upload_rejects_non_csv_content_type(db, user_and_event):
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    # Send a PNG-ish content-type to make sure we 400
    bad_file = UploadFile(
        filename="not-a-csv.png", file=io.BytesIO(b"not csv"),
        headers={"content-type": "image/png"},
    )
    with pytest.raises(HTTPException) as exc:
        upload_applicants(ev.id, bad_file, db, user)
    assert exc.value.status_code == 400


def test_upload_accepts_csv_extension_even_with_octet_stream(db, user_and_event):
    """Browsers sometimes send application/octet-stream for CSVs;
    accept it as long as the filename ends in .csv."""
    from backend.routes.triage import upload_applicants
    user, ev = user_and_event
    csv = "Name,Email\nMaya,m@x.com\n"
    f = UploadFile(
        filename="applicants.csv", file=io.BytesIO(csv.encode()),
        headers={"content-type": "application/octet-stream"},
    )
    result = upload_applicants(ev.id, f, db, user)
    assert result.inserted == 1


# ── list ───────────────────────────────────────────────────────────────

def test_list_applicants_returns_sorted_by_created_at(db, user_and_event):
    from backend.routes.triage import upload_applicants, list_applicants
    user, ev = user_and_event
    csv = "Name,Email\nFirst,1@x.com\nSecond,2@x.com\nThird,3@x.com\n"
    upload_applicants(ev.id, _upload_file(csv), db, user)
    listed = list_applicants(ev.id, db, user)
    assert [a.name for a in listed] == ["First", "Second", "Third"]
    # Spot-check ApplicantOut shape
    assert listed[0].raw_application_data == {}
    assert listed[0].email == "1@x.com"
