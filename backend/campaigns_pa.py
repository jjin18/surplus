"""campaigns_pa.py : Pennsylvania — candidate filings from the state open data
portal.

PENNSYLVANIA IS SECOND because it carries two of the eighteen toss-ups and 17
congressional districts plus a governorship: 18 of the 504 federal and
gubernatorial seats. It is also the state that pays for the tabular layer,
since data.pa.gov runs Socrata and so do New York, Connecticut, Maryland and
Washington -- the states after this one cost a resource id, not a fetcher.

-----------------------------------------------------------------------------
WHAT IS CONFIRMED AND WHAT IS NOT
-----------------------------------------------------------------------------
CONFIRMED: Pennsylvania publishes election data through data.pa.gov, a Socrata
portal, and the Department of State separately runs a candidate search at
pavoterservices.pa.gov.

NOT CONFIRMED: the resource id of the 2026 candidate dataset, and its column
names. Neither could be checked from the environment this was written in, and
guessing a Socrata id produces one of two failures -- a 404 on first run, or
the far worse case where the id is real but points at a previous cycle and the
adapter quietly returns a list of people who ran in 2022.

So DATASET_ID is a placeholder and the adapter REFUSES TO RUN rather than
guessing, with NotConfigured naming the command that finds the real id:

    python -c "from backend import campaigns_pa; \\
               print(campaigns_pa.discover())"

That searches the portal catalogue and prints candidate ids with names, row
counts and last-updated dates. Paste the right one into DATASET_ID, run
`probe()` to see the actual column names, and correct FIELDS below. The parse
logic is already tested; those two constants are the whole remaining job.

-----------------------------------------------------------------------------
PENNSYLVANIA'S OFFICE NAMES
-----------------------------------------------------------------------------
The wording is genuinely unlike other states and is why _OFFICE_NAMES exists
here rather than in the shared normaliser. Pennsylvania elects a "Representative
in Congress" (federal) and, separately, a "Representative in the General
Assembly" (state house) and a "Senator in the General Assembly" (state senate).
The shared normalize_office() looks for the word "state" to tell a statehouse
office from a federal one, and Pennsylvania never uses it -- so left to the
shared rule, every state representative in the commonwealth would be filed as a
candidate for the U.S. House. Mapped here, before from_rows() ever sees the row.
"""
from __future__ import annotations

from typing import Callable, Optional

from .campaigns_filings import FieldMap, from_rows, override
from .campaigns_races import HOUSE_SEATS
from .campaigns_sources import CandidateRecord, register_filing_source
from .campaigns_tabular import (Fetcher, NotConfigured, fetch_socrata,
                                discover_socrata)

STATE = "PA"
SOCRATA_DOMAIN = "data.pa.gov"

# PLACEHOLDER. See the module docstring: run discover() and paste the real id.
DATASET_ID = ""

# The page a human opens to check a row. The portal dataset page once the id is
# known; the Department of State's own candidate search until then.
SOURCE_PAGE = "https://www.pa.gov/agencies/dos/programs/voting-and-elections/running-for-office"

# UNVERIFIED column names. probe() prints the real ones.
FIELDS = FieldMap(
    name=("candidate_name", "name", "candidate"),
    office=("office_name", "office", "office_sought"),
    district=("district", "district_number", "district_name"),
    status=("status", "candidate_status", "filing_status"),
    email=("email", "candidate_email"),
    website=("website", "url"),
    first_name=("first_name", "candidate_first_name"),
    last_name=("last_name", "candidate_last_name"),
)

# Pennsylvania's wording -> the canonical names. See the docstring: without
# this, "Representative in the General Assembly" reads as U.S. House.
_OFFICE_NAMES: dict[str, str] = {
    "representative in congress": "U.S. House",
    "united states representative": "U.S. House",
    "united states senator": "U.S. Senate",
    "senator in congress": "U.S. Senate",
    "governor": "Governor",
    "senator in the general assembly": "State Senate",
    "representative in the general assembly": "State House",
}


