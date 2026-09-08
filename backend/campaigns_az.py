"""campaigns_az.py : Arizona — the Secretary of State's candidate list.

ARIZONA IS FOURTH: two of the eighteen toss-ups, 9 congressional districts plus
a governorship, 10 of the 504 federal and gubernatorial seats. No Senate race --
Arizona's seats are Class 1 and Class 3, so neither is on the 2026 ballot.

-----------------------------------------------------------------------------
THE THING THAT IS NEW THIS CYCLE
-----------------------------------------------------------------------------
Arizona elects a LIEUTENANT GOVERNOR FOR THE FIRST TIME on November 3, 2026,
on a joint ticket with the governor. Proposition 131 created the office in
2022; before it, Arizona was one of five states without one. Gubernatorial
nominees had to name a running mate by September 4, 2026.

That matters here for a boring reason with an unboring failure mode: the office
will read "Governor and Lieutenant Governor" (or similar) and the name cell may
carry two people. Left alone, "Katie Hobbs and Someone Else" is a candidate
name that matches no campaign, no search and no dedup key -- a row that is
present and useless, which is worse than a row that is missing, because nothing
counts it as absent. campaigns_filings.split_ticket handles it, and it is
shared rather than local precisely because Ohio has had this for decades and
Arizona is simply the newest instance.

Because 2026 is the first cycle, there is NO PRIOR YEAR'S FILE to check the
layout against. Every other state has last cycle's document to look at; Arizona's
gubernatorial rows are new in shape this year. Treat the office wording below
as the least-confirmed thing in this module and check it first when the parse
comes back thin.

-----------------------------------------------------------------------------
FORMAT: A PDF, LIKE CALIFORNIA
-----------------------------------------------------------------------------
Arizona publishes candidate filings as PDFs on azsos.gov (the primary-ballot
list for 2026 was "2026-Candidate-Nominations-and-Petitions-Filed-0330.pdf",
the March 30 filing deadline). So this reuses California's shape: a pure
parse_candidate_list() over extracted text, a thin injectable fetcher, and a
loud failure when the layout does not match.

DOCUMENT_URL is unset. The general-election list is published after the July 21
primary and its URL is not the primary-ballot one above, so pointing at that
file would return the primary field -- people who lost in July and are not on
the November ballot. That is the "quietly wrong" failure this package refuses
to make, so the adapter raises NotConfigured until someone sets the real URL:

    https://azsos.gov/elections/candidates  ->  the 2026 General Election
    certified candidate list. Set DOCUMENT_URL, run probe(), correct LAYOUT.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .campaigns_filings import FieldMap, ParseReport, from_rows, split_ticket
from .campaigns_races import HOUSE_SEATS
from .campaigns_sources import CandidateRecord, register_filing_source
from .campaigns_tabular import Fetcher, NotConfigured

STATE = "AZ"

# UNSET on purpose: see the module docstring. The primary-ballot file is not
# the general-election file, and reading it would return July's losers.
DOCUMENT_URL = ""

SOURCE_PAGE = "https://azsos.gov/elections/candidates"


# ---------------------------------------------------------------------------
# LAYOUT : the unverified half. Arizona's list is organised as office headings
# with candidates beneath, districts repeated per office. Same family as
# California's, and the same rule applies -- if the real document disagrees,
# this block changes and nothing below it does.
# ---------------------------------------------------------------------------

_OFFICE_HEADING = re.compile(
    r"^\s*(?P<office>"
    r"(?:UNITED\s+STATES|U\.?\s?S\.?)\s+(?:REPRESENTATIVE|SENATOR)"
    r"|REPRESENTATIVE\s+IN\s+CONGRESS"
    r"|GOVERNOR(?:\s+AND\s+LIEUTENANT\s+GOVERNOR)?"
    r"|STATE\s+SENATOR?"
    r"|STATE\s+REPRESENTATIVE"
    r"|SECRETARY\s+OF\s+STATE|ATTORNEY\s+GENERAL"
    r"|STATE\s+TREASURER|SUPERINTENDENT\s+OF\s+PUBLIC\s+INSTRUCTION"
    r"|MINE\s+INSPECTOR|CORPORATION\s+COMMISSIONER"
    r")\s*$",
    re.IGNORECASE,
)

# Arizona numbers congressional and legislative districts the same way, and
# uses one legislative district number for both chambers.
_DISTRICT_HEADING = re.compile(
    r"^\s*(?:(?P<kind>Congressional|Legislative)\s+)?District\s+(?P<num>\d+)\s*$"
    r"|^\s*(?P<num2>\d+)(?:st|nd|rd|th)\s+(?P<kind2>Congressional|Legislative)\s+District\s*$",
    re.IGNORECASE,
)

# Name, then party and any other column, separated by the whitespace runs a PDF
# leaves. The name must not swallow the gap : see campaigns_ca for the bug that
# rule prevents.
_CANDIDATE_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z.'\-]*(?: [A-Za-z.'\-]+)*)"
    r"(?:(?:\s{2,}|\t+)(?P<rest>\S.*?))?\s*$"
)

_NOISE = re.compile(
    r"^\s*(?:page\s+\d+.*|\d+|.*secretary\s+of\s+state.*|.*certified.*list.*"
    r"|.*general\s+election.*|name\s{2,}party.*|party.*|.*nominations.*)\s*$",
    re.IGNORECASE,
)

# Arizona's wording -> canonical. "Representative in Congress" is federal;
# "State Representative" is not. Both pinned rather than left to the shared
# normaliser, for the reason Pennsylvania made expensive.
_OFFICE_NAMES: dict[str, str] = {
    "united states representative": "U.S. House",
    "u.s. representative": "U.S. House",
    "us representative": "U.S. House",
    "representative in congress": "U.S. House",
    "united states senator": "U.S. Senate",
    "u.s. senator": "U.S. Senate",
    "governor and lieutenant governor": "Governor",
    "governor": "Governor",
    "state senator": "State Senate",
    "state senate": "State Senate",
    "state representative": "State House",
}

# Offices whose rows carry a joint ticket. A set rather than a check against
# "Governor" so that a state adding another joint office is one entry.
JOINT_TICKET_OFFICES = frozenset({"Governor"})


class LayoutError(RuntimeError):
    """The document did not look like an Arizona candidate list.

    Raised rather than returning [], for the reason campaigns_ca gives: an
    empty result from a real document is indistinguishable downstream from a
    state with no filings, and "Arizona has no candidates" must never be silent.
    """


@dataclass
class ArizonaParse:
    records: list[CandidateRecord]
    report: ParseReport
    districts_seen: set[str]
    offices_seen: set[str]


def map_office(raw: str) -> str:
    """Arizona's wording -> canonical, or '' to drop the row.

    Longest key first: "governor and lieutenant governor" must beat "governor",
    which is a substring of it. That ordering is load-bearing from 2026, when
    the longer form starts appearing for the first time.
    """
    text = " ".join((raw or "").lower().split())
    if not text:
        return ""
    for wording in sorted(_OFFICE_NAMES, key=len, reverse=True):
        if wording in text:
            return _OFFICE_NAMES[wording]
    return ""


def _district_of(line: str) -> Optional[str]:
    match = _DISTRICT_HEADING.match(line)
    if not match:
        return None
    number = match.group("num") or match.group("num2") or ""
    return number.lstrip("0") or "0"


def parse_candidate_list(text: str, *, source_url: str = SOURCE_PAGE,
                         offices: Optional[Iterable[str]] = None,
                         strict: bool = True) -> ArizonaParse:
    """Extracted PDF text into candidate records. Pure."""
    if not (text or "").strip():
        if strict:
            raise LayoutError("empty document: nothing was extracted from the PDF")
        return ArizonaParse([], ParseReport(), set(), set())

    rows: list[dict] = []
    districts: set[str] = set()
    offices_seen: set[str] = set()
    current_office = ""
    current_district = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or _NOISE.match(line):
            continue

        heading = _OFFICE_HEADING.match(line)
        if heading:
            label = " ".join(heading.group("office").lower().split())
            offices_seen.add(label)
            current_office = map_office(label)
            current_district = ""
            continue

        district = _district_of(line)
        if district is not None:
            current_district = district
            continue

        if not current_office:
            continue

        candidate = _CANDIDATE_LINE.match(line)
        if not candidate:
            continue
        name = candidate.group("name").strip()
        if not name or (name.isupper() and len(name.split()) == 1):
            continue

        notes = ""
        if current_office in JOINT_TICKET_OFFICES:
            head, mate = split_ticket(name)
            if mate:
                name, notes = head, f"running mate: {mate}"

        rows.append({
            "name": name,
            "office": current_office,
            "district": "" if current_office in ("U.S. Senate", "Governor")
                        else current_district,
            "status": "certified",
            "notes": notes,
            "source_url": source_url,
        })
        if current_office == "U.S. House" and current_district:
            districts.add(current_district)

    records, report = from_rows(
        rows, state=STATE, source_url=source_url, found_by="filing:az",
        fields=FieldMap(), offices=offices)

    if strict and report.seen and not records:
        raise LayoutError(
            f"parsed {report.seen} candidate line(s) but kept none "
            f"({report.as_dict()}); the layout patterns no longer match")
    if strict and not report.seen:
        raise LayoutError(
            "no candidate lines matched: the document did not look like an "
            "Arizona candidate list. Run probe() and check the LAYOUT block.")

    return ArizonaParse(records, report, districts, offices_seen)


def fetch_document(*, fetcher: Optional[Fetcher] = None) -> str:
    if not DOCUMENT_URL:
        raise NotConfigured(
            "campaigns_az.DOCUMENT_URL is not set. Arizona publishes a PDF and "
            "the GENERAL election list is a different file from the primary "
            f"one -- pointing at the primary file would return candidates who "
            f"lost in July. Find the 2026 general list at {SOURCE_PAGE}, set "
            "DOCUMENT_URL, then run probe().")
    from .campaigns_ca import extract_pdf_text
    from .campaigns_tabular import _default_fetcher
    return extract_pdf_text((fetcher or _default_fetcher)(DOCUMENT_URL))


def arizona(state: str, office: str = "", *,
            fetcher: Optional[Fetcher] = None,
            text: Optional[str] = None) -> list[CandidateRecord]:
    """The STATE_FILING_SOURCES adapter for Arizona."""
    if (state or "").upper() != STATE:
        return []

    parsed = parse_candidate_list(
        text if text is not None else fetch_document(fetcher=fetcher),
        offices=[office] if office else None)

    if not office or office == "U.S. House":
        expected = HOUSE_SEATS[STATE]
        found = len(parsed.districts_seen)
        if found and found < expected * 0.5:
            raise LayoutError(
                f"found U.S. House candidates in only {found} of {expected} "
                f"Arizona districts: the parse is incomplete, not the ballot")
    return parsed.records


def probe(*, fetcher: Optional[Fetcher] = None,
          text: Optional[str] = None, lines: int = 60) -> dict[str, object]:
    """Read the real document and report what it looks like. Never raises."""
    try:
        body = text if text is not None else fetch_document(fetcher=fetcher)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "stage": "fetch",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "document_url": DOCUMENT_URL or "(unset)"}

    parsed = parse_candidate_list(body, strict=False)
    joint = [r for r in parsed.records if "running mate" in (r.notes or "")]
    return {
        "ok": bool(parsed.records),
        "stage": "parse",
        "document_url": DOCUMENT_URL or "(unset)",
        "chars": len(body),
        "candidates": len(parsed.records),
        "house_districts_found": len(parsed.districts_seen),
        "house_districts_expected": HOUSE_SEATS[STATE],
        "offices_recognised": sorted(parsed.offices_seen),
        "joint_tickets_split": len(joint),
        "report": parsed.report.as_dict(),
        "first_lines": [l for l in body.splitlines() if l.strip()][:lines],
    }


register_filing_source(STATE, arizona)
