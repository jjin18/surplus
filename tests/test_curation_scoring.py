"""Tests for backend/curation/scoring.py : rule-based fit score + audit log."""
from __future__ import annotations
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.curation import scoring


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


def _attendee(db, event, **overrides):
    enrichment = overrides.pop("enrichment", None)
    a = models.Attendee(
        event_id=event.id,
        name=overrides.pop("name", "Tester"),
        email=overrides.pop("email", "t@x.com"),
        role=overrides.pop("role", "Senior PM"),
        company=overrides.pop("company", "Acme"),
        seniority=overrides.pop("seniority", "Senior"),
        linkedin_url=overrides.pop("linkedin_url", None),
        list_source=overrides.pop("list_source", "alumni"),
        rsvp_status=overrides.pop("rsvp_status", None),
        enrichment=json.dumps(enrichment) if enrichment else "{}",
    )
    db.add(a); db.commit()
    return a


def test_score_is_deterministic(db, event):
    a = _attendee(db, event, enrichment={
        "firmographic": {"company_stage": "Seed", "company_industry": "ML",
                          "company_size_bucket": "11-50",
                          "company_summary": "ML tooling for ops"},
        "role": {"function": "Engineering", "specialty": "ML platform",
                  "ic_or_management": "ic"},
        "seniority": {"level": "Senior", "years_experience_estimate": 8},
        "confidence": "high",
    })
    icp = scoring.ICP(role="ML engineer", seniority="Senior",
                       function="Engineering", company_stage="Seed",
                       keywords=["ml"])
    s1, t1 = scoring.score_attendee(a, icp)
    s2, t2 = scoring.score_attendee(a, icp)
    assert s1 == s2
    assert t1 == t2
    assert 0 <= s1 <= 100


def test_seniority_match_boosts_score(db, event):
    icp = scoring.ICP(seniority="Senior")
    high = _attendee(db, event, name="High", email="h@x.com",
                     enrichment={"seniority": {"level": "Staff+"},
                                 "firmographic": {}, "role": {},
                                 "confidence": "high"})
    low = _attendee(db, event, name="Low", email="l@x.com",
                    enrichment={"seniority": {"level": "Junior"},
                                "firmographic": {}, "role": {},
                                "confidence": "high"})
    sh, _ = scoring.score_attendee(high, icp)
    sl, _ = scoring.score_attendee(low, icp)
    assert sh > sl


def test_function_off_penalises_score(db, event):
    icp = scoring.ICP(function="Engineering")
    a = _attendee(db, event, enrichment={
        "role": {"function": "Sales"},
        "firmographic": {}, "seniority": {}, "confidence": "medium",
    })
    score, trace = scoring.score_attendee(a, icp)
    assert any("function_off" in t for t in trace)


def test_no_contact_penalises_score(db, event):
    a = _attendee(db, event, email=None, linkedin_url=None)
    s, trace = scoring.score_attendee(a, scoring.ICP())
    assert "no_contact" in trace


def test_score_and_explain_writes_to_attendee(db, event, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = _attendee(db, event)
    icp = scoring.ICP(seniority="Senior", function="Product")
    score, trace, rationale = scoring.score_and_explain(
        db, a, icp, with_rationale=True,
    )
    db.commit()
    db.refresh(a)
    assert a.fit_score == score
    assert json.loads(a.fit_rule_trace) == trace
    # No API key : rationale should fall back to the rule-trace string
    assert rationale != ""
    # And we logged a disabled LLM call
    rows = db.query(models.LLMCall).filter(
        models.LLMCall.attendee_id == a.id
    ).all()
    assert any(r.status == "disabled" for r in rows)


def test_icp_from_event_reads_event_intake(db, event):
    icp = scoring.ICP.from_event(event)
    assert icp.role == "x"
    assert "Senior" in icp.seniority
    assert "Seed" in icp.company_stage