def map_office(raw: str) -> str:
    """Pennsylvania's office wording -> canonical, or '' to drop the row.

    Substring rather than exact match: the portal appends a district to the
    office in some exports ("Representative in Congress 12"). Longest key
    first, so "senator in the general assembly" is never shadowed by a looser
    key that happens to be a prefix of it.
    """
    text = " ".join((raw or "").lower().split())
    if not text:
        return ""
    for wording in sorted(_OFFICE_NAMES, key=len, reverse=True):
        if wording in text:
            return _OFFICE_NAMES[wording]
    return ""


def discover(*, fetcher: Optional[Fetcher] = None,
             query: str = "candidate") -> list[dict]:
    """Find the candidate dataset on data.pa.gov. Run this first."""
    return discover_socrata(SOCRATA_DOMAIN, query, fetcher=fetcher)


def _rows(fetcher: Optional[Fetcher], rows: Optional[list[dict]]) -> list[dict]:
    if rows is not None:
        return rows
    if not DATASET_ID:
        raise NotConfigured(
            "campaigns_pa.DATASET_ID is not set: run "
            "`python -c \"from backend import campaigns_pa; "
            "print(campaigns_pa.discover())\"` to find the 2026 candidate "
            f"dataset on {SOCRATA_DOMAIN}, then paste its id into DATASET_ID.")
    return fetch_socrata(SOCRATA_DOMAIN, DATASET_ID, fetcher=fetcher)


def pennsylvania(state: str, office: str = "", *,
                 fetcher: Optional[Fetcher] = None,
                 rows: Optional[list[dict]] = None) -> list[CandidateRecord]:
    """The STATE_FILING_SOURCES adapter for Pennsylvania.

    `rows` short-circuits the fetch, for tests and for re-parsing an export
    already on disk.
    """
    if (state or "").upper() != STATE:
        return []

    raw = _rows(fetcher, rows)
    mapped: list[dict] = []
    for row in raw:
        office_raw = ""
        for key in ("office_name", "office", "office_sought"):
            office_raw = office_raw or str(row.get(key) or "")
        canonical = map_office(office_raw)
        if not canonical:
            continue        # an office this product does not pursue
        # override(), not {**row, "office": ...}: FIELDS.office tries
        # "office_name" first, so the raw wording would otherwise win and every
        # General Assembly candidate would be filed as U.S. House.
        entry = override(row, FIELDS, office=canonical)
        entry["source_url"] = str(row.get("source_url") or SOURCE_PAGE)
        mapped.append(entry)

    records, report = from_rows(
        mapped, state=STATE, source_url=SOURCE_PAGE, found_by="filing:pa",
        fields=FIELDS, offices=[office] if office else None)

    # A dataset that fetched fine but parsed to nothing is a column-name
    # mismatch, not an empty ballot. Distinguishing them is the whole point of
    # keeping the report.
    if raw and not records and not office:
        raise NotConfigured(
            f"read {len(raw)} row(s) from {SOCRATA_DOMAIN} but kept none "
            f"({report.as_dict()}): the column names in FIELDS do not match "
            f"this dataset. Run probe() to see the real ones.")
    return records


def probe(*, fetcher: Optional[Fetcher] = None,
          rows: Optional[list[dict]] = None, sample: int = 3) -> dict[str, object]:
    """Report what the dataset actually looks like. Never raises on a bad
    shape -- reading the failure is the point."""
    try:
        raw = _rows(fetcher, rows)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "stage": "fetch",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "dataset_id": DATASET_ID or "(unset)"}

    columns = sorted({key for row in raw[:200] for key in row})
    offices = sorted({str(row.get("office_name") or row.get("office") or "")
                      for row in raw[:2000]} - {""})
    try:
        records = pennsylvania(STATE, rows=raw, fetcher=fetcher)
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
        "dataset_id": DATASET_ID or "(unset)",
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


register_filing_source(STATE, pennsylvania)
