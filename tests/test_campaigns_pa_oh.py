"""
Pennsylvania and Ohio: the office wording that would silently misfile half a
statehouse, and the refusal to run on a dataset nobody has identified.

Both adapters ship with placeholder dataset locations. The tests that matter
most here are the ones asserting they REFUSE rather than guess: a placeholder
that 404s is recoverable, and a guessed id that resolves to the previous
cycle's file returns a list of people who are not running, which nothing
downstream can detect.
"""
from __future__ import annotations

import pytest

from backend import campaigns_oh as oh
from backend import campaigns_pa as pa
from backend import campaigns_races as races
from backend import campaigns_sources as src
from backend import campaigns_tabular as tab


# --------------------------------------------------------------------------
# Pennsylvania office wording
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Representative in Congress", "U.S. House"),
    ("REPRESENTATIVE IN CONGRESS", "U.S. House"),
    ("Representative in Congress 12", "U.S. House"),
    ("United States Senator", "U.S. Senate"),
    ("Governor", "Governor"),
])
def test_pa_federal_and_statewide_offices(raw, expected):
    assert pa.map_office(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Representative in the General Assembly", "State House"),
    ("Senator in the General Assembly", "State Senate"),
    ("SENATOR IN THE GENERAL ASSEMBLY 42", "State Senate"),
])
def test_pa_general_assembly_is_not_congress(raw, expected):
    """Pennsylvania never says "state", so the shared normaliser would file
    every state representative in the commonwealth as a U.S. House candidate.
    This mapping is the only thing preventing that."""
    assert pa.map_office(raw) == expected


def test_pa_longest_wording_wins():
    """'senator in the general assembly' must not lose to a shorter key."""
    assert pa.map_office("Senator in the General Assembly") == "State Senate"


@pytest.mark.parametrize("raw", ["County Coroner", "Prothonotary", "", "   "])
def test_pa_drops_offices_it_does_not_pursue(raw):
    assert pa.map_office(raw) == ""


# --------------------------------------------------------------------------
# Ohio office wording and the joint ticket
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Representative to Congress", "U.S. House"),
    ("U.S. Senator", "U.S. Senate"),
    ("State Representative", "State House"),
    ("State Senator", "State Senate"),
])
def test_oh_office_wording(raw, expected):
    assert oh.map_office(raw) == expected


def test_oh_joint_ticket_office_beats_the_shorter_governor_key():
    """'governor' is a substring of 'governor and lieutenant governor'; the
    longest-key ordering is what keeps them apart."""
    assert oh.map_office("Governor and Lieutenant Governor") == "Governor"
    assert oh.map_office("Governor") == "Governor"


@pytest.mark.parametrize("raw,head,mate", [
    ("Jane Doe and John Roe", "Jane Doe", "John Roe"),
    ("Jane Doe & John Roe", "Jane Doe", "John Roe"),
    ("Jane Doe / John Roe", "Jane Doe", "John Roe"),
    ("Jane Doe AND John Roe", "Jane Doe", "John Roe"),
])
def test_oh_joint_ticket_names_split(raw, head, mate):
    assert oh.split_ticket(raw) == (head, mate)


@pytest.mark.parametrize("raw", [
    "Alexander Anderson",       # 'and' inside a word must not split
    "Andrea Sanders",
    "Fernando Castillo",
])
def test_oh_a_name_containing_and_is_not_split(raw):
    assert oh.split_ticket(raw) == (raw, "")


def test_oh_empty_name_splits_to_nothing():
    assert oh.split_ticket("") == ("", "")
    assert oh.split_ticket("   ") == ("", "")


def test_oh_running_mate_is_recorded_not_glued_into_the_name():
    rows = [{"candidate_name": "Jane Doe and John Roe",
             "office": "Governor and Lieutenant Governor", "status": "certified"}]
    records = oh.ohio("OH", rows=rows)
    assert len(records) == 1
    assert records[0].name == "Jane Doe"
    assert "John Roe" in records[0].notes
    assert records[0].office == "Governor"


def test_oh_a_non_governor_name_is_left_alone():
    rows = [{"candidate_name": "Andrea Sanders",
             "office": "Representative to Congress", "district": "9"}]
    records = oh.ohio("OH", rows=rows)
    assert records[0].name == "Andrea Sanders"
    assert "running mate" not in records[0].notes


# --------------------------------------------------------------------------
# Refusing to run unconfigured
# --------------------------------------------------------------------------

def test_pa_refuses_without_a_dataset_id_and_names_the_fix():
    assert pa.DATASET_ID == "", "a real id should update this test too"
    with pytest.raises(tab.NotConfigured, match="discover"):
        pa.pennsylvania("PA")


def test_oh_refuses_without_a_download_url_and_names_the_fix():
    assert oh.DOWNLOAD_URL == ""
    with pytest.raises(tab.NotConfigured, match="ohiosos.gov"):
        oh.ohio("OH")


def test_not_configured_is_distinguishable_from_an_outage():
    """Different problems, different fixes: one is a missing constant, the
    other is the state's server."""
    assert issubclass(tab.NotConfigured, tab.SourceError)
    assert not issubclass(tab.SourceError, tab.NotConfigured)


