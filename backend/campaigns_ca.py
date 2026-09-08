"""campaigns_ca.py : California — the certified list of candidates.

CALIFORNIA IS THE FIRST ADAPTER because it is two toss-up House races and the
largest delegation in the country: 52 districts plus a governorship, 53 of the
504 federal and gubernatorial seats on the 2026 ballot from one filing office.

-----------------------------------------------------------------------------
WHICH CALIFORNIA SOURCE, AND WHY NOT THE OTHER ONE
-----------------------------------------------------------------------------
Two datasets could plausibly answer "who is running in California".

CAL-ACCESS is the Secretary of State's campaign finance system. It is richer,
it is tab-delimited rather than a PDF, and it is NOT used here. Two reasons.
First, it answers a different question: it knows who has registered a committee,
which is neither necessary nor sufficient for being on the ballot. Second, and
the reason it is not worth a second look, campaign-finance datasets are exactly
the category that carries sale-and-use restrictions -- the FEC's is why
campaigns_sources.py has no FEC backend (52 U.S.C. 30111(a)(4)), and whether
California imposes a comparable restriction on CAL-ACCESS bulk data for
commercial use is a question this module deliberately does not need to answer.
Government Code 84602(d) restricts displaying street addresses from those
filings, which establishes that the dataset carries use restrictions of some
kind; whether they reach commercial prospecting is UNRESOLVED and would need
counsel before anyone builds on it.

THE CERTIFIED LIST avoids the question entirely. It is a ballot-access
publication -- the Secretary of State certifying who will appear on the
November ballot -- not a finance filing, and it is the authoritative answer to
the question this product actually asks. It is also, unhelpfully, a PDF.

-----------------------------------------------------------------------------
THE PDF IS WHY THIS FILE IS SHAPED THE WAY IT IS
-----------------------------------------------------------------------------
Everything here splits into two halves along one line: what can be verified
without the file, and what cannot.

`parse_certified_list()` is pure. Text in, records out, no network, no clock.
Every quirk of the layout lives in it and is tested against fixtures, so the
logic is under test today even though the real document has not been read.

`fetch_certified_list()` is the half that touches the network, and it is a
thin wrapper taking an injectable fetcher so the parser is never coupled to it.

WHEN THE LAYOUT ASSUMPTION IS WRONG -- and it may be, since the 2026 document
has not been opened -- the failure has to be loud. `parse_certified_list` on a
non-empty document that yields no candidates RAISES. An empty list would be
read downstream as "California has no candidates", which is absurd on its face
and completely silent in every view built on top of it. Worse, it would look
identical to a successful parse of a state where nothing has been filed yet.

For the same reason `california()` checks its own result against the seat count
in campaigns_races: 52 districts are known to exist, so a parse that finds
candidates in eleven of them has failed even though it returned data. That
check is only possible because the denominator was built first, and it is the
difference between an adapter that breaks and an adapter that quietly thins.

TO FINISH THIS ADAPTER against the real document: run `probe()` with a fetcher,
read the first page of extracted text it returns, and correct the three regexes
in the LAYOUT block below. That is the whole job -- everything else is tested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .campaigns_filings import FieldMap, ParseReport, from_rows
from .campaigns_races import HOUSE_SEATS
from .campaigns_sources import CandidateRecord

STATE = "CA"

# The certified list for the November 3, 2026 general election, certified
# 2026-08-27. Update the path for a different election; the parser does not
# care which election it is reading.
CERTIFIED_LIST_URL = (
    "https://elections.cdn.sos.ca.gov/statewide-elections/2026-general/"
    "cert-list-candidates.pdf"
)

# The page a human opens to check any row parsed out of that PDF.
SOURCE_PAGE = CERTIFIED_LIST_URL


# ---------------------------------------------------------------------------
# LAYOUT : the only unverified thing in this file.
#
# The certified list is organised as office headings, each followed by the
# candidates for that office. District-level offices repeat the heading per
# district ("1st Congressional District"). These three patterns encode that
# structure. If the real document disagrees, this block is what changes --
# nothing below it should need to.
# ---------------------------------------------------------------------------

# An office heading: a line that names an office and nothing else.
_OFFICE_HEADING = re.compile(
    r"^\s*(?P<office>"
    r"UNITED\s+STATES\s+(?:REPRESENTATIVE|SENATOR)"
    r"|GOVERNOR"
    r"|LIEUTENANT\s+GOVERNOR"
    r"|MEMBER\s+OF\s+THE\s+STATE\s+ASSEMBLY"
    r"|STATE\s+ASSEMBLY"
    r"|STATE\s+SENATOR?"
    r"|SECRETARY\s+OF\s+STATE"
    r"|ATTORNEY\s+GENERAL"
    r"|CONTROLLER|TREASURER"
    r"|INSURANCE\s+COMMISSIONER"
    r"|SUPERINTENDENT\s+OF\s+PUBLIC\s+INSTRUCTION"
    r"|BOARD\s+OF\s+EQUALIZATION"
    r")\s*$",
    re.IGNORECASE,
)

# A district heading beneath an office: "1st Congressional District".
_DISTRICT_HEADING = re.compile(
    r"^\s*(?P<ordinal>\d+)(?:st|nd|rd|th)?\s+"
    r"(?P<kind>Congressional|Senatorial|Assembly|District)\s*District\s*$",
    re.IGNORECASE,
)

# A candidate line. The certified list carries name, party preference and
# ballot designation, separated by the runs of whitespace a PDF's column layout
# leaves behind. Party is captured only so it can be discarded -- see below.
#
# The name must NOT be allowed to match a run of two or more spaces: a class
# like [A-Za-z .'-]* contains a space, so it happily eats straight across the
# column gap and backtracking still satisfies the separator further along the
# line. The result is a "name" of "Doug Ramirez        Republican" that looks
# plausible in every log. Single spaces between name tokens only.
# Party gets its own group so that discarding it is an explicit act with a
# test on it, rather than a substring left inside a blob that later leaks into
# a note. See _PARTY_WORDS for the collapsed-column fallback.
_CANDIDATE_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z.'\-]*(?: [A-Za-z.'\-]+)*)"
    r"(?:\s{2,}|\t+)(?P<party>\S[^\t]*?)"
    r"(?:(?:\s{2,}|\t+)(?P<designation>\S.*?))?\s*$"
)

# When the PDF's columns collapse to single spaces, party and designation
# arrive glued together. Stripping a leading party word is the fallback that
# keeps party out of the note in that case.
_PARTY_WORDS = re.compile(
    r"^(?:democratic|republican|libertarian|green|peace\s+and\s+freedom"
    r"|american\s+independent|no\s+party\s+preference|independent|none)\b[\s,:-]*",
    re.IGNORECASE,
)

# Lines that are furniture rather than data, including the column header, which
# otherwise parses as a candidate named "Name".
_NOISE = re.compile(
    r"^\s*(?:page\s+\d+.*|\d+|certified\s+list.*|secretary\s+of\s+state.*"
    r"|november\s+\d+,?\s+\d{4}.*|general\s+election.*"
    r"|name\s{2,}party.*|party\s+preference.*|ballot\s+designation.*)\s*$",
    re.IGNORECASE,
)

# California's own wording for an office -> the canonical name used everywhere
# else. An office the certified list carries but this product does not pursue
# (Insurance Commissioner, Board of Equalization, judicial) is simply absent
# here: the lookup returns "" and its candidates are skipped.
#
# Kept here rather than loosened into campaigns_filings.normalize_office --
# "STATE ASSEMBLY" means State House in California specifically, and would be
# a bad global rule, so state quirks stay in the state adapter.
_OFFICE_NAMES: dict[str, str] = {
    "united states representative": "U.S. House",
    "united states senator": "U.S. Senate",
    "governor": "Governor",
    "member of the state assembly": "State House",
    "state assembly": "State House",
    "state senator": "State Senate",
    "state senate": "State Senate",
}


class LayoutError(RuntimeError):
    """The document did not look like a certified candidate list.

    Raised rather than returning [] because an empty result from a real
    document is indistinguishable downstream from a state with no filings,
    and 'California has no candidates' must never be a silent outcome.
    """


@dataclass
class CaliforniaParse:
    records: list[CandidateRecord]
    report: ParseReport
    districts_seen: set[str]
    offices_seen: set[str]


def _is_noise(line: str) -> bool:
    return not line.strip() or bool(_NOISE.match(line))


def parse_certified_list(text: str, *, source_url: str = SOURCE_PAGE,
                         offices: Optional[Iterable[str]] = None,
                         strict: bool = True) -> CaliforniaParse:
    """Turn the extracted text of the certified list into candidate records.

    Pure: no network, no clock, no filesystem. `strict` raises LayoutError when
    a non-empty document yields nothing, which is the intended default -- pass
    strict=False only from probe(), where reading a failed parse is the point.
    """
    if not (text or "").strip():
        if strict:
            raise LayoutError("empty document: nothing was extracted from the PDF")
        return CaliforniaParse([], ParseReport(), set(), set())

    rows: list[dict] = []
    districts: set[str] = set()
    offices_seen: set[str] = set()
    current_office = ""
    current_district = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _is_noise(line):
            continue

        heading = _OFFICE_HEADING.match(line)
        if heading:
            label = " ".join(heading.group("office").lower().split())
            offices_seen.add(label)
            current_office = _OFFICE_NAMES.get(label, "")
            current_district = ""      # a new office resets the district
            continue

        district = _DISTRICT_HEADING.match(line)
        if district:
            current_district = district.group("ordinal").lstrip("0") or "0"
            continue

        if not current_office:
            continue                   # under an office we do not pursue

        candidate = _CANDIDATE_LINE.match(line)
        if not candidate:
            continue

        name = candidate.group("name").strip()
        if not name or name.isupper() and len(name.split()) == 1:
            continue                   # a stray heading fragment, not a person

        # Party is matched so it can be dropped on the floor here, and only
        # here: campaigns_score.Campaign has no party field by design, so a
        # party string surviving onto the record is the beginning of the leak
        # that eventually defeats it. The ballot designation is the candidate's
        # own occupation line -- real evidence for personalisation -- and is
        # the only part of the line kept.
        designation = " ".join((candidate.group("designation") or "").split())
        if not designation:
            # Columns collapsed: party and designation came through as one
            # field. Strip the party word off the front of it.
            designation = _PARTY_WORDS.sub(
                "", " ".join(candidate.group("party").split())).strip()

        rows.append({
            "name": name,
            "office": current_office,
            "district": current_district,
            "status": "certified",
            "notes": designation,
            "source_url": source_url,
        })
        if current_office == "U.S. House" and current_district:
            districts.add(current_district)

    records, report = from_rows(
        rows, state=STATE, source_url=source_url, found_by="filing:ca",
        fields=FieldMap(), offices=offices,
    )

    if strict and report.seen and not records:
        raise LayoutError(
            f"parsed {report.seen} candidate line(s) but kept none "
            f"({report.as_dict()}); the layout patterns no longer match")
    if strict and not report.seen:
        raise LayoutError(
            "no candidate lines matched: the document did not look like a "
            "certified candidate list. Run probe() and check the LAYOUT block.")

    return CaliforniaParse(records, report, districts, offices_seen)


def extract_pdf_text(data: bytes) -> str:
    """Text out of the certified-list PDF, page by page.

    pypdf is imported lazily and is not a hard dependency of the package: this
    module stays importable (and its parser stays testable) on a deploy that
    never fetches a PDF, which is the same reason civic_sources imports httpx
    inside its backends.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:       # pragma: no cover - environment-dependent
        raise LayoutError(
            "pypdf is required to read the certified list PDF: "
            "pip install pypdf") from exc

    import io
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def fetch_certified_list(url: str = CERTIFIED_LIST_URL, *,
                         fetcher: Optional[Callable[[str], bytes]] = None) -> str:
    """Download the certified list and return its extracted text."""
    if fetcher is None:
        def fetcher(target: str) -> bytes:
            import httpx
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(target, headers={
                    "user-agent": "surplus-campaigns/1.0 "
                                  "(+https://event.surpluslayer.com)"})
            resp.raise_for_status()
            return resp.content

    return extract_pdf_text(fetcher(url))


