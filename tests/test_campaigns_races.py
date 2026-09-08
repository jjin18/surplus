"""
The 2026 ballot: arithmetic that catches a typo in the schedule tables, and the
staleness rule that stops a target list being built on an old map.

The apportionment and class-membership tests are worth more than they look. A
single wrong digit in HOUSE_SEATS is invisible in every downstream output --
it just means one district is never contacted -- and the only place it can be
caught is against the totals the Constitution and the census fix.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend import campaigns_races as r


TODAY = date(2026, 9, 8)


# --------------------------------------------------------------------------
# Schedule facts: the arithmetic guards
# --------------------------------------------------------------------------

def test_house_apportionment_sums_to_435():
    assert sum(r.HOUSE_SEATS.values()) == 435


def test_all_fifty_states_present_exactly_once():
    assert len(r.HOUSE_SEATS) == 50
    assert len(set(r.HOUSE_SEATS)) == 50


def test_every_state_has_at_least_one_seat():
    assert all(n >= 1 for n in r.HOUSE_SEATS.values())


def test_senate_class_2_is_thirty_three_seats():
    """Class 2 is 33 of the 100 seats; specials are events, not schedule."""
    assert len(r.SENATE_CLASS_2) == 33


def test_thirty_six_states_elect_a_governor():
    assert len(r.GOVERNOR_2026) == 36


def test_schedule_tables_only_contain_real_states():
    for table in (r.SENATE_CLASS_2, r.GOVERNOR_2026, r.LEGISLATIVE_2026):
        assert table <= r.STATES


def test_four_states_run_legislative_elections_in_odd_years():
    assert r.STATES - r.LEGISLATIVE_2026 == {"LA", "MS", "NJ", "VA"}
    assert len(r.LEGISLATIVE_2026) == 46


# --------------------------------------------------------------------------
# The ballot
# --------------------------------------------------------------------------

def test_national_ballot_totals():
    totals = r.ballot_totals()
    assert totals["house"] == 435
    assert totals["senate"] == 33
    assert totals["governor"] == 36
    assert totals["total"] == 504


def test_a_states_seats_are_its_districts_plus_any_statewide_races():
    # Texas: 38 districts, a Class 2 Senate seat, and a governorship.
    seats = r.seats_for_state("TX")
    assert sum(1 for s in seats if s.office == "U.S. House") == 38
    assert any(s.office == "U.S. Senate" for s in seats)
    assert any(s.office == "Governor" for s in seats)
    assert len(seats) == 40


def test_a_state_with_no_statewide_race_gets_only_districts():
    # Florida: 28 districts, no Class 2 seat, no 2026 governor race.
    assert "FL" not in r.SENATE_CLASS_2 and "FL" in r.GOVERNOR_2026
    seats = r.seats_for_state("FL")
    assert sum(1 for s in seats if s.office == "U.S. House") == 28
    assert not any(s.office == "U.S. Senate" for s in seats)


def test_at_large_states_number_their_single_district():
    """An at-large district is '1' at the filing office, not blank -- a blank
    would silently fail to join against a state's own candidate rows."""
    for state in ("AK", "DE", "ND", "SD", "VT", "WY"):
        house = [s for s in r.seats_for_state(state) if s.office == "U.S. House"]
        assert [s.district for s in house] == ["1"], state


def test_unknown_state_yields_no_seats():
    assert r.seats_for_state("XX") == []
    assert r.seats_for_state("") == []


def test_specials_are_injected_not_hardcoded():
    """A resignation is an event; the schedule tables must not need editing."""
    special = r.Seat("FL", "U.S. Senate", is_special=True)
    seats = r.seats_for_state("FL", specials=[special])
    assert special in seats
    assert r.ballot_totals(specials=[special])["senate"] == 34


def test_seat_keys_are_unique_across_the_national_ballot():
    seats = r.all_seats()
    assert len({s.key for s in seats}) == len(seats)


def test_a_special_does_not_collide_with_the_regular_seat():
    regular = r.Seat("GA", "U.S. Senate")
    special = r.Seat("GA", "U.S. Senate", is_special=True)
    assert regular.key != special.key


# --------------------------------------------------------------------------
# Ratings: injected, dated, and allowed to go stale
# --------------------------------------------------------------------------

def rating(state, band="toss-up", days_old=1, district="1") -> r.RaceRating:
    return r.RaceRating(state=state, office="U.S. House", district=district,
                        band=band, source="test",
                        as_of=TODAY - timedelta(days=days_old))


def test_priority_is_derived_from_injected_ratings():
    ratings = [rating("AZ", district="1"), rating("AZ", district="6"),
               rating("ME", district="2")]
    assert r.priority_states(ratings, today=TODAY) == [("AZ", 2), ("ME", 1)]


def test_undated_ratings_are_not_current():
    undated = r.RaceRating(state="AZ", office="U.S. House", band="toss-up")
    assert r.priority_states([undated], today=TODAY) == []


