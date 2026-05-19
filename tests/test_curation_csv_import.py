"""Tests for backend/curation/csv_import.py : header guessing + import + dedupe."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.curation import csv_import


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def event(db):
    u = models.User(name="Op", unipile_account_id=None)
    db.add(u); db.flush()
    ev = models.Event(user_id=u.id, role="x", seniority="Senior",
                       co_stage="Seed", headcount=20, format="Mixer",
                       city="SF", goal="Hiring pipeline", budget=5000)
    db.add(ev); db.commit()
    return ev


def test_suggest_field_handles_variants():
    assert csv_import.suggest_field("Full Name") == "name"
    assert csv_import.suggest_field("Email Address") == "email"
    assert csv_import.suggest_field("LinkedIn URL") == "linkedin_url"
    assert csv_import.suggest_field("RSVP Status") == "rsvp_status"
    assert csv_import.suggest_field("favourite_color") is None


def test_propose_mapping_returns_columns_and_sample():
    csv = (
        "Full Name,Email,Title,Company,Years in industry\n"
        "Alice,a@x.com,Senior PM,Acme,8\n"
        "Bob,b@y.com,Staff Eng,Globex,6\n"
    )
    proposal = csv_import.propose_mapping(csv.encode("utf-8"), sample_size=10)
    assert proposal["row_count"] == 2
    assert proposal["mapping"]["name"] == "Full Name"
    assert proposal["mapping"]["email"] == "Email"
    assert proposal["mapping"]["company"] == "Company"
    assert proposal["mapping"]["role"] == "Title"
    assert len(proposal["sample"]) == 2


def test_import_csv_persists_with_mapping(db, event):
    csv = (
        "Full Name,Email,Title,Company,Years in industry\n"
        "Alice,a@x.com,Senior PM,Acme,8\n"
        "Bob,b@y.com,Staff Eng,Globex,6\n"
    )
    inserted, skipped, applied = csv_import.import_csv(
        db, event.id, csv.encode("utf-8"),
        mapping={"name": "Full Name", "email": "Email",
                 "role": "Title", "company": "Company"},
        list_source="alumni",
    )
    db.commit()
    assert len(inserted) == 2
    assert skipped == 0
    assert applied["role"] == "Title"
    a = inserted[0]
    assert a.list_source == "alumni"
    # The unmapped 'Years in industry' should land in raw
    import json
    raw = json.loads(a.raw)
    assert raw.get("Years in industry") == "8"


def test_import_csv_dedupes_on_re_run(db, event):
    csv = "Full Name,Email\nAlice,a@x.com\nBob,b@x.com\n"
    csv_import.import_csv(
        db, event.id, csv,
        mapping={"name": "Full Name", "email": "Email"},
    )
    db.commit()
    # Re-import same content : everything should dedupe.
    inserted2, skipped2, _ = csv_import.import_csv(
        db, event.id, csv,
        mapping={"name": "Full Name", "email": "Email"},
    )
    db.commit()
    assert len(inserted2) == 0
    assert skipped2 == 2


def test_import_csv_dedupes_on_email_case_insensitive(db, event):
    csv1 = "Name,Email\nAlice,Alice@Example.com\n"
    csv2 = "Name,Email\nAlice,alice@example.com\n"
    csv_import.import_csv(db, event.id, csv1,
                          mapping={"name": "Name", "email": "Email"})
    db.commit()
    inserted2, skipped2, _ = csv_import.import_csv(
        db, event.id, csv2,
        mapping={"name": "Name", "email": "Email"},
    )
    db.commit()
    assert len(inserted2) == 0
    assert skipped2 == 1


def test_import_csv_skips_empty_rows(db, event):
    csv = "Name,Email\n,\nAlice,a@x.com\n,\n"
    inserted, skipped, _ = csv_import.import_csv(
        db, event.id, csv,
        mapping={"name": "Name", "email": "Email"},
    )
    db.commit()
    assert len(inserted) == 1
    assert skipped == 2


def test_import_csv_applies_default_rsvp(db, event):
    csv = "Name,Email\nAlice,a@x.com\n"
    inserted, _, _ = csv_import.import_csv(
        db, event.id, csv,
        mapping={"name": "Name", "email": "Email"},
        default_rsvp="rsvp_yes",
    )
    db.commit()
    assert inserted[0].rsvp_status == "rsvp_yes"


def test_import_csv_normalizes_rsvp_status(db, event):
    csv = "Name,Email,RSVP\nA,a@x.com,Yes\nB,b@x.com,Declined\nC,c@x.com,Maybe\n"
    inserted, _, _ = csv_import.import_csv(
        db, event.id, csv,
        mapping={"name": "Name", "email": "Email", "rsvp_status": "RSVP"},
    )
    db.commit()
    statuses = sorted(a.rsvp_status for a in inserted)
    assert statuses == ["rsvp_no", "rsvp_yes", "waitlist"]
