"""
The worklist: does it describe the code that actually exists?

The point of keeping this in code rather than a document is that it cannot
drift, so most of these tests check exactly that -- the registry it reports is
the live one, and "written" and "configured" are two different claims.
"""
from __future__ import annotations

import pytest

from backend import campaigns_races as races
from backend import campaigns_sources as src
from backend import campaigns_states as st


def test_every_toss_up_state_has_a_profile():
    """The fourteen states carrying the toss-ups are the worklist. A state in
    the priority order with no profile is one nobody has looked at and nobody
    will notice."""
    ratings = races.load_ratings()
    priority = {state for state, _ in
                races.priority_states(ratings, today=ratings[0].as_of)}
    assert priority <= set(st.PROFILES)
    assert len(st.PROFILES) == 14


def test_profiles_only_name_real_states():
    assert set(st.PROFILES) <= races.STATES


@pytest.mark.parametrize("state", sorted(st.PROFILES))
def test_every_profile_is_internally_consistent(state):
    prof = st.PROFILES[state]
    assert prof.state == state
    assert prof.publication in ("confirmed", "likely", "assumed", "unknown")
    assert prof.shape in ("pdf", "socrata", "file", "search-form", "unknown")
    assert prof.source_page.startswith("http")
    assert prof.note, "a profile with no note tells the next person nothing"


def test_an_unknown_publication_does_not_claim_a_shape():
    """Writing 'CSV' for a state nobody has checked makes the worklist look
    finished and sends the next person down the wrong path."""
    for prof in st.PROFILES.values():
        if prof.publication == "unknown":
            assert prof.shape == "unknown", prof.state


def test_an_assumed_shape_says_so_in_its_note():
    """'assumed' exists so that wiring an adapter never launders a guess into
    a finding. Every one of them has to admit it in the note."""
    assumed = [p for p in st.PROFILES.values() if p.publication == "assumed"]
    assert assumed, "the level should be in use or removed"
    for prof in assumed:
        assert "not investigated" in prof.note.lower(), prof.state
        assert "assumption" in prof.note.lower(), prof.state


def test_no_assumed_state_is_configured():
    """An assumption must never become a fetch. Their datasets stay empty, so
    the adapter refuses before it can read anything."""
    for prof in st.PROFILES.values():
        if prof.publication == "assumed":
            assert not prof.dataset, prof.state


def test_the_bespoke_states_are_the_confirmed_ones():
    """A state gets its own module once its source is confirmed; the rest ride
    the generic path until someone identifies theirs."""
    for state in st.BESPOKE_ADAPTERS:
        assert st.PROFILES[state].publication == "confirmed", state


# --------------------------------------------------------------------------
# Written is not configured
# --------------------------------------------------------------------------

def test_status_reads_the_live_registry_not_a_hand_kept_field():
    report = st.status()
    src.load_state_adapters()
    assert set(report["written"]) == set(src.STATE_FILING_SOURCES)


def test_written_and_configured_are_different_claims():
    """Four adapters exist; only California knows which document to read. A
    coverage number that conflated them would describe reach, not data."""
    report = st.status()
    assert len(report["written"]) == 14
    assert report["configured"] == ["CA"]
    assert report["reach"]["seats_reachable"] == 234
    assert report["actual"]["seats_reachable"] == 53


def test_configured_is_read_from_each_module_not_duplicated():
    """Filling in a dataset id must change this report with no edit here."""
    from backend import campaigns_pa as pa
    assert st._is_configured("PA") is False
    original = pa.DATASET_ID
    try:
        pa.DATASET_ID = "abcd-1234"
        assert st._is_configured("PA") is True
        assert "PA" in st.status()["configured"]
    finally:
        pa.DATASET_ID = original
    assert st._is_configured("PA") is False


def test_a_state_with_no_adapter_is_not_configured():
    assert st._is_configured("TX") is False
    assert st._is_configured("XX") is False


# --------------------------------------------------------------------------
# The traps, which are the actual value of the list
# --------------------------------------------------------------------------

def test_the_states_that_never_say_state_are_flagged():
    """PA and NY both name their statehouse without the word the shared
    normaliser depends on. Recording that before writing the adapter is the
    difference between a mapped office and a silent misfile."""
    for state in ("PA", "NY"):
        assert st.PROFILES[state].office_trap
        assert "state" in st.PROFILES[state].office_trap.lower()


def test_the_joint_ticket_states_are_flagged():
    for state in ("OH", "AZ"):
        assert "joint ticket" in st.PROFILES[state].quirk.lower()


def test_arizonas_first_lieutenant_governor_is_recorded():
    """New in 2026, so no prior file exists to check the layout against."""
    quirk = st.PROFILES["AZ"].quirk.lower()
    assert "lieutenant governor" in quirk and "2026" in quirk


def test_nebraskas_unicameral_legislature_is_recorded():
    """Expecting a State House row from Nebraska would read as a broken parse."""
    quirk = st.PROFILES["NE"].quirk.lower()
    assert "unicameral" in quirk
    assert "no state house" in st.PROFILES["NE"].office_trap.lower()


# --------------------------------------------------------------------------
# What to do next
# --------------------------------------------------------------------------

def test_next_states_is_empty_now_that_every_state_has_an_adapter():
    """Every priority state is written. What remains is configuring them, which
    unconfigured_states() reports instead."""
    assert st.next_states(limit=20) == []


def test_unconfigured_states_are_ordered_by_seats():
    """Once a dataset is filled in the whole state comes with it, so seats
    order the work -- Texas is 40 seats behind one file."""
    rows = st.unconfigured_states(limit=4)
    # PA and OH are written but equally unconfigured, so they are in the list.
    assert [row["state"] for row in rows] == ["TX", "NY", "PA", "OH"]
    assert [row["seats"] for row in rows] == [40, 27, 18, 16]


def test_unconfigured_states_carry_what_you_need_to_start():
    row = st.unconfigured_states(limit=1)[0]
    assert row["source_page"].startswith("http")
    assert "shape" in row and "publication" in row
    assert row["how"], "each row should say what the next action is"


def test_california_is_not_listed_as_unconfigured():
    assert "CA" not in {r["state"] for r in st.unconfigured_states(limit=20)}


def test_the_worklist_totals_the_toss_up_states():
    report = st.status()
    assert sum(row["seats"] for row in report["states"]) == 234
