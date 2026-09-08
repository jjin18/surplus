"""
Arizona: the ordinary parse, plus the joint ticket that is new in 2026.

The joint-ticket tests carry the weight. Arizona elects a lieutenant governor
for the first time this November, so there is no prior cycle's file to check the
gubernatorial rows against, and a name cell holding "A and B" produces a record
that is present, plausible and matches nothing.
"""
from __future__ import annotations

import pytest

from backend import campaigns_az as az
from backend import campaigns_races as races
from backend import campaigns_sources as src
from backend import campaigns_tabular as tab


FIXTURE = """
STATE OF ARIZONA
2026 General Election

UNITED STATES REPRESENTATIVE

Congressional District 1

Name                     Party
Dana Ruiz                Republican
Ellis Brand              Democratic

Congressional District 6

Juan Ciscomani-Vega      Republican
Kirsten Engel-Ward       Democratic

GOVERNOR AND LIEUTENANT GOVERNOR

Katie Hobbes and Marco Silva      Democratic
Karrin Taylor and Dana Webb       Republican

STATE REPRESENTATIVE

Legislative District 4

Sam Ortega               Democratic

MINE INSPECTOR

Paul Marsh               Republican
"""


def parse(text: str = FIXTURE, **kw):
    return az.parse_candidate_list(text, **kw)


# --------------------------------------------------------------------------
# Office wording
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("United States Representative", "U.S. House"),
    ("Representative in Congress", "U.S. House"),
    ("U.S. Senator", "U.S. Senate"),
    ("State Representative", "State House"),
    ("State Senator", "State Senate"),
])
def test_office_wording_maps(raw, expected):
    assert az.map_office(raw) == expected


def test_the_new_joint_office_beats_the_shorter_governor_key():
    """New in 2026, and 'governor' is a substring of it."""
    assert az.map_office("Governor and Lieutenant Governor") == "Governor"
    assert az.map_office("GOVERNOR") == "Governor"


@pytest.mark.parametrize("raw", ["Mine Inspector", "Corporation Commissioner",
                                 "Attorney General", "", "  "])
def test_offices_we_do_not_pursue_are_dropped(raw):
    assert az.map_office(raw) == ""


# --------------------------------------------------------------------------
# The parse
# --------------------------------------------------------------------------

def test_candidates_land_under_their_office_and_district():
    by_name = {r.name: r for r in parse().records}
    assert by_name["Dana Ruiz"].office == "U.S. House"
    assert by_name["Dana Ruiz"].district == "1"
    assert by_name["Kirsten Engel-Ward"].district == "6"


def test_a_new_office_resets_the_district():
    governors = [r for r in parse().records if r.office == "Governor"]
    assert governors and all(r.district == "" for r in governors)


def test_legislative_districts_are_read_for_state_offices():
    sam = next(r for r in parse().records if r.name == "Sam Ortega")
    assert sam.office == "State House" and sam.district == "4"


def test_unpursued_offices_do_not_appear():
    assert "Paul Marsh" not in {r.name for r in parse().records}


def test_records_carry_state_and_provenance():
    for record in parse().records:
        assert record.state == "AZ"
        assert record.found_by == "filing:az"
        assert record.source_url.startswith("http")


# --------------------------------------------------------------------------
# The 2026 joint ticket
# --------------------------------------------------------------------------

def test_a_joint_ticket_keeps_the_head_and_records_the_mate():
    governors = {r.name: r for r in parse().records if r.office == "Governor"}
    assert set(governors) == {"Katie Hobbes", "Karrin Taylor"}
    assert "Marco Silva" in governors["Katie Hobbes"].notes
    assert "Dana Webb" in governors["Karrin Taylor"].notes


def test_a_joint_name_would_otherwise_match_nothing():
    """The failure being prevented: a record that is present and useless."""
    glued = "Katie Hobbes and Marco Silva"
    kept = next(r for r in parse().records if r.office == "Governor"
                and r.name == "Katie Hobbes")
    assert kept.name != glued
    assert kept.identity() != src.CandidateRecord(
        name=glued, office="Governor", state="AZ").identity()


def test_house_candidates_are_never_ticket_split():
    """Only the joint executive offices split; a hyphenated surname elsewhere
    must survive."""
    house = [r for r in parse().records if r.office == "U.S. House"]
    assert "Juan Ciscomani-Vega" in {r.name for r in house}
    assert all("running mate" not in (r.notes or "") for r in house)


# --------------------------------------------------------------------------
# Failing loudly
# --------------------------------------------------------------------------

def test_an_empty_document_raises():
    with pytest.raises(az.LayoutError, match="empty document"):
        parse("")


def test_an_unrecognised_document_raises():
    with pytest.raises(az.LayoutError, match="did not look like"):
        parse("404 Not Found\nNothing here.\n")


def test_non_strict_mode_returns_the_failure_for_probe():
    assert parse("nothing at all", strict=False).records == []


def test_a_thin_parse_is_caught_against_the_nine_districts():
    with pytest.raises(az.LayoutError, match="only 2 of 9"):
        az.arizona("AZ", text=FIXTURE)


def test_a_full_parse_passes_the_seat_check():
    records = az.arizona("AZ", text=_full_ballot())
    districts = {r.district for r in records if r.office == "U.S. House"}
    assert len(districts) == races.HOUSE_SEATS["AZ"] == 9


def _full_ballot() -> str:
    lines = ["STATE OF ARIZONA", "", "UNITED STATES REPRESENTATIVE", ""]
    for n in range(1, 10):
        tag = chr(ord("a") + n)
        lines += [f"Congressional District {n}", "",
                  f"Ada {tag.upper()}son            Democratic",
                  f"Bo {tag.upper()}wright          Republican", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Not configured
# --------------------------------------------------------------------------

def test_the_document_url_is_unset_and_refuses_with_a_reason():
    """The primary file is not the general file: reading it would return
    people who lost in July."""
    assert az.DOCUMENT_URL == ""
    with pytest.raises(tab.NotConfigured, match="lost in July"):
        az.arizona("AZ")


def test_probe_reports_the_unset_document_rather_than_raising():
    result = az.probe()
    assert result["ok"] is False and result["stage"] == "fetch"
    assert result["document_url"] == "(unset)"


def test_probe_counts_the_joint_tickets_it_split():
    result = az.probe(text=FIXTURE)
    assert result["ok"] is True
    assert result["joint_tickets_split"] == 2
    assert result["house_districts_expected"] == 9


def test_the_adapter_ignores_other_states():
    assert az.arizona("CA", text=FIXTURE) == []


def test_the_adapter_is_registered():
    src.load_state_adapters()
    assert src.STATE_FILING_SOURCES["AZ"] is az.arizona
