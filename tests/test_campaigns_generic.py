"""
The profile-driven adapter, and the two states it was written for.

Texas and New York are the biggest remaining prizes at 40 and 27 seats, and New
York is the one with a real trap in it: fusion voting means a candidate holds
several rows legitimately, so the extra rows are not malformed and nothing
flags them. A naive parse reports half again as many candidates as are running.
"""
from __future__ import annotations

import pytest

from backend import campaigns_generic as gen
from backend import campaigns_sources as src
from backend import campaigns_states as st
from backend import campaigns_tabular as tab


# --------------------------------------------------------------------------
# Office mapping
# --------------------------------------------------------------------------

def test_longest_wording_wins():
    names = {"governor": "Governor",
             "governor and lieutenant governor": "Governor",
             "senator in the general assembly": "State Senate"}
    assert gen.map_office("Governor and Lieutenant Governor", names) == "Governor"
    assert gen.map_office("Senator in the General Assembly", names) == "State Senate"


def test_an_unmapped_wording_drops_the_row_rather_than_guessing():
    assert gen.map_office("County Coroner", {"governor": "Governor"}) == ""
    assert gen.map_office("", {"governor": "Governor"}) == ""


# --------------------------------------------------------------------------
# New York: the statehouse that never says "state"
# --------------------------------------------------------------------------

def test_member_of_assembly_is_the_state_house_not_congress():
    """Pennsylvania's trap exactly. Unpinned, every New York assembly
    candidate would file as a candidate for the U.S. House."""
    names = st.PROFILES["NY"].office_names
    assert gen.map_office("Member of Assembly", names) == "State House"
    assert gen.map_office("Representative in Congress", names) == "U.S. House"


def test_ny_assembly_rows_parse_as_state_house_end_to_end():
    profile = st.PROFILES["NY"]
    records, _ = gen.build_records(profile, [
        {"name": "Ada Keller", "office": "Member of Assembly", "district": "104"},
        {"name": "Bo Marsh", "office": "Representative in Congress", "district": "19"},
    ])
    by_name = {r.name: r for r in records}
    assert by_name["Ada Keller"].office == "State House"
    assert by_name["Bo Marsh"].office == "U.S. House"


# --------------------------------------------------------------------------
# New York: fusion voting
# --------------------------------------------------------------------------

FUSION_ROWS = [
    {"name": "Ada Keller", "office": "Representative in Congress",
     "district": "19", "party": "Democratic"},
    {"name": "Ada Keller", "office": "Representative in Congress",
     "district": "19", "party": "Working Families"},
    {"name": "Ada Keller", "office": "Representative in Congress",
     "district": "19", "party": "Conservative"},
    {"name": "Bo Marsh", "office": "Representative in Congress",
     "district": "19", "party": "Republican"},
]


def test_fusion_rows_collapse_to_one_candidate_each():
    records, stats = gen.build_records(st.PROFILES["NY"], FUSION_ROWS)
    assert sorted(r.name for r in records) == ["Ada Keller", "Bo Marsh"]
    assert stats["fusion_collapsed"] == 2


def test_the_fusion_collapse_is_reported_not_silent():
    """Four rows in, two candidates out, and the report says where the other
    two went -- otherwise the count looks like a thin parse."""
    _, stats = gen.build_records(st.PROFILES["NY"], FUSION_ROWS)
    assert stats["seen"] == 4 and stats["kept"] == 4
    assert stats["fusion_collapsed"] == 2


def test_fusion_is_off_everywhere_else():
    """Outside a fusion state two rows for one seat IS a bug, and merging them
    silently would hide it."""
    assert st.PROFILES["NY"].fusion_voting is True
    for state in ("TX", "NC", "MI", "WA", "CO", "IA", "NE", "NM", "ME"):
        assert st.PROFILES[state].fusion_voting is False, state

    records, stats = gen.build_records(st.PROFILES["TX"], [
        {"name": "Ada Keller", "office": "State Representative", "district": "9"},
        {"name": "Ada Keller", "office": "State Representative", "district": "9"},
    ])
    assert len(records) == 2                 # left visible, not merged away
    assert stats["fusion_collapsed"] == 0


def test_fusion_does_not_merge_two_different_people():
    records, _ = gen.build_records(st.PROFILES["NY"], FUSION_ROWS)
    assert len({r.name for r in records}) == 2


# --------------------------------------------------------------------------
# North Carolina and Nebraska
# --------------------------------------------------------------------------

def test_nc_uses_its_abbreviation_where_the_shared_rule_wants_the_word_state():
    names = st.PROFILES["NC"].office_names
    assert gen.map_office("NC House of Representatives", names) == "State House"
    assert gen.map_office("NC Senate", names) == "State Senate"
    assert gen.map_office("US House of Representatives", names) == "U.S. House"


def test_nebraska_maps_its_only_chamber_to_state_senate():
    names = st.PROFILES["NE"].office_names
    assert gen.map_office("State Senator", names) == "State Senate"
    assert gen.map_office("Member of the Legislature", names) == "State Senate"


def test_nebraska_has_no_state_house_wording_at_all():
    """Waiting for a State House row from a unicameral legislature would read
    as a thin parse forever."""
    assert "State House" not in set(st.PROFILES["NE"].office_names.values())


# --------------------------------------------------------------------------
# The joint ticket, applied unconditionally
# --------------------------------------------------------------------------