@pytest.mark.parametrize("adapter,state", [(pa.pennsylvania, "PA"), (oh.ohio, "OH")])
def test_adapters_ignore_other_states_without_fetching(adapter, state):
    """Wrong state must short-circuit BEFORE the unconfigured check, or gather()
    across 50 states would raise on every one of them."""
    assert adapter("XX") == []


# --------------------------------------------------------------------------
# Parsing rows, once rows exist
# --------------------------------------------------------------------------

PA_ROWS = [
    {"candidate_name": "Ada Keller", "office_name": "Representative in Congress",
     "district": "7", "status": "Certified"},
    {"candidate_name": "Bo Marsh", "office_name": "Governor", "status": "Certified"},
    {"candidate_name": "Cy Webb",
     "office_name": "Representative in the General Assembly", "district": "101"},
    {"candidate_name": "Withdrawn Person", "office_name": "Representative in Congress",
     "district": "7", "status": "Withdrawn"},
    {"candidate_name": "Not Ours", "office_name": "County Coroner"},
]


def test_pa_rows_parse_into_records():
    records = pa.pennsylvania("PA", rows=PA_ROWS)
    by_name = {r.name: r for r in records}
    assert by_name["Ada Keller"].office == "U.S. House"
    assert by_name["Ada Keller"].district == "7"
    assert by_name["Bo Marsh"].office == "Governor"
    assert by_name["Bo Marsh"].district == ""
    assert by_name["Cy Webb"].office == "State House"


def test_pa_withdrawn_and_unpursued_offices_are_dropped():
    names = {r.name for r in pa.pennsylvania("PA", rows=PA_ROWS)}
    assert "Withdrawn Person" not in names
    assert "Not Ours" not in names


def test_pa_records_carry_state_and_provenance():
    for record in pa.pennsylvania("PA", rows=PA_ROWS):
        assert record.state == "PA"
        assert record.found_by == "filing:pa"
        assert record.source_url.startswith("http")


def test_pa_column_mismatch_raises_rather_than_returning_empty():
    """Rows fetched but none kept is a FIELDS problem, not an empty ballot."""
    wrong = [{"totally": "different", "columns": "here"} for _ in range(5)]
    with pytest.raises(tab.NotConfigured, match="column names"):
        pa.pennsylvania("PA", rows=wrong)


def test_oh_column_mismatch_raises_too():
    wrong = [{"nope": "x"} for _ in range(3)]
    with pytest.raises(tab.NotConfigured, match="column names"):
        oh.ohio("OH", rows=wrong)


def test_an_empty_dataset_is_not_treated_as_a_mismatch():
    """No rows at all is a different thing from rows that would not map."""
    assert pa.pennsylvania("PA", rows=[]) == []
    assert oh.ohio("OH", rows=[]) == []


def test_office_filter_narrows_without_tripping_the_mismatch_check():
    records = pa.pennsylvania("PA", office="Governor", rows=PA_ROWS)
    assert {r.office for r in records} == {"Governor"}


# --------------------------------------------------------------------------
# probe()
# --------------------------------------------------------------------------

def test_pa_probe_reports_the_unset_dataset_instead_of_raising():
    result = pa.probe()
    assert result["ok"] is False and result["stage"] == "fetch"
    assert result["dataset_id"] == "(unset)"
    assert "NotConfigured" in str(result["error"])


def test_oh_probe_reports_the_unset_url_instead_of_raising():
    result = oh.probe()
    assert result["ok"] is False
    assert result["download_url"] == "(unset)"


def test_pa_probe_reports_the_real_columns_and_offices():
    result = pa.probe(rows=PA_ROWS)
    assert result["ok"] is True
    assert "candidate_name" in result["columns"]
    assert "Representative in Congress" in result["offices_in_data"]
    assert "U.S. House" in result["offices_mapped"]
    assert result["house_districts_expected"] == 17


def test_probe_survives_a_parse_failure_and_says_why():
    result = pa.probe(rows=[{"wrong": "shape"}] * 3)
    assert result["ok"] is False
    assert "NotConfigured" in result["parse_error"]
    assert result["sample_rows"]


# --------------------------------------------------------------------------
# Registry and coverage
# --------------------------------------------------------------------------

def test_both_adapters_are_registered():
    src.load_state_adapters()
    assert src.STATE_FILING_SOURCES["PA"] is pa.pennsylvania
    assert src.STATE_FILING_SOURCES["OH"] is oh.ohio


def test_three_states_now_cover_eighty_seven_seats():
    src.load_state_adapters()
    cov = races.coverage(src.STATE_FILING_SOURCES.keys())
    assert cov["states_covered"] == ["CA", "OH", "PA"]
    assert cov["seats_by_office"]["U.S. House"] == 52 + 15 + 17
    assert cov["seats_by_office"]["Governor"] == 3
    assert cov["seats_by_office"]["U.S. Senate"] == 0   # none are Class 2
    assert cov["seats_reachable"] == 87


def test_gather_does_not_die_when_one_state_is_unconfigured():
    """PA and OH raise NotConfigured until someone fills in a dataset. That
    must be one failed backend in LAST_RUN, not a dead run."""
    src._RESULT_CACHE.clear()
    found = src.gather("PA", incumbent_backends={})
    assert found == []
    assert "NotConfigured" in str(src.LAST_RUN["backends"]["filing:PA"])
