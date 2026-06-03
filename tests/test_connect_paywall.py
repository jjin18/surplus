"""
Tests for the PAY-AT-CONNECT paywall, tied to the LinkedIn identity.

Model: running Exa / prospecting is free with no LinkedIn. The LinkedIn
round-trip (/linkedin/start) always proceeds; payment is enforced in
/linkedin/callback AFTER the LinkedIn identity is known, keyed on the
deduped user's paid_at. Because dedup matches on provider_id/public_id/
email, paid_at is portable across browsers/devices : a LinkedIn that paid
once is recognized everywhere and skips Stripe. The in-person host stays
free.

We can't cheaply drive the async callback (Unipile + AuthState), so we test
the two load-bearing pieces directly:
  - user_has_paid : the predicate the callback branches on.
  - build_checkout_url : tags the checkout with user.id so the webhook
    stamps THIS row (the recovery from a re-charge would be a mistag).
  - grant_paid : by-email recovery for payments Stripe confirms but the DB
    lost (webhook miss or DB reset).

Style mirrors test_demo_paywall.py : in-memory session, direct calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.auth import user_has_paid
from backend.db import Base
from backend.routes.admin import GrantPaidIn, grant_paid
from backend.routes.billing import build_checkout_url


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
                    linkedin_provider_id="ACoAA_paid",
                    paid_at=datetime.now(timezone.utc))
    db.add(u); db.commit(); db.refresh(u)
    return u


def _unpaid(db):
    u = models.User(name="Free", email="free@example.com",
                    linkedin_provider_id="ACoAA_free")
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── the predicate the callback pay-gate branches on ─────────────────────

def test_paid_linkedin_skips_checkout(db):
    """A LinkedIn that paid once : user_has_paid True everywhere (the row is
    found by dedup on any browser), so the callback skips Stripe."""
    assert user_has_paid(_paid(db)) is True


def test_unpaid_linkedin_is_routed_to_checkout(db):
    assert user_has_paid(_unpaid(db)) is False


# ── checkout url is tagged with the user's id (payment-link mode) ────────

def test_build_checkout_url_tags_client_reference_id(db, monkeypatch):
    monkeypatch.setenv("STRIPE_PAYMENT_LINK", "https://buy.stripe.com/test_abc")
    monkeypatch.delenv("STRIPE_PRICE_ID", raising=False)
    u = _unpaid(db)
    url = build_checkout_url(request=None, db=db, user=u)  # request unused in link mode
    q = parse_qs(urlparse(url).query)
    assert q["client_reference_id"] == [str(u.id)]
    # Real email is prefilled so the webhook + Checkout form line up.
    assert q.get("prefilled_email") == ["free@example.com"]


def test_build_checkout_url_omits_placeholder_email(db, monkeypatch):
    monkeypatch.setenv("STRIPE_PAYMENT_LINK", "https://buy.stripe.com/test_abc")
    u = models.User(name="Anon", email="prepay-xyz@anonymous.surplus")
    db.add(u); db.commit(); db.refresh(u)
    url = build_checkout_url(request=None, db=db, user=u)
    q = parse_qs(urlparse(url).query)
    assert q["client_reference_id"] == [str(u.id)]
    assert "prefilled_email" not in q  # never ship placeholder emails to Stripe


# ── by-email recovery : payment Stripe confirms but DB lost ─────────────

def test_grant_paid_stamps_unpaid_user(db):
    u = _unpaid(db)
    assert u.paid_at is None
    out = grant_paid(GrantPaidIn(email="free@example.com"), db, None)
    assert out["ok"] is True and out["already_paid"] is False
    assert out["user_id"] == u.id
    db.refresh(u)
    assert u.paid_at is not None


def test_grant_paid_is_idempotent(db):
    u = _paid(db)
    first = u.paid_at
    out = grant_paid(GrantPaidIn(email="paid@example.com"), db, None)
    assert out["already_paid"] is True
    db.refresh(u)
    assert u.paid_at == first


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
