"""
The shared filing-parse layer, tested against the shapes state election offices
actually publish rather than the shape one would design.

The name and office cases below are not hypothetical tidiness. "State
Representative" read as U.S. House puts a statehouse candidate in a
congressional pipeline, and a withdrawn row read as live sends email to
somebody who stopped running in June. Both are silent in every downstream view.
"""
from __future__ import annotations

import pytest

from backend import campaigns_filings as f


# --------------------------------------------------------------------------
# Office normalisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("U.S. Representative", "U.S. House"),
    ("US House of Representatives", "U.S. House"),
    ("United States Congress", "U.S. House"),
    ("Congressional District 3", "U.S. House"),
    ("REPRESENTATIVE IN CONGRESS", "U.S. House"),
    ("U.S. Senator", "U.S. Senate"),
    ("United States Senate", "U.S. Senate"),
    ("Governor", "Governor"),
    ("GOVERNOR OF TEXAS", "Governor"),
])
def test_federal_and_statewide_offices_are_recognised(raw, expected):
    assert f.normalize_office(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("State Representative", "State House"),
    ("State Assembly", "State House"),
    ("State Delegate", "State House"),
    ("State Senator", "State Senate"),
    ("STATE SENATE DISTRICT 4", "State Senate"),
])
def test_state_offices_do_not_leak_into_federal_ones(raw, expected):
    """The single most expensive confusion in this data."""
    assert f.normalize_office(raw) == expected


@pytest.mark.parametrize("raw", [
    "County Coroner", "Soil and Water Conservation District Supervisor",
    "School Board Member", "Mosquito Abatement District", "", "   ",
])
def test_offices_we_do_not_handle_return_empty_not_a_guess(raw):
    assert f.normalize_office(raw) == ""


# --------------------------------------------------------------------------
# Districts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("12", "12"), ("District 12", "12"), ("12th", "12"), ("CD-3", "3"),
    ("003", "3"), ("District 03", "3"), ("  7  ", "7"),
    ("Congressional District 14", "14"),
])
def test_district_numbers_are_extracted_and_unpadded(raw, expected):
    assert f.extract_district(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("45A", "45A"), ("HD 45B", "45B"), ("District 12A", "12A"),
])
def test_sub_district_letters_are_kept(raw, expected):
    """In several states 45A and 45B are different seats."""
    assert f.extract_district(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "Statewide", "At-Large", "N/A"])
def test_no_number_means_no_district(raw):
    assert f.extract_district(raw) == ""


def test_ordinal_suffix_is_not_mistaken_for_a_sub_district():
    assert f.extract_district("3rd") == "3"
    assert f.extract_district("21st") == "21"


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("DOE, PATRICIA", "Patricia Doe"),
    ("Doe, Patricia A.", "Patricia A. Doe"),
    ("SMITH, JOHN", "John Smith"),
    ("PATRICIA DOE", "Patricia Doe"),
    ("patricia doe", "Patricia Doe"),
])
def test_surname_first_is_reordered_and_caps_are_fixed(raw, expected):
    assert f.clean_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ('DOE, PATRICIA "PAT"', "Patricia Doe"),
    ("DOE, PATRICIA (PAT)", "Patricia Doe"),
    ("Patricia (Pat) Doe", "Patricia Doe"),
])
def test_nicknames_are_dropped(raw, expected):
    assert f.clean_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("O'BRIEN, MARY", "Mary O'Brien"),
    ("MCDONALD, IAN", "Ian McDonald"),
    ("SMITH-JONES, ANA", "Ana Smith-Jones"),
])
def test_capitalisation_survives_real_surnames(raw, expected):
    assert f.clean_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Doe, Jr.", "Doe Jr."),                    # surname + suffix, no given name
    ("DOE, PATRICIA, III", "Patricia Doe III"),  # suffix as its own comma part
    ("Doe Jr., Patricia", "Patricia Doe Jr."),   # suffix glued to the surname
    ("DOE, PATRICIA A.", "Patricia A. Doe"),
])
def test_a_suffix_does_not_become_the_surname(raw, expected):
    assert f.clean_name(raw) == expected


def test_lowercase_particles_are_capitalised_and_that_is_documented():
    """Not ideal, but predictable, and documented rather than dressed up as a
    correctness this cannot have. Both spellings land in the same place."""
    assert f.clean_name("VAN DER BERG, ANA") == "Ana Van Der Berg"
    assert f.clean_name("Ana van der Berg") == "Ana Van Der Berg"


def test_internally_mixed_case_tokens_are_left_alone():
    """An internal capital can only have been deliberate, so it is trusted;
    an all-lowercase token carries no such signal."""
    assert f.clean_name("Patricia deLuca") == "Patricia deLuca"
    assert f.clean_name("Ana MacDonald") == "Ana MacDonald"


