"""campaigns_oh.py : Ohio — the Secretary of State's candidate list.

OHIO IS THIRD: two of the eighteen toss-ups, 15 congressional districts plus a
governorship, 16 of the 504 federal and gubernatorial seats.

-----------------------------------------------------------------------------
OHIO IS THE HARDER SHAPE, AND IT IS WORTH SAYING WHY
-----------------------------------------------------------------------------
California publishes a certified PDF at a stable, predictable URL. Pennsylvania
publishes to a Socrata portal with a catalogue you can search. Ohio does
neither: the Secretary of State announces candidate filings through press
releases and web pages, and the machine-readable file -- when there is one --
is a spreadsheet at a URL that changes every cycle, with no catalogue to search.

That is not a defect in this adapter, it is the actual distribution of how the
fifty states publish, and it is the reason the registry is filled one state at
a time against the real document rather than by writing fifty at once. Roughly:
a handful publish clean APIs, more publish a file you must find by hand, and
several publish only a search form. Ohio is the middle case.

CONSEQUENCE: DOWNLOAD_URL is a placeholder and this adapter refuses to run
without it, rather than guessing a URL that would either 404 or -- much worse
-- resolve to a previous cycle's file and return people who ran in 2022. There
is no discover() here because there is no catalogue to search; finding the URL
is a human step:

    https://www.ohiosos.gov/elections/  ->  candidate lists for the current
    election. Copy the .xlsx or .csv link into DOWNLOAD_URL, run probe() to see
    the real column names, and correct FIELDS.

fetch_table() dispatches on the extension, so an .xlsx URL and a .csv URL both
work without touching this file again -- which matters because Ohio has
switched format between cycles before.

-----------------------------------------------------------------------------
THE JOINT TICKET
-----------------------------------------------------------------------------
Ohio elects Governor and Lieutenant Governor as a single ticket, so the office
reads "Governor and Lieutenant Governor" and the name cell can carry two people
("Jane Doe and John Roe"). Both facts are handled here: the office maps to
"Governor", and a joint name keeps the gubernatorial candidate -- the head of
the ticket, and the person a campaign's software decision runs through -- with
the running mate recorded in notes rather than silently dropped or, worse, left
glued into the name so that every downstream match on that campaign fails.
"""
from __future__ import annotations

import re
from typing import Optional

from .campaigns_filings import FieldMap, from_rows, override
from .campaigns_races import HOUSE_SEATS
from .campaigns_sources import CandidateRecord, register_filing_source
from .campaigns_tabular import Fetcher, NotConfigured, fetch_table

STATE = "OH"

# PLACEHOLDER. See the module docstring: no catalogue to search, so this is a
# human step against ohiosos.gov.
DOWNLOAD_URL = ""

SOURCE_PAGE = "https://www.ohiosos.gov/elections/"

# UNVERIFIED column names. probe() prints the real ones.
FIELDS = FieldMap(
    name=("candidate_name", "name", "candidate", "full_name"),
    office=("office", "office_title", "office_sought"),
    district=("district", "district_number", "dist"),
    status=("status", "certification_status", "filing_status"),
    email=("email", "candidate_email"),
    website=("website", "url"),
    first_name=("first_name", "firstname"),
    last_name=("last_name", "lastname"),
)

# Ohio's wording -> canonical. "Representative to Congress" is the federal
# office; "State Representative" is not, and the shared normaliser would read
# the first correctly but relies on the word "state" to exclude the second, so
# both are pinned here to be safe.
_OFFICE_NAMES: dict[str, str] = {
    "representative to congress": "U.S. House",
    "united states representative": "U.S. House",
    "u.s. representative": "U.S. House",
    "united states senator": "U.S. Senate",
    "u.s. senator": "U.S. Senate",
    "governor and lieutenant governor": "Governor",
    "governor": "Governor",
    "state senator": "State Senate",
    "state representative": "State House",
}

# "Jane Doe and John Roe" on a joint ticket. Split on a standalone "and" or an
# ampersand, never on a name that merely contains those letters.
_JOINT_TICKET = re.compile(r"\s+(?:and|&|/)\s+", re.IGNORECASE)


def map_office(raw: str) -> str:
    """Ohio's office wording -> canonical, or '' to drop the row.

    Longest key first: "governor and lieutenant governor" must win over
    "governor", which is a substring of it. Getting that order wrong would file
    every joint ticket correctly by accident and then break the day someone
    adds a shorter key.
    """
    text = " ".join((raw or "").lower().split())
    if not text:
        return ""
    for wording in sorted(_OFFICE_NAMES, key=len, reverse=True):
        if wording in text:
            return _OFFICE_NAMES[wording]
    return ""


