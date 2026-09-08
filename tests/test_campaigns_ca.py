"""
California's certified list: the parse, and the two ways it is allowed to fail.

The fixtures here encode what the certified list is BELIEVED to look like; the
2026 PDF has not been read from this environment. That is deliberate and it is
the point of the split -- these tests hold the parsing logic to account today,
and when the real document turns out to differ, the fixture is corrected and
they go on holding it to account. What they must never do is pass because the
parser quietly returned nothing.
"""
from __future__ import annotations

import pytest

from backend import campaigns_ca as ca
from backend import campaigns_races as races
from backend import campaigns_sources as src


# A slice of the layout: two congressional districts, a statewide office, an
# office we do not pursue, and the page furniture that has to be ignored.
FIXTURE = """
CERTIFIED LIST OF CANDIDATES
Secretary of State
November 3, 2026, General Election
Page 1

UNITED STATES REPRESENTATIVE

1st Congressional District

Name                          Party Preference        Ballot Designation
Doug Ramirez                  Republican              Farmer/Business Owner
Audrey Chen                   Democratic              Educator

2nd Congressional District

Jared Huffman-Lee             Democratic              Member of Congress
Chris O'Neill                 Republican              Small Business Owner

GOVERNOR

Katie Porter Smith            Democratic              Law Professor
Steve Hilton Jones            Republican              Author/Entrepreneur

INSURANCE COMMISSIONER

Ricardo Lara Ruiz             Democratic              Insurance Commissioner

MEMBER OF THE STATE ASSEMBLY

5th Assembly District

Ana Delgado                   Democratic              Council Member
"""


def parse(text: str = FIXTURE, **kw):
    return ca.parse_certified_list(text, **kw)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_candidates_are_found_under_their_office_and_district():
    result = parse()
    by_name = {r.name: r for r in result.records}

    assert by_name["Doug Ramirez"].office == "U.S. House"
    assert by_name["Doug Ramirez"].district == "1"
    assert by_name["Audrey Chen"].district == "1"
    assert by_name["Chris O'Neill"].district == "2"


def test_a_new_office_heading_resets_the_district():
    """Otherwise the governor inherits '2nd Congressional District'."""
    governor = [r for r in parse().records if r.office == "Governor"]
    assert governor and all(r.district == "" for r in governor)


def test_state_assembly_maps_to_state_house():
    result = parse()
    ana = next(r for r in result.records if r.name == "Ana Delgado")
    assert ana.office == "State House"
    assert ana.district == "5"


def test_offices_we_do_not_pursue_are_dropped_not_bucketed():
    names = {r.name for r in parse().records}
    assert "Ricardo Lara Ruiz" not in names


def test_page_furniture_is_not_read_as_candidates():
    names = {r.name for r in parse().records}
    for noise in ("Certified List Of Candidates", "Secretary Of State", "Name"):
        assert noise not in names


def test_every_record_carries_a_checkable_source():
    assert all(r.source_url.startswith("http") for r in parse().records)
    assert all(r.found_by == "filing:ca" for r in parse().records)
    assert all(r.state == "CA" for r in parse().records)


def test_status_records_that_these_are_certified_for_the_ballot():
    assert {r.status for r in parse().records} == {"certified"}


# --------------------------------------------------------------------------
# Party is captured only to be discarded
# --------------------------------------------------------------------------

def test_party_never_reaches_the_record():
    """campaigns_score.Campaign has no party field by construction; an adapter
    that carried party would be the thing that eventually defeats that."""
    for record in parse().records:
        blob = " ".join(str(v).lower() for v in vars(record).values())
        for party in ("democratic", "republican"):
            assert party not in blob, f"{record.name} carries party: {blob}"


def test_candidate_record_still_has_no_party_field():
    assert "party" not in src.CandidateRecord.__dataclass_fields__


# --------------------------------------------------------------------------
# Failing loudly
# --------------------------------------------------------------------------

def test_an_empty_document_raises_rather_than_returning_nothing():
    with pytest.raises(ca.LayoutError, match="empty document"):
        parse("")


def test_a_document_that_is_not_the_certified_list_raises():
    with pytest.raises(ca.LayoutError, match="did not look like"):
        parse("404 Not Found\nThe page you requested does not exist.\n")


def test_an_html_error_page_does_not_parse_as_candidates():
    with pytest.raises(ca.LayoutError):
        parse("<html><body><h1>Service Unavailable</h1></body></html>")


def test_non_strict_mode_returns_the_failure_instead_of_raising():
    """probe() needs to read a failed parse, not be stopped by it."""
    result = parse("nothing here at all", strict=False)
    assert result.records == []


def test_a_thin_parse_is_caught_by_the_seat_count():
    """Candidates in 2 of 52 districts is a broken parse, not a quiet ballot --
    and no exception inside the parser would ever notice."""
    with pytest.raises(ca.LayoutError, match="only 2 of 52"):
        ca.california("CA", text=FIXTURE)


def test_a_full_parse_passes_the_seat_count_check():
    full = _synthetic_full_ballot()
    records = ca.california("CA", text=full)
    districts = {r.district for r in records if r.office == "U.S. House"}
    assert len(districts) == races.HOUSE_SEATS["CA"] == 52