def test_split_name_columns_are_preferred_when_present():
    assert f.clean_name("", first="patricia", last="doe") == "Patricia Doe"
    assert f.clean_name("IGNORED, VALUE", first="Ana", last="Ruiz") == "Ana Ruiz"


def test_mixed_case_from_the_source_is_trusted():
    assert f.clean_name("Patricia deLuca") == "Patricia deLuca"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_name_is_empty(raw):
    assert f.clean_name(raw or "") == ""


# --------------------------------------------------------------------------
# Withdrawals
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    "Withdrawn", "WITHDREW", "Disqualified", "Failed to Qualify",
    "Not Qualified", "superseded", "Deceased", "Removed from ballot",
])
def test_audit_trail_rows_are_recognised(status):
    assert f.is_withdrawn(status)


@pytest.mark.parametrize("status", [
    "Filed", "Qualified", "On Ballot", "Active", "", "Certified",
])
def test_live_candidacies_are_not_withdrawals(status):
    assert not f.is_withdrawn(status)


# --------------------------------------------------------------------------
# from_rows(): the adapter contract
# --------------------------------------------------------------------------

SRC = "https://sos.example.gov/2026/candidates"


def rows(*items) -> list[dict]:
    return list(items)


def test_a_plain_file_parses_into_records():
    found, report = f.from_rows(rows(
        {"name": "DOE, PATRICIA", "office": "U.S. Representative",
         "district": "District 12", "status": "Qualified"},
    ), state="tx", source_url=SRC, found_by="filing:tx")

    assert report.kept == 1 and report.seen == 1
    record = found[0]
    assert record.name == "Patricia Doe"
    assert record.office == "U.S. House"
    assert record.state == "TX"
    assert record.district == "12"
    assert record.status == "qualified"
    assert record.source_url == SRC
    assert record.found_by == "filing:tx"


def test_withdrawn_rows_are_dropped_by_default():
    found, report = f.from_rows(rows(
        {"name": "A Live", "office": "U.S. Representative", "status": "Filed"},
        {"name": "B Gone", "office": "U.S. Representative", "status": "Withdrawn"},
    ), state="TX", source_url=SRC, found_by="filing:tx")

    assert [r.name for r in found] == ["A Live"]
    assert report.dropped_withdrawn == 1


def test_withdrawn_rows_can_be_kept_for_reconciliation():
    found, _ = f.from_rows(rows(
        {"name": "B Gone", "office": "U.S. Representative", "status": "Withdrawn"},
    ), state="TX", source_url=SRC, found_by="filing:tx", keep_withdrawn=True)
    assert len(found) == 1


def test_rows_without_a_checkable_source_are_dropped():
    found, report = f.from_rows(rows(
        {"name": "Pat Doe", "office": "U.S. Representative"},
    ), state="TX", source_url="not-a-url", found_by="filing:tx")
    assert found == [] and report.dropped_no_source == 1


def test_a_row_may_carry_its_own_source_url():
    found, _ = f.from_rows(rows(
        {"name": "Pat Doe", "office": "U.S. Representative",
         "source_url": "https://sos.example.gov/row/1"},
    ), state="TX", source_url=SRC, found_by="filing:tx")
    assert found[0].source_url == "https://sos.example.gov/row/1"


def test_unrecognised_offices_are_dropped_and_counted():
    found, report = f.from_rows(rows(
        {"name": "Pat Doe", "office": "County Coroner"},
    ), state="TX", source_url=SRC, found_by="filing:tx")
    assert found == [] and report.dropped_unknown_office == 1


def test_office_filter_keeps_only_what_was_asked_for():
    found, _ = f.from_rows(rows(
        {"name": "Fed Person", "office": "U.S. Representative"},
        {"name": "State Person", "office": "State Representative"},
    ), state="TX", source_url=SRC, found_by="filing:tx",
        offices=["U.S. House"])
    assert [r.name for r in found] == ["Fed Person"]


def test_statewide_offices_never_carry_a_district():
    """A district on a Senate row is a data error, not a district."""
    found, _ = f.from_rows(rows(
        {"name": "Pat Doe", "office": "U.S. Senator", "district": "3"},
        {"name": "Gov Person", "office": "Governor", "district": "District 1"},
    ), state="TX", source_url=SRC, found_by="filing:tx")
    assert all(r.district == "" for r in found)


