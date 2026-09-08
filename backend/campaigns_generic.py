"""campaigns_generic.py : one adapter, driven by a state's profile.

WHY THIS REPLACES WRITING TEN MORE MODULES. California and Arizona needed real
code: they publish PDFs, and a PDF needs a layout parser. Every remaining
priority state publishes a table, and once campaigns_tabular fetches it and
campaigns_filings parses it, what is actually left per state is DATA -- which
column holds the name, what the state calls its statehouse, where the file
lives. Ten modules of that would be ten copies of the same twenty lines with a
different dict at the top, and ten places for the same bug to be fixed nine
times.

So a tabular state is a StateProfile entry plus this function. The profile
carries the parts that differ; this carries the parts that do not.

-----------------------------------------------------------------------------
THE JOINT-TICKET DECISION, AND WHY IT IS UNCONDITIONAL
-----------------------------------------------------------------------------
Governor rows go through split_ticket in EVERY state, not only the ones known
to elect a joint ticket. That looks sloppy and is deliberate.

split_ticket only splits on an explicit conjunction between two names; a single
name passes through untouched. So applying it where there is no joint ticket
costs nothing, while NOT applying it where there is one produces "Jane Doe and
John Roe" as a candidate name -- a record that matches no campaign and no dedup
key. The two errors are not symmetric, and the states are genuinely hard to
enumerate confidently: Ohio has had a joint ticket for decades, Arizona gets
one for the first time in 2026, Maine has no lieutenant governor at all, and
Texas elects its own separately. Guessing that list wrong in the direction of
"no split" is the expensive way to be wrong.

-----------------------------------------------------------------------------
FUSION VOTING
-----------------------------------------------------------------------------
New York lets a candidate run on several party lines at once, so one person
legitimately occupies several rows of the candidate list -- a major-party line
plus Conservative or Working Families. Parsed naively, New York reports roughly
half again as many candidates as are running, and the extra rows are not
errors, so nothing flags them.

`fusion_voting` on the profile collapses them on the identity key and reports
how many were folded, so the count reconciles and the collapse is visible
rather than being mistaken for a thin parse. It is off by default: outside the
handful of fusion states, two rows for one person in one seat IS a bug and
silently merging them would hide it.

-----------------------------------------------------------------------------
OFFICE MAPS ARE BEST-EFFORT AND SAY SO
-----------------------------------------------------------------------------
Each profile's office_names is written from how the state is known to word its
offices, and has NOT been checked against its real file. That is fine for a
map -- a wording that is missing simply drops the row rather than misfiling it
-- with one exception that matters: a statehouse office wrongly falling through
to the shared normaliser becomes a U.S. House candidate. Every state whose
statehouse name lacks the word "state" therefore has that wording pinned
explicitly, and `office_trap` on the profile records which ones those are.

probe() prints `offices_in_data` against `offices_mapped`, so the first real
run shows exactly which wordings are unaccounted for.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from .campaigns_filings import from_rows, override, split_ticket
from .campaigns_races import HOUSE_SEATS
from .campaigns_sources import CandidateRecord, merge
from .campaigns_tabular import (Fetcher, NotConfigured, fetch_socrata,
                                fetch_table)

# Offices whose rows may carry two names. See the docstring: unconditional.
JOINT_TICKET_OFFICES = frozenset({"Governor"})


def map_office(raw: str, office_names: dict[str, str]) -> str:
    """A state's wording -> canonical, or '' to drop the row.

    Longest key first, always: "governor and lieutenant governor" contains
    "governor", and "senator in the general assembly" contains neither of the
    shorter keys but would be shadowed by any key that is a prefix of it.
    """
    text = " ".join((raw or "").lower().split())
    if not text:
        return ""
    for wording in sorted(office_names, key=len, reverse=True):
        if wording in text:
            return office_names[wording]
    return ""


def _office_value(row: dict, spec) -> str:
    keys = (spec,) if isinstance(spec, str) else tuple(spec)
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).strip().lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def fetch_rows(profile, *, fetcher: Optional[Fetcher] = None) -> list[dict]:
    """Get the state's table, or explain why we cannot."""
    if not profile.dataset:
        raise NotConfigured(_unconfigured_message(profile))
    if profile.shape == "socrata":
        return fetch_socrata(profile.socrata_domain, profile.dataset,
                             fetcher=fetcher)
    return fetch_table(profile.dataset, fetcher=fetcher)


def _unconfigured_message(profile) -> str:
    if profile.shape == "socrata":
        return (f"campaigns_states.PROFILES['{profile.state}'].dataset is not "
                f"set. {profile.socrata_domain} is a Socrata portal: run "
                f"`campaigns_generic.discover('{profile.state}')` to search its "
                f"catalogue, then set dataset to the resource id.")
    return (f"campaigns_states.PROFILES['{profile.state}'].dataset is not set. "
            f"Find the 2026 candidate file at {profile.source_page} and set "
            f"dataset to its URL, then run "
            f"`campaigns_generic.probe('{profile.state}')`.")