def _alpha(n: int) -> str:
    """Digits as letters. Candidate names must not contain digits -- real ones
    do not, and the name pattern rejects them, so a fixture using "Candidate1"
    tests nothing except that the parser skipped it."""
    return "".join(chr(ord("a") + int(d)) for d in str(n))


def _synthetic_full_ballot() -> str:
    """All 52 districts, two candidates each, in the believed layout."""
    lines = ["CERTIFIED LIST OF CANDIDATES", "", "UNITED STATES REPRESENTATIVE", ""]
    for n in range(1, 53):
        tag = _alpha(n)
        lines += [f"{n}th Congressional District", "",
                  f"Ada {tag.capitalize()}son          Democratic     Educator",
                  f"Bo {tag.capitalize()}wright        Republican     Business Owner", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The adapter contract
# --------------------------------------------------------------------------

def test_the_adapter_ignores_other_states():
    assert ca.california("TX", text=FIXTURE) == []
    assert ca.california("", text=FIXTURE) == []


def test_office_filter_narrows_the_result():
    records = ca.california("CA", office="Governor", text=FIXTURE)
    assert records and {r.office for r in records} == {"Governor"}


def test_the_adapter_is_registered_for_california():
    assert "CA" in src.STATE_FILING_SOURCES
    assert src.STATE_FILING_SOURCES["CA"] is ca.california


def test_gather_reaches_the_adapter_through_the_registry():
    src._RESULT_CACHE.clear()
    found = src.gather("CA", filing_sources={"CA": lambda s, o: ca.california(
        s, o, text=_synthetic_full_ballot())}, incumbent_backends={})
    assert found and all(r.state == "CA" for r in found)
    assert src.LAST_RUN["backends"]["filing:CA"] == len(found)


def test_records_deduplicate_against_a_roster_row():
    """A certified-list row must beat the GovTrack row for the same person."""
    src._RESULT_CACHE.clear()
    # parse directly: the FIXTURE is a two-district slice, which california()
    # correctly rejects as a thin parse.
    filing = ca.parse_certified_list(FIXTURE).records[0]
    roster = src.CandidateRecord(name=filing.name, office=filing.office,
                                 state="CA", district=filing.district,
                                 status="incumbent", found_by="govtrack",
                                 source_url="https://govtrack.example/1")
    merged = src.merge([roster, filing])
    assert len(merged) == 1 and merged[0].found_by == "filing:ca"


# --------------------------------------------------------------------------
# Fetch and probe: the network half, driven by an injected fetcher
# --------------------------------------------------------------------------

def test_fetch_uses_the_injected_fetcher_and_never_the_network(monkeypatch):
    seen: list[str] = []

    def fake_fetch(url: str) -> bytes:
        seen.append(url)
        return b"%PDF-fake"

    monkeypatch.setattr(ca, "extract_pdf_text", lambda data: FIXTURE)
    text = ca.fetch_certified_list(fetcher=fake_fetch)
    assert seen == [ca.CERTIFIED_LIST_URL]
    assert "UNITED STATES REPRESENTATIVE" in text


def test_probe_reports_a_fetch_failure_instead_of_raising():
    def broken(url: str) -> bytes:
        raise RuntimeError("connection refused")

    result = ca.probe(fetcher=broken)
    assert result["ok"] is False and result["stage"] == "fetch"
    assert "RuntimeError" in str(result["error"])


def test_probe_reports_a_layout_failure_with_the_text_to_debug_it(monkeypatch):
    monkeypatch.setattr(ca, "extract_pdf_text",
                        lambda data: "SOME UNEXPECTED DOCUMENT\nwith lines\n")
    result = ca.probe(fetcher=lambda url: b"x")
    assert result["ok"] is False and result["stage"] == "parse"
    assert result["candidates"] == 0
    assert result["first_lines"][0] == "SOME UNEXPECTED DOCUMENT"
    assert result["house_districts_expected"] == 52


def test_probe_reports_success_and_the_district_count(monkeypatch):
    monkeypatch.setattr(ca, "extract_pdf_text",
                        lambda data: _synthetic_full_ballot())
    result = ca.probe(fetcher=lambda url: b"x")
    assert result["ok"] is True
    assert result["house_districts_found"] == 52


def test_missing_pypdf_is_a_clear_error_not_an_import_crash(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_pypdf(name, *args, **kw):
        if name == "pypdf":
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pypdf)
    with pytest.raises(ca.LayoutError, match="pypdf is required"):
        ca.extract_pdf_text(b"%PDF-")


# --------------------------------------------------------------------------
# Coverage now that one state is wired
# --------------------------------------------------------------------------

def test_california_alone_covers_fifty_three_seats():
    cov = races.coverage(["CA"])
    assert cov["states_covered"] == ["CA"]
    assert cov["seats_by_office"]["U.S. House"] == 52
    assert cov["seats_by_office"]["Governor"] == 1
    assert cov["seats_by_office"]["U.S. Senate"] == 0   # CA is not Class 2
    assert cov["seats_reachable"] == 53


def test_filing_coverage_now_reports_challenger_coverage():
    coverage = src.filing_coverage()
    assert coverage["has_challenger_coverage"] is True
    assert "CA" in coverage["states_with_filing_source"]