def test_column_names_are_matched_case_insensitively():
    found, _ = f.from_rows(rows(
        {"NAME": "Pat Doe", "Office": "U.S. Representative", "DISTRICT": "4"},
    ), state="TX", source_url=SRC, found_by="filing:tx")
    assert found[0].district == "4"


def test_a_field_map_handles_a_states_own_column_names():
    fields = f.FieldMap(name="candidate_full_name", office="office_sought",
                        district="dist_no", status="ballot_status")
    found, _ = f.from_rows(rows(
        {"candidate_full_name": "DOE, PATRICIA", "office_sought": "US HOUSE",
         "dist_no": "007", "ballot_status": "On Ballot"},
    ), state="TX", source_url=SRC, found_by="filing:tx", fields=fields)
    assert found[0].name == "Patricia Doe" and found[0].district == "7"


def test_alternate_column_names_are_tried_in_order():
    fields = f.FieldMap(name=("full_name", "name"))
    found, _ = f.from_rows(rows({"name": "Pat Doe", "office": "Governor"}),
                           state="TX", source_url=SRC, found_by="filing:tx",
                           fields=fields)
    assert found[0].name == "Pat Doe"


def test_the_report_reconciles_against_the_row_count():
    """A count that does not add up is the first sign an adapter has drifted."""
    found, report = f.from_rows(rows(
        {"name": "Keep Me", "office": "U.S. Representative"},
        {"name": "", "office": "U.S. Representative"},
        {"name": "Gone", "office": "U.S. Representative", "status": "Withdrawn"},
        {"name": "Coroner", "office": "County Coroner"},
    ), state="TX", source_url=SRC, found_by="filing:tx")

    assert report.seen == 4 and report.kept == 1 and report.dropped == 3
    assert report.dropped_no_name == 1
    assert report.dropped_withdrawn == 1
    assert report.dropped_unknown_office == 1
    counted = (report.dropped_no_name + report.dropped_no_source
               + report.dropped_unknown_office + report.dropped_withdrawn)
    assert counted == report.dropped
    assert len(found) == report.kept


def test_empty_input_is_an_empty_result_not_an_error():
    found, report = f.from_rows([], state="TX", source_url=SRC, found_by="filing:tx")
    assert found == [] and report.seen == 0 and report.kept == 0


def test_parsed_records_survive_the_dedup_key():
    """Two spellings of one filing must merge rather than double-contact."""
    from backend import campaigns_sources as src
    found, _ = f.from_rows(rows(
        {"name": "DOE, PATRICIA", "office": "U.S. Representative", "district": "03"},
        {"name": "Patricia Doe", "office": "US House", "district": "3"},
    ), state="TX", source_url=SRC, found_by="filing:tx")
    assert len(src.merge(found)) == 1


# --------------------------------------------------------------------------
# override(): the alias-precedence trap
# --------------------------------------------------------------------------

def test_override_clears_the_alias_that_would_have_won():
    """The bug this exists for: an adapter canonicalises the office, sets it
    alongside the raw column, and FieldMap picks the raw one anyway."""
    fields = f.FieldMap(office=("office_name", "office"))
    row = {"office_name": "Representative in the General Assembly",
           "name": "Cy Webb"}

    naive = {**row, "office": "State House"}
    assert f._pick(naive, fields.office) == "Representative in the General Assembly"

    fixed = f.override(row, fields, office="State House")
    assert f._pick(fixed, fields.office) == "State House"
    assert "office_name" not in fixed


def test_override_is_case_insensitive_about_the_alias_it_clears():
    fields = f.FieldMap(name=("Candidate_Name", "name"))
    fixed = f.override({"CANDIDATE_NAME": "Jane Doe and John Roe"},
                       fields, name="Jane Doe")
    assert f._pick(fixed, fields.name) == "Jane Doe"


def test_override_leaves_other_columns_untouched():
    fields = f.FieldMap(office=("office_name", "office"))
    fixed = f.override({"office_name": "x", "district": "7", "status": "Filed"},
                       fields, office="Governor")
    assert fixed["district"] == "7" and fixed["status"] == "Filed"


def test_override_does_not_mutate_the_input_row():
    """Adapters iterate the fetched rows; mutating them corrupts a retry."""
    fields = f.FieldMap(office=("office_name", "office"))
    row = {"office_name": "Representative in Congress"}
    f.override(row, fields, office="U.S. House")
    assert row == {"office_name": "Representative in Congress"}


def test_override_handles_several_fields_at_once():
    fields = f.FieldMap(name=("full_name", "name"), notes=("notes",))
    fixed = f.override({"full_name": "A and B"}, fields,
                       name="A", notes="running mate: B")
    assert fixed["name"] == "A" and fixed["notes"] == "running mate: B"
    assert "full_name" not in fixed
