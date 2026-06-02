"""
Tests for the cookie-driven Unipile reconnect path.

PR #52 attempted this with the wrong request body shape (sent
create-only fields like `providers` / `notify_url` / `name` on a
reconnect call); Unipile 4xx'd and blocked sign-in entirely. This file
covers the body-builder helpers in isolation so the shape stays right.

No FastAPI app spin-up : same Python-3.9 workaround test_followups uses.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest
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


def _request(cookie_value=None, session_token=None):
    from backend.auth import SESSION_COOKIE
    cookies = {}
    if cookie_value:
        cookies["surplus_last_account"] = cookie_value
    if session_token:
        cookies[SESSION_COOKIE] = session_token
    req = MagicMock()
    req.cookies = cookies
    return req


def _seed_session(db, user, token="sess-tok"):
    """Create a live (non-revoked, unexpired) Session for `user`."""
    from datetime import datetime, timedelta, timezone
    db.add(models.Session(
        session_token=token, user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7)))
    db.commit()
    return token


# ── _create_body shape ────────────────────────────────────────────────

def test_create_body_has_all_create_fields():
    from backend.routes.auth import _create_body
    body = _create_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
    )
    assert body["type"] == "create"
    assert body["providers"] == ["LINKEDIN"]
    assert "notify_url" in body
    assert body["name"] == "state-1"
    assert "success_redirect_url" in body
    assert "failure_redirect_url" in body


# ── _reconnect_body shape (the core PR #52 bug) ───────────────────────

def test_reconnect_body_uses_correct_field_name():
    """Field is `reconnect_account` per Unipile docs."""
    from backend.routes.auth import _reconnect_body
    body = _reconnect_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
        account_id="acct-abc",
    )
    assert body["type"] == "reconnect"
    assert body["reconnect_account"] == "acct-abc"


def test_reconnect_body_omits_create_only_fields():
    """The PR #52 bug : sending `providers` / `notify_url` / `name` on a
    reconnect call made Unipile 4xx. They must NOT be in the body."""
    from backend.routes.auth import _reconnect_body
    body = _reconnect_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
        account_id="acct-abc",
    )
    assert "providers" not in body
    assert "notify_url" not in body
    assert "name" not in body


def test_reconnect_body_keeps_redirect_urls():
    """We still need success_redirect_url so the user comes back to us
    after Unipile re-auths the account."""
    from backend.routes.auth import _reconnect_body
    body = _reconnect_body(
        dsn="https://api.example", expires="2026-12-31T00:00:00Z",
        state_token="state-1", base="https://www.surpluslayer.com",
        failure_url="https://www.surpluslayer.com/signin?error=x",
        account_id="acct-abc",
    )
    assert "success_redirect_url" in body
    assert "failure_redirect_url" in body


# ── _resolve_returning_user ───────────────────────────────────────────

def test_no_cookie_means_no_returning_user(db):
    from backend.routes.auth import _resolve_returning_user
    assert _resolve_returning_user(_request(None), db) is None


def test_cookie_pointing_at_unknown_account_returns_none(db):
    """Stale cookie (DB reset, user revoked Unipile externally) shouldn't
    crash : caller falls back to create."""
    from backend.routes.auth import _resolve_returning_user
    assert _resolve_returning_user(_request("never-existed"), db) is None


def test_cookie_pointing_at_existing_user_returns_user(db):
    from backend.routes.auth import _resolve_returning_user
    db.add(models.User(unipile_account_id="acct-abc", name="Daniel"))
    db.commit()
    user = _resolve_returning_user(_request("acct-abc"), db)
    assert user is not None
    assert user.unipile_account_id == "acct-abc"


# ── session fallback : the orphaning-prevention fix ───────────────────

def test_session_fallback_finds_logged_in_user_without_cookie(db):
    """A logged-in operator re-connecting from a browser that lost the
    LAST_ACCOUNT_COOKIE must still resolve to their existing row (so we
    reconnect / re-point onto it) instead of looking brand-new -> create
    -> orphaned events. This is the core recurrence fix."""
    from backend.routes.auth import _resolve_returning_user
    u = models.User(unipile_account_id="acct-live", name="Jia")
    db.add(u); db.commit()
    tok = _seed_session(db, u)
    # No LAST_ACCOUNT_COOKIE at all, only a session.
    user = _resolve_returning_user(_request(None, session_token=tok), db)
    assert user is not None
    assert user.id == u.id


def test_session_fallback_skips_user_without_unipile_account(db):
    """A triage / email-only user has no account to reconnect to : must
    fall through to create, not attempt a bogus reconnect."""
    from backend.routes.auth import _resolve_returning_user
    u = models.User(unipile_account_id=None, name="Triage user")
    db.add(u); db.commit()
    tok = _seed_session(db, u)
    assert _resolve_returning_user(_request(None, session_token=tok), db) is None


def test_cookie_takes_precedence_over_session(db):
    from backend.routes.auth import _resolve_returning_user
    cookie_user = models.User(unipile_account_id="acct-cookie", name="ByCookie")
    sess_user = models.User(unipile_account_id="acct-sess", name="BySession")
    db.add_all([cookie_user, sess_user]); db.commit()
    tok = _seed_session(db, sess_user)
    user = _resolve_returning_user(_request("acct-cookie", session_token=tok), db)
    assert user.unipile_account_id == "acct-cookie"


def test_stale_cookie_falls_through_to_session(db):
    """Cookie points at a deleted account (no matching row) but the user is
    still logged in : resolve via session, not None."""
    from backend.routes.auth import _resolve_returning_user
    u = models.User(unipile_account_id="acct-live", name="Jia")
    db.add(u); db.commit()
    tok = _seed_session(db, u)
    user = _resolve_returning_user(
        _request("deleted-acct", session_token=tok), db)
    assert user is not None
    assert user.id == u.id


# ── cookie / TTL constants sanity ─────────────────────────────────────

def test_cookie_constant_outlasts_session():
    from backend.auth import (
        LAST_ACCOUNT_COOKIE, LAST_ACCOUNT_TTL_DAYS, SESSION_TTL_DAYS,
    )
    assert LAST_ACCOUNT_COOKIE == "surplus_last_account"
    assert LAST_ACCOUNT_TTL_DAYS > SESSION_TTL_DAYS
