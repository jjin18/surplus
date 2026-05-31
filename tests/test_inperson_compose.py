"""
In-person connection-note composition (event.surpluslayer.com flow).

These exercise the deterministic template branch (_compose_inperson_template via
compose()), which is what runs offline / without an API key. With no
ANTHROPIC_API_KEY in the test env, compose() falls back to the template, so we
get stable strings to assert on. The goal: a BRIEF invite that LEADS with the
specific thing you talked about (the "fun fact"), not a generic template.
"""
from __future__ import annotations

import os
from types import SimpleNamespace as N

import pytest

from backend.agents.outreach import compose


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    # Force the deterministic template path so assertions are stable.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OUTREACH_COMPOSE_DISABLE", "1")
    yield


def _p(name="Maya Rodriguez", note=None):
    return N(name=name, note=note, role=None, company=None,
             works_on=None, offers=None, headline=None, linkedin_url=None)


def _ev(label="SF Mixer", city=None):
    return N(kind="in_person", label=label, city=city,
             event_name=None, format=None, user=None)


def test_topic_note_leads_with_the_callback():
    # A topic-style note ("the Ottawa bagel spot") slots after "about" and the
    # invite stays brief (well under LinkedIn's 300-char cap).
    d = compose(_p(note="the Ottawa bagel spot"), _ev(label="SF Mixer"))
    assert "chatting about the Ottawa bagel spot" in d.note
    assert "SF Mixer" in d.note
    assert len(d.note) <= 300


def test_fact_note_reads_as_you_are():
    # A preposition-led "fact" note ("from Ottawa") becomes a "love that you're …"
    # callback instead of the awkward "chatting about from Ottawa", in both the
    # connection note and the post-accept DM.
    d = compose(_p(note="from Ottawa"), _ev(label="LinkedIn Local"))
    assert "love that you're from Ottawa" in d.note
    assert "chatting about from Ottawa" not in d.note
    assert "Love that you're from Ottawa" in d.message


def test_conversational_leadin_is_stripped():
    # "we talked about X" must not double up into "chatting about we talked about".
    d = compose(_p(note="we talked about rock climbing"), _ev(label="YC Day"))
    assert "chatting about rock climbing" in d.note
    assert "we talked about" not in d.note


def test_no_note_stays_generic_and_brief():
    d = compose(_p(note=None), _ev(label="Web Summit"))
    assert "Web Summit" in d.note
    assert "love that you're" not in d.note.lower()
    assert "chatting about" not in d.note.lower()
    assert len(d.note) <= 300
