"""Tests for the per-contact MEMORY store (agents/relationship/contact_memory.py
over the ContactFact table). In-memory SQLite, no network."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents.relationship import contact_memory as cm


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


def _contact(db):
    u = models.User(name="Host", email="h@x.com", unipile_account_id="a1")
    db.add(u); db.commit()
    c = models.Contact(user_id=u.id, primary_identity_key="li:sarah", name="Sarah")
    db.add(c); db.commit()
    return u, c


def test_contact_facts_table_exists():
    assert models.ContactFact.__tablename__ == "contact_facts"


def test_upsert_creates_then_updates_in_place(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "based_in", "NYC", source="linkedin")
    cm.upsert_fact(db, u.id, c.id, "based_in", "San Francisco", source="whatsapp")
    rows = cm.get_facts(db, c.id, key="based_in")
    assert len(rows) == 1                      # upserted in place, not duplicated
    assert rows[0].value == "San Francisco"    # latest value wins
    assert rows[0].source == "whatsapp"


def test_same_key_different_dedup_key_coexist(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "interest", "climbing", dedup_key="climbing")
    cm.upsert_fact(db, u.id, c.id, "interest", "jazz", dedup_key="jazz")
    interests = {r.value for r in cm.get_facts(db, c.id, key="interest")}
    assert interests == {"climbing", "jazz"}


def test_high_confidence_filter(db):
    u, c = _contact(db)
    cm.upsert_fact(db, u.id, c.id, "birthday", "03-04", confidence="high")
    cm.upsert_fact(db, u.id, c.id, "works_on", "general", confidence="low")
    all_facts = cm.get_facts(db, c.id)
    high = cm.get_facts(db, c.id, high_confidence_only=True)
    assert len(all_facts) == 2
    assert [r.key for r in high] == ["birthday"]


def test_due_date_hook_is_stored(db):
    """The time-trigger hook is just a stored column for now (no engine yet)."""
    from datetime import datetime, timezone
    u, c = _contact(db)
    due = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cm.upsert_fact(db, u.id, c.id, "upcoming_travel", "SF trip",
                   due_date=due, recurring=False)
    row = cm.get_facts(db, c.id, key="upcoming_travel")[0]
    assert row.due_date is not None
    assert row.recurring is False