def california(state: str, office: str = "", *,
               fetcher: Optional[Callable[[str], bytes]] = None,
               text: Optional[str] = None) -> list[CandidateRecord]:
    """The STATE_FILING_SOURCES adapter for California.

    Conforms to the contract in campaigns_sources: returns candidates who have
    qualified for the 2026 ballot, raises on transport or layout failure, and
    returns [] only for "nothing here" -- which for California is itself a
    failure, so it cannot happen without LayoutError firing first.

    `text` short-circuits the fetch and exists for tests and for re-parsing a
    document already on disk.
    """
    if (state or "").upper() != STATE:
        return []

    wanted = [office] if office else None
    parsed = parse_certified_list(
        text if text is not None else fetch_certified_list(fetcher=fetcher),
        offices=wanted,
    )

    # Self-check against the denominator. California has 52 congressional
    # districts; finding candidates in a small fraction of them means the parse
    # thinned rather than failed, which no exception would otherwise catch.
    if not office or office == "U.S. House":
        expected = HOUSE_SEATS[STATE]
        found = len(parsed.districts_seen)
        if found and found < expected * 0.5:
            raise LayoutError(
                f"found U.S. House candidates in only {found} of {expected} "
                f"California districts: the parse is incomplete, not the ballot")

    return parsed.records


def probe(*, fetcher: Optional[Callable[[str], bytes]] = None,
          lines: int = 60) -> dict[str, object]:
    """Read the real document and report what it looks like.

    The one command to run when finishing or repairing this adapter: it never
    raises on a bad layout, it shows the text that the patterns are failing
    against, and it says which offices and how many districts were recognised.
    """
    try:
        text = fetch_certified_list(fetcher=fetcher)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "stage": "fetch",
                "error": f"{type(exc).__name__}: {exc}"[:300]}

    parsed = parse_certified_list(text, strict=False)
    sample = [line for line in text.splitlines() if line.strip()][:lines]
    return {
        "ok": bool(parsed.records),
        "stage": "parse",
        "chars": len(text),
        "candidates": len(parsed.records),
        "house_districts_found": len(parsed.districts_seen),
        "house_districts_expected": HOUSE_SEATS[STATE],
        "offices_recognised": sorted(parsed.offices_seen),
        "report": parsed.report.as_dict(),
        "first_lines": sample,
    }


# Self-register so importing this adapter is enough to wire it in; loading it
# through campaigns_sources.load_state_adapters() reaches the same call.
from .campaigns_sources import register_filing_source  # noqa: E402

register_filing_source(STATE, california)