def discover(state: str, *, query: str = "candidate",
             fetcher: Optional[Fetcher] = None) -> list[dict]:
    """Search a Socrata state's catalogue for its candidate dataset."""
    from . import campaigns_states
    from .campaigns_tabular import discover_socrata
    profile = campaigns_states.PROFILES[state.upper()]
    if profile.shape != "socrata":
        raise NotConfigured(
            f"{profile.state} does not publish to a Socrata portal "
            f"(shape={profile.shape!r}); there is no catalogue to search. "
            f"Find the file at {profile.source_page}.")
    return discover_socrata(profile.socrata_domain, query, fetcher=fetcher)


def build_records(profile, raw: list[dict], *,
                  office: str = "") -> tuple[list[CandidateRecord], dict]:
    """Rows -> records, applying this state's profile. Pure."""
    mapped: list[dict] = []
    for row in raw:
        canonical = map_office(_office_value(row, profile.fields.office),
                               profile.office_names)
        if not canonical:
            continue

        # override(), never a dict splat: FieldMap resolves aliases in order,
        # so a raw office column left in place beats the canonical value.
        entry = override(row, profile.fields, office=canonical)
        entry["source_url"] = str(row.get("source_url") or profile.dataset
                                  or profile.source_page)

        if canonical in JOINT_TICKET_OFFICES:
            raw_name = _office_value(row, profile.fields.name)
            head, mate = split_ticket(raw_name)
            if mate:
                existing = str(row.get("notes") or "")
                entry = override(entry, profile.fields, name=head,
                                 notes=f"{existing} running mate: {mate}".strip())
        mapped.append(entry)

    records, report = from_rows(
        mapped, state=profile.state, source_url=profile.source_page,
        found_by=f"filing:{profile.state.lower()}", fields=profile.fields,
        offices=[office] if office else None)

    stats = report.as_dict()
    stats["fusion_collapsed"] = 0
    if profile.fusion_voting and records:
        before = len(records)
        records = merge(records)
        stats["fusion_collapsed"] = before - len(records)
    return records, stats


def build_adapter(profile) -> Callable[..., list[CandidateRecord]]:
    """The STATE_FILING_SOURCES callable for a tabular state."""

    def adapter(state: str, office: str = "", *,
                fetcher: Optional[Fetcher] = None,
                rows: Optional[list[dict]] = None) -> list[CandidateRecord]:
        if (state or "").upper() != profile.state:
            return []
        raw = rows if rows is not None else fetch_rows(profile, fetcher=fetcher)
        records, stats = build_records(profile, raw, office=office)

        # Rows read but none kept is a column-name mismatch, not an empty
        # ballot. They look identical from outside, so say which it is.
        if raw and not records and not office:
            raise NotConfigured(
                f"read {len(raw)} row(s) for {profile.state} but kept none "
                f"({stats}): the column names in this profile's FieldMap do "
                f"not match the file. Run "
                f"campaigns_generic.probe('{profile.state}') to see the real "
                f"ones.")
        return records

    adapter.__name__ = f"adapter_{profile.state.lower()}"
    adapter.__doc__ = f"Profile-driven filing adapter for {profile.state}."
    return adapter


def probe(state: str, *, fetcher: Optional[Fetcher] = None,
          rows: Optional[list[dict]] = None, sample: int = 3) -> dict[str, object]:
    """What the state's file actually looks like. Never raises on a bad shape."""
    from . import campaigns_states
    profile = campaigns_states.PROFILES[state.upper()]

    try:
        raw = rows if rows is not None else fetch_rows(profile, fetcher=fetcher)
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "stage": "fetch", "state": profile.state,
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "dataset": profile.dataset or "(unset)",
                "source_page": profile.source_page}

    offices_raw = sorted({_office_value(row, profile.fields.office)
                          for row in raw[:5000]} - {""})
    records, stats = build_records(profile, raw)
    districts = {r.district for r in records
                 if r.office == "U.S. House" and r.district}

    return {
        "ok": bool(records),
        "stage": "parse",
        "state": profile.state,
        "dataset": profile.dataset or "(unset)",
        "rows": len(raw),
        "columns": sorted({key for row in raw[:200] for key in row}),
        "offices_in_data": offices_raw[:40],
        "offices_mapped": sorted({map_office(o, profile.office_names)
                                  for o in offices_raw} - {""}),
        "offices_unmapped": [o for o in offices_raw
                             if not map_office(o, profile.office_names)][:20],
        "candidates": len(records),
        "house_districts_found": len(districts),
        "house_districts_expected": HOUSE_SEATS[profile.state],
        "report": stats,
        "sample_rows": raw[:sample],
    }