def test_stale_ratings_are_ignored():
    assert r.priority_states([rating("AZ", days_old=200)], today=TODAY) == []
    assert r.priority_states([rating("AZ", days_old=200)], today=TODAY,
                             stale_after_days=365) == [("AZ", 1)]


def test_future_dated_ratings_are_rejected():
    ahead = r.RaceRating(state="AZ", office="U.S. House", band="toss-up",
                         as_of=TODAY + timedelta(days=5))
    assert r.priority_states([ahead], today=TODAY) == []


def test_band_selection_is_what_makes_the_list_18_or_50():
    """The same ratings give a different target list depending on the band --
    which is exactly why this is a parameter and not a constant."""
    ratings = [rating("AZ", band="toss-up"), rating("NY", band="lean"),
               rating("TX", band="likely")]
    assert r.priority_states(ratings, today=TODAY) == [("AZ", 1)]
    wide = r.priority_states(ratings, today=TODAY,
                             bands=("toss-up", "lean", "likely"))
    assert {state for state, _ in wide} == {"AZ", "NY", "TX"}


def test_band_matching_is_case_and_spelling_tolerant():
    for spelling in ("Toss-Up", "TOSSUP", " toss up "):
        assert r.priority_states([rating("AZ", band=spelling)],
                                 today=TODAY) == [("AZ", 1)]


def test_ties_break_on_delegation_size_then_name():
    ratings = [rating("CA"), rating("ME"), rating("NE")]
    # One race each: CA (52 seats) leads, then ME and NE (2 and 3) by size.
    assert r.priority_states(ratings, today=TODAY) == [
        ("CA", 1), ("NE", 1), ("ME", 1)]


def test_ratings_for_unknown_states_are_dropped():
    assert r.priority_states([rating("XX")], today=TODAY) == []


# --------------------------------------------------------------------------
# Coverage: the honest denominator
# --------------------------------------------------------------------------

def test_no_filing_sources_means_no_coverage():
    cov = r.coverage([])
    assert cov["state_count"] == 0
    assert cov["seats_reachable"] == 0
    assert cov["pct_federal_and_gubernatorial"] == 0.0


def test_coverage_counts_the_seats_a_state_actually_has():
    cov = r.coverage(["TX"])
    assert cov["seats_reachable"] == 40
    assert cov["seats_by_office"]["U.S. House"] == 38
    assert cov["seats_by_office"]["Governor"] == 1


def test_full_coverage_is_the_whole_ballot():
    cov = r.coverage(r.STATES)
    assert cov["seats_reachable"] == cov["seats_total"] == 504
    assert cov["pct_federal_and_gubernatorial"] == 100.0


def test_legislative_gap_is_reported_separately_not_blended():
    """'40% of the midterm' must not be able to mean two different things."""
    cov = r.coverage(["TX", "CA"])
    assert cov["legislative_seat_counts_known"] is False
    assert cov["legislative_states_covered"] == 2
    assert cov["legislative_states_total"] == 46
    # The headline percentage is explicitly scoped to what is counted.
    assert "federal_and_gubernatorial" in "".join(cov.keys())


def test_coverage_ignores_states_that_do_not_exist():
    assert r.coverage(["TX", "XX", ""])["states_covered"] == ["TX"]


@pytest.mark.parametrize("state", ["tx", "Tx", " TX "])
def test_coverage_normalises_state_input(state):
    assert r.coverage([state.strip()])["states_covered"] == ["TX"]


# --------------------------------------------------------------------------
# The shipped ratings snapshot
# --------------------------------------------------------------------------

def test_shipped_ratings_snapshot_loads_and_is_dated():
    ratings = r.load_ratings()
    assert ratings, "the ratings snapshot did not load"
    assert all(rating.as_of is not None for rating in ratings)
    assert all(rating.state in r.STATES for rating in ratings)


def test_shipped_snapshot_is_the_eighteen_toss_ups_across_fourteen_states():
    """The spec assumed ~50 competitive races; the toss-up band is 18. The
    difference is the band, which is why it is a parameter."""
    ratings = r.load_ratings()
    assert len(ratings) == 18
    assert len({rating.state for rating in ratings}) == 14


def test_priority_order_from_the_shipped_snapshot():
    ratings = r.load_ratings()
    as_of = ratings[0].as_of
    order = r.priority_states(ratings, today=as_of)
    # Four states carry two toss-ups each; CA leads them on delegation size.
    assert [state for state, _ in order][:4] == ["CA", "PA", "OH", "AZ"]
    assert dict(order)["CA"] == 2
    assert len(order) == 14
    assert sum(count for _, count in order) == 18


def test_the_snapshot_ages_out_on_its_own():
    from datetime import timedelta
    ratings = r.load_ratings()
    long_after = ratings[0].as_of + timedelta(days=400)
    assert r.priority_states(ratings, today=long_after) == []


def test_a_missing_ratings_file_is_an_empty_list_not_a_crash():
    import pathlib
    assert r.load_ratings(pathlib.Path("/nonexistent/ratings.json")) == []
