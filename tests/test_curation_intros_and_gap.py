"""Tests for backend/curation/intros.py and gap_analysis.py."""
from __future__ import annotations
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.curation import gap_analysis, intros


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


def _attendee(db, event, function, stage=None, fit=70, **kw):
    enrichment = {
        "role": {"function": function, "specialty": kw.get("specialty")},
        "firmographic": {"company_stage": stage,
                          "company_industry": kw.get("industry")},
        "seniority": {"level": kw.get("seniority", "Senior")},
        "confidence": "high",
    }
    a = models.Attendee(
        event_id=event.id, name=kw.get("name", function),
        email=f"{function.lower()}@x.com",
        role=function, company=kw.get("company", "Acme"),
        fit_score=fit, enrichment=json.dumps(enrichment),
    )
    db.add(a); db.commit()
    return a


# ── intros ─────────────────────────────────────────────────────────────

def test_score_pair_recognizes_function_complement(db, event):
    eng = _attendee(db, event, "Engineering")
    prod = _attendee(db, event, "Product")
    weight, trace = intros.score_pair(eng, prod)
    assert weight > 0
    assert any("function_complement" in t for t in trace)


def test_score_pair_below_min_for_same_function(db, event):
    a = _attendee(db, event, "Engineering", specialty="ML")
    b = _attendee(db, event, "Engineering", specialty="Distributed")
    weight, _ = intros.score_pair(a, b)
    # Same function, different specialties : should be low/zero
    assert weight < intros.MIN_WEIGHT


def test_build_intros_persists_and_is_idempotent(db, event):
    eng = _attendee(db, event, "Engineering")
    prod = _attendee(db, event, "Product")
    founder = _attendee(db, event, "Founder")
    investor = _attendee(db, event, "Investor")
    attendees = [eng, prod, founder, investor]

    built1 = intros.build_intros_for_event(db, event.id, attendees)
    db.commit()
    n1 = len(built1)
    assert n1 > 0

    # Re-run : same answer, no growth (idempotent).
    built2 = intros.build_intros_for_event(db, event.id, attendees)
    db.commit()
    assert len(built2) == n1


def test_export_intro_card_returns_targets(db, event):
    eng = _attendee(db, event, "Engineering")
    prod = _attendee(db, event, "Product")
    intros.build_intros_for_event(db, event.id, [eng, prod])
    db.commit()
    card = intros.export_intro_card(db, event.id, eng.id)
    assert card["attendee_id"] == eng.id
    assert card["intro_count"] >= 1
    assert all(row["method"] == "rule_based" for row in card["intros"])


# ── gap analysis ───────────────────────────────────────────────────────

def test_gap_reports_deficit_when_function_missing(db, event):
    _attendee(db, event, "Engineering")
    _attendee(db, event, "Engineering")
    # Target wants Engineering AND Product; only Engineering present.
    target = {"function": {"Engineering": 0.5, "Product": 0.5}}
    attendees = db.query(models.Attendee).filter(
        models.Attendee.event_id == event.id
    ).all()
    out = gap_analysis.compute_gap(event, attendees, target, headcount_override=10)
    assert out["method"] == "rule_based"
    func_bucket = out["buckets"]["function"]
    # Target is 5 Product : we have 0 of them, so Product is a deficit.
    assert func_bucket["deficit"].get("Product", 0) > 0
    # And we have NO surplus of Product.
    assert "Product" not in func_bucket["surplus"]
    assert any("Product" in s for s in out["ideal_signals_missing"])


def test_gap_with_no_targets_returns_empty(db, event):
    out = gap_analysis.compute_gap(event, [], {})
    assert out["buckets"] == {}
    assert out["summary"] == "no gaps"
