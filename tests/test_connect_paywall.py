"""
Tests for the PAY-FIRST connect-time paywall.

The product gates LinkedIn-connect (which is also sign-in) behind payment:
an anonymous or unpaid caller is bounced to Stripe BEFORE they can connect.
The in-person host keeps connect free (covered by the in-person send-gate
tests); here we exercise the helper + the by-email recovery endpoint.

Style mirrors test_demo_paywall.py : call functions directly with an
in-memory session, no TestClient/auth plumbing.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.auth import require_paid_to_connect_linkedin
from backend.db import Base
from backend.routes.admin import GrantPaidIn, grant_paid


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


def _paid(db):
    u = models.User(name="Paid", email="paid@example.com",
                    paid_at=datetime.now(timezone.utc))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _unpaid(db):
    u = models.User(name="Free", email="free@example.com")
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── connect-time gate : pay-first ───────────────────────────────────────

def test_connect_gate_blocks_anonymous():
    """No session at all : bounced to Stripe (the front-door paywall)."""
    with pytest.raises(HTTPException) as ei:
        require_paid_to_connect_linkedin(None)
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "payment_required"


def test_connect_gate_blocks_unpaid_user(db):
    with pytest.raises(HTTPException) as ei:
        require_paid_to_connect_linkedin(_unpaid(db))
    assert ei.value.status_code == 402
    assert ei.value.detail["code"] == "payment_required"


def test_connect_gate_allows_paid_user(db):
    assert require_paid_to_connect_linkedin(_paid(db)) is None


# ── by-email recovery endpoint : payment Stripe confirms but DB lost ─────

def test_grant_paid_stamps_unpaid_user(db):
    u = _unpaid(db)
    assert u.paid_at is None
    out = grant_paid(GrantPaidIn(email="free@example.com"), db, None)
    assert out["ok"] is True
    assert out["already_paid"] is False
    assert out["user_id"] == u.id
    db.refresh(u)
    assert u.paid_at is not None


def test_grant_paid_is_idempotent(db):
    u = _paid(db)
    first = u.paid_at
    out = grant_paid(GrantPaidIn(email="paid@example.com"), db, None)
    assert out["already_paid"] is True
    db.refresh(u)
    assert u.paid_at == first  # unchanged


def test_grant_paid_is_case_insensitive(db):
    u = _unpaid(db)
    out = grant_paid(GrantPaidIn(email="FREE@Example.com"), db, None)
    assert out["user_id"] == u.id
    db.refresh(u)
    assert u.paid_at is not None


def test_grant_paid_404_for_unknown_email(db):
    with pytest.raises(HTTPException) as ei:
        grant_paid(GrantPaidIn(email="nobody@example.com"), db, None)
    assert ei.value.status_code == 404