def test_a_governor_row_with_two_names_splits_in_any_state():
    for state in ("NY", "MI", "CO", "IA", "NE", "NM"):
        records, _ = gen.build_records(st.PROFILES[state], [
            {"name": "Jane Doe and John Roe", "office": "Governor"}])
        assert records[0].name == "Jane Doe", state
        assert "John Roe" in records[0].notes, state


def test_a_single_name_governor_row_is_untouched():
    """Texas elects its lieutenant governor separately and Maine has none, so
    splitting is a no-op there -- which is why it is applied unconditionally
    rather than from a list of states somebody has to keep correct."""
    for state in ("TX", "ME"):
        records, _ = gen.build_records(st.PROFILES[state], [
            {"name": "Greg Sanders", "office": "Governor"}])
        assert records[0].name == "Greg Sanders", state
        assert "running mate" not in (records[0].notes or ""), state


def test_a_house_row_is_never_ticket_split():
    records, _ = gen.build_records(st.PROFILES["TX"], [
        {"name": "Alexander Anderson", "office": "United States Representative",
         "district": "7"}])
    assert records[0].name == "Alexander Anderson"


# --------------------------------------------------------------------------
# Refusing to run unconfigured
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["TX", "NY", "NC", "MI", "WA", "CO",
                                   "IA", "NE", "NM", "ME"])
def test_every_profile_state_refuses_until_its_dataset_is_set(state):
    assert st.PROFILES[state].dataset == ""
    adapter = src.STATE_FILING_SOURCES[state]
    with pytest.raises(tab.NotConfigured):
        adapter(state)


def test_a_socrata_state_is_told_to_search_the_catalogue():
    with pytest.raises(tab.NotConfigured, match="discover"):
        src.STATE_FILING_SOURCES["NY"]("NY")


def test_a_file_state_is_pointed_at_its_source_page():
    with pytest.raises(tab.NotConfigured, match="sos.state.tx.us"):
        src.STATE_FILING_SOURCES["TX"]("TX")


def test_discover_refuses_for_a_state_with_no_catalogue():
    with pytest.raises(tab.NotConfigured, match="no catalogue"):
        gen.discover("TX")


def test_adapters_ignore_other_states_before_checking_configuration():
    """Otherwise gather() across the country raises on every unconfigured
    state instead of skipping them."""
    for state in ("TX", "NY"):
        assert src.STATE_FILING_SOURCES[state]("XX") == []


def test_a_column_mismatch_raises_rather_than_returning_empty():
    adapter = src.STATE_FILING_SOURCES["TX"]
    with pytest.raises(tab.NotConfigured, match="column names"):
        adapter("TX", rows=[{"totally": "wrong"} for _ in range(4)])


def test_an_empty_file_is_not_a_mismatch():
    assert src.STATE_FILING_SOURCES["TX"]("TX", rows=[]) == []


# --------------------------------------------------------------------------
# probe()
# --------------------------------------------------------------------------

def test_probe_reports_the_unset_dataset_without_raising():
    result = gen.probe("TX")
    assert result["ok"] is False and result["stage"] == "fetch"
    assert result["dataset"] == "(unset)"
    assert result["state"] == "TX"


def test_probe_names_the_wordings_it_could_not_map():
    """The first real run should say exactly which offices are unaccounted
    for, rather than leaving a thin result to be puzzled over."""
    result = gen.probe("TX", rows=[
        {"name": "Ada Keller", "office": "United States Representative", "district": "7"},
        {"name": "Bo Marsh", "office": "Railroad Commissioner"},
    ])
    assert result["ok"] is True
    assert "Railroad Commissioner" in result["offices_unmapped"]
    assert "U.S. House" in result["offices_mapped"]
    assert result["house_districts_expected"] == 38


def test_probe_reports_the_fusion_collapse_for_new_york():
    result = gen.probe("NY", rows=FUSION_ROWS)
    assert result["report"]["fusion_collapsed"] == 2
    assert result["candidates"] == 2


# --------------------------------------------------------------------------
# Registry and coverage
# --------------------------------------------------------------------------

def test_all_fourteen_priority_states_have_an_adapter():
    src.load_state_adapters()
    assert set(src.STATE_FILING_SOURCES) == set(st.PROFILES)
    assert len(src.STATE_FILING_SOURCES) == 14


def test_bespoke_states_keep_their_own_modules():
    from backend import campaigns_ca, campaigns_az, campaigns_oh, campaigns_pa
    src.load_state_adapters()
    assert src.STATE_FILING_SOURCES["CA"] is campaigns_ca.california
    assert src.STATE_FILING_SOURCES["AZ"] is campaigns_az.arizona
    assert src.STATE_FILING_SOURCES["PA"] is campaigns_pa.pennsylvania
    assert src.STATE_FILING_SOURCES["OH"] is campaigns_oh.ohio


def test_reach_is_now_every_toss_up_state():
    report = st.status()
    assert report["reach"]["seats_reachable"] == 234
    assert report["configured"] == ["CA"]
    assert report["actual"]["seats_reachable"] == 53


def test_gather_survives_a_state_that_is_not_configured():
    src._RESULT_CACHE.clear()
    assert src.gather("TX", incumbent_backends={}) == []
    assert "NotConfigured" in str(src.LAST_RUN["backends"]["filing:TX"])
