"""
Tests for the admin user-merge recovery path (routes/admin.py).

Context: a LinkedIn re-auth can mint a NEW Unipile account + a NEW User row
when dedup misses, orphaning the operator's Events under the old user_id
(get_owned_event then 404s). /admin/merge-users re-points every FK from the
orphaned row onto the survivor and deletes the source.

Direct-function tests with an in-memory SQLAlchemy session : same pattern as
test_followups.py (avoids importing backend.main / schemas.py, which don't
parse on Python 3.9).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.routes.admin import MergeUsersBody, merge_users, lookup_users


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


def _seed_orphan_and_survivor(db):
    """Old row (orphan): owns the events, NULL provider id, has billing.
    New row (survivor): live account, has provider id, no billing yet."""
    now = datetime.now(timezone.utc)
    old = models.User(
        unipile_account_id="old_deleted_acct",
        name="Jia (old)", email="jia@example.com",
        linkedin_provider_id=None,  # the NULL that broke dedup
        paid_at=now - timedelta(days=30),
        stripe_customer_id="cus_123",
    )
    new = models.User(
        unipile_account_id="new_live_acct",
        name="Jia", email="jia@example.com",
        linkedin_provider_id="ACoAA_jia_stable",
    )
    db.add_all([old, new]); db.flush()

    # Old row owns an event + the things hanging off the relationship layer.
    ev = models.Event(
        role="founders", seniority="Senior", co_stage="Seed", headcount=30,
        format="Mixer", city="NYC", goal="connect", budget=0, threshold=60,
        user_id=old.id,
    )
    db.add(ev)
    db.add(models.Contact(
        user_id=old.id, primary_identity_key="li:someone", name="Someone"))
    db.add(models.RelationshipInteraction(
        actor_user_id=old.id, source_type="manual",
        interaction_type="note", occurred_at=now))
    db.add(models.Session(
        session_token="sess_old", user_id=old.id,
        expires_at=now + timedelta(days=1)))
    db.commit()
    return old, new


def test_dry_run_moves_nothing(db):
    old, new = _seed_orphan_and_survivor(db)
    res = merge_users(MergeUsersBody(
        from_user_id=old.id, to_user_id=new.id, dry_run=True), db=db, _=None)

    assert res["dry_run"] is True
    assert res["would_move"]["events"] == 1
    assert res["would_copy_billing"] is True
    # Nothing actually moved.
    assert db.query(models.Event).filter(
        models.Event.user_id == old.id).count() == 1
    assert db.get(models.User, old.id) is not None


def test_commit_repoints_all_fks_and_deletes_source(db):
    old, new = _seed_orphan_and_survivor(db)
    res = merge_users(MergeUsersBody(
        from_user_id=old.id, to_user_id=new.id, dry_run=False), db=db, _=None)

    assert res["dry_run"] is False
    assert res["moved"]["events"] == 1

    # Every FK now points at the survivor; none remain on the source.
    for q in (
        db.query(models.Event).filter(models.Event.user_id == old.id),
        db.query(models.Contact).filter(models.Contact.user_id == old.id),
        db.query(models.RelationshipInteraction).filter(
            models.RelationshipInteraction.actor_user_id == old.id),
        db.query(models.Session).filter(models.Session.user_id == old.id),
    ):
        assert q.count() == 0
    assert db.query(models.Event).filter(
        models.Event.user_id == new.id).count() == 1

    # Source row is gone; survivor inherited billing.
    assert db.get(models.User, old.id) is None
    survivor = db.get(models.User, new.id)
    assert survivor.paid_at is not None
    assert survivor.stripe_customer_id == "cus_123"


def test_does_not_clobber_existing_survivor_billing(db):
    old, new = _seed_orphan_and_survivor(db)
    new.paid_at = datetime.now(timezone.utc)
    new.stripe_customer_id = "cus_survivor"
    db.commit()

    merge_users(MergeUsersBody(
        from_user_id=old.id, to_user_id=new.id, dry_run=False), db=db, _=None)

    survivor = db.get(models.User, new.id)
    assert survivor.stripe_customer_id == "cus_survivor"


def test_rejects_self_merge(db):
    old, _new = _seed_orphan_and_survivor(db)
    with pytest.raises(Exception):
        merge_users(MergeUsersBody(
            from_user_id=old.id, to_user_id=old.id, dry_run=True),
            db=db, _=None)


def test_lookup_filters_by_identity(db):
    _seed_orphan_and_survivor(db)
    res = lookup_users(identity="ACoAA_jia_stable", db=db, _=None)
    assert res["count"] == 1
    assert res["users"][0]["unipile_account_id"] == "new_live_acct"