def split_ticket(name: str) -> tuple[str, str]:
    """A joint-ticket name into (head of ticket, running mate).

    Returns the name unchanged and an empty mate when there is no split, which
    is every office except governor.
    """
    text = " ".join((name or "").split())
    if not text:
        return "", ""
    parts = _JOINT_TICKET.split(text, maxsplit=1)
    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()
    return text, ""


def _rows(fetcher: Optional[Fetcher], rows: Optional[list[dict]]) -> list[dict]:
    if rows is not None:
        return rows
    if not DOWNLOAD_URL:
        raise NotConfigured(
            "campaigns_oh.DOWNLOAD_URL is not set. Ohio publishes no dataset "
            f"catalogue, so this is a human step: find the current candidate "
            f"list at {SOURCE_PAGE} and paste its .xlsx or .csv link into "
            "DOWNLOAD_URL, then run probe().")
    return fetch_table(DOWNLOAD_URL, fetcher=fetcher)


def ohio(state: str, office: str = "", *,
         fetcher: Optional[Fetcher] = None,
         rows: Optional[list[dict]] = None) -> list[CandidateRecord]:
    """The STATE_FILING_SOURCES adapter for Ohio."""
    if (state or "").upper() != STATE:
        return []

    raw = _rows(fetcher, rows)
    mapped: list[dict] = []
    for row in raw:
        office_raw = ""
        for key in ("office", "office_title", "office_sought"):
            office_raw = office_raw or str(row.get(key) or "")
        canonical = map_office(office_raw)
        if not canonical:
            continue

        # override() rather than a dict splat : FIELDS resolves aliases in
        # order, so a raw "office"/"candidate_name" left in place beats the
        # canonical value set alongside it. See campaigns_filings.override.
        entry = override(row, FIELDS, office=canonical)
        entry["source_url"] = str(
            row.get("source_url") or DOWNLOAD_URL or SOURCE_PAGE)

        # Joint ticket: keep the head of the ticket as the candidate and record
        # the running mate rather than leaving both glued into one name, which
        # would make every downstream match on that campaign miss.
        raw_name = ""
        for key in ("candidate_name", "name", "candidate", "full_name"):
            raw_name = raw_name or str(row.get(key) or "")
        if canonical == "Governor" and raw_name:
            head, mate = split_ticket(raw_name)
            if mate:
                existing = str(row.get("notes") or "")
                entry = override(entry, FIELDS, name=head,
                                 notes=f"{existing} running mate: {mate}".strip())
        mapped.append(entry)

    records, report = from_rows(
        mapped, state=STATE, source_url=DOWNLOAD_URL or SOURCE_PAGE,
        found_by="filing:oh", fields=FIELDS,
        offices=[office] if office else None)

    if raw and not records and not office:
        raise NotConfigured(
            f"read {len(raw)} row(s) from the Ohio file but kept none "
            f"({report.as_dict()}): the column names in FIELDS do not match "
            f"this file. Run probe() to see the real ones.")
    return records


def probe(*, fetcher: Optional[Fetcher] = None,
          rows: Optional[list[dict]] = None, sample: int = 3) -> dict[str, object]:
    """Report what the file actually looks like. Never raises on a bad shape."""
    try:
        raw = _rows(fetcher, rows)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "stage": "fetch",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "download_url": DOWNLOAD_URL or "(unset)"}

    columns = sorted({key for row in raw[:200] for key in row})
    offices = sorted({str(row.get("office") or row.get("office_title") or "")
                      for row in raw[:2000]} - {""})
    try:
        records = ohio(STATE, rows=raw, fetcher=fetcher)
    except Exception as exc:                        # noqa: BLE001
        records = []
        parse_error = f"{type(exc).__name__}: {exc}"[:300]
    else:
        parse_error = ""

    districts = {r.district for r in records
                 if r.office == "U.S. House" and r.district}
    return {
        "ok": bool(records),
        "stage": "parse",
        "download_url": DOWNLOAD_URL or "(unset)",
        "rows": len(raw),
        "columns": columns,
        "offices_in_data": offices[:40],
        "offices_mapped": sorted({map_office(o) for o in offices} - {""}),
        "candidates": len(records),
        "house_districts_found": len(districts),
        "house_districts_expected": HOUSE_SEATS[STATE],
        "parse_error": parse_error,
        "sample_rows": raw[:sample],
    }


register_filing_source(STATE, ohio)
