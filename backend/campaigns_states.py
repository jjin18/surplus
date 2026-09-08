"""campaigns_states.py : the worklist — every priority state, what it publishes,
and how far its adapter has got.

WHY THIS IS CODE AND NOT A MARKDOWN TABLE. A list of states in a document goes
stale the moment an adapter lands and nobody edits the doc; this one reads the
live registry, so "written" and "configured" are observed rather than claimed.
`status()` is the answer to "where are we", and it cannot lie about which
adapters exist because it asks campaigns_sources rather than a hand-kept field.

WHAT A PROFILE IS FOR. Before writing a state's adapter you want to know three
things: what shape it publishes (a PDF needs a parser, a Socrata portal needs
an id), whether anyone has confirmed that, and which of its office names will
break the shared normaliser. The first two order the work. The third is the one
that silently corrupts data if missed -- Pennsylvania's "Representative in the
General Assembly" filed every statehouse candidate as a U.S. House candidate
until it was mapped -- so `office_trap` records it per state, in advance,
whether or not the adapter exists yet.

CONFIDENCE IS RECORDED, NOT ASSUMED. `publication` says how well the format is
actually known: "confirmed" means a document or portal was identified,
"likely" means the state is known to run a platform that probably carries it,
"unknown" means nobody has looked. Writing "CSV" for a state nobody has checked
would make the worklist look finished and send whoever picks it up down the
wrong path, so unknown stays unknown.

None of this fetches anything. It is a map of the territory, kept beside the
code that crosses it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import campaigns_races as races
from . import campaigns_sources as sources


@dataclass(frozen=True)
class StateProfile:
    """What is known about one state's candidate publishing."""
    state: str
    publication: str          # "confirmed" | "likely" | "unknown"
    shape: str                # "pdf" | "socrata" | "file" | "search-form" | "unknown"
    source_page: str = ""
    note: str = ""
    # The office wording that will be misfiled if it is not mapped explicitly.
    office_trap: str = ""
    # Anything about the state's institutions that changes what a row means.
    quirk: str = ""


PROFILES: dict[str, StateProfile] = {
    "CA": StateProfile(
        state="CA", publication="confirmed", shape="pdf",
        source_page="https://elections.cdn.sos.ca.gov/statewide-elections/"
                    "2026-general/cert-list-candidates.pdf",
        note="Certified list, certified 2026-08-27. URL confirmed; layout not "
             "yet read. Cal-Access deliberately not used -- finance data, "
             "different question, and use restrictions unresolved.",
        office_trap="'MEMBER OF THE STATE ASSEMBLY' is the state house.",
    ),
    "PA": StateProfile(
        state="PA", publication="confirmed", shape="socrata",
        source_page="https://data.pa.gov/",
        note="Socrata portal confirmed; 2026 dataset id NOT identified. "
             "campaigns_pa.discover() searches the catalogue for it.",
        office_trap="'Representative in the General Assembly' is the STATE "
                    "house and 'Senator in the General Assembly' the state "
                    "senate. Pennsylvania never says 'state', which is the "
                    "word the shared normaliser relies on -- unmapped, every "
                    "statehouse candidate files as U.S. House.",
    ),
    "OH": StateProfile(
        state="OH", publication="confirmed", shape="file",
        source_page="https://www.ohiosos.gov/elections/",
        note="Publishes through press releases and a spreadsheet whose URL "
             "changes each cycle. No catalogue to search, so finding the file "
             "is a human step.",
        office_trap="'Representative to Congress' is federal.",
        quirk="Governor and Lieutenant Governor run as a joint ticket, so one "
              "name cell can hold two people.",
    ),
    "AZ": StateProfile(
        state="AZ", publication="confirmed", shape="pdf",
        source_page="https://azsos.gov/elections/candidates",
        note="Publishes PDFs. The primary-ballot file is NOT the general one; "
             "reading it would return candidates who lost in July.",
        office_trap="'State Representative' vs 'Representative in Congress'.",
        quirk="FIRST lieutenant governor election, Nov 2026, on a joint ticket "
              "with the governor (Prop 131, 2022; running mates due 2026-09-04). "
              "No prior cycle's file exists to check the gubernatorial rows "
              "against, so that wording is the least-confirmed thing about AZ.",
    ),
    "TX": StateProfile(
        state="TX", publication="unknown", shape="unknown",
        source_page="https://www.sos.state.tx.us/elections/",
        note="Not investigated. Largest unclaimed prize on this list at 40 "
             "seats -- 38 districts, a Class 2 Senate seat and a governorship.",
        office_trap="'State Representative' / 'State Senator' against the "
                    "federal offices; check whether TX writes 'United States "
                    "Representative' or 'U.S. Representative'.",
    ),
    "NY": StateProfile(
        state="NY", publication="likely", shape="socrata",
        source_page="https://data.ny.gov/",
        note="New York runs a Socrata portal, so IF the candidate list is "
             "published there this is a resource id plus a FieldMap and "
             "nothing else. Whether it carries 2026 candidates is unconfirmed; "
             "the State Board of Elections is the fallback.",
        office_trap="'Member of Assembly' is the state house and does not "
                    "contain the word 'state'. Same trap as Pennsylvania.",
    ),
    "NC": StateProfile(
        state="NC", publication="likely", shape="file",
        source_page="https://www.ncsbe.gov/results-data",
        note="The State Board of Elections is known for publishing bulk data "
             "files rather than a portal or a search form, which if it holds "
             "makes this one of the easier states. Unconfirmed.",
        office_trap="'NC House of Representatives' / 'NC Senate' use the state "
                    "abbreviation instead of the word 'state'.",
    ),
    "MI": StateProfile(
        state="MI", publication="unknown", shape="unknown",
        source_page="https://www.michigan.gov/sos/elections",
        note="Not investigated.",
        office_trap="'Representative in Congress' is federal; check the "
                    "statehouse wording.",
    ),
    "WA": StateProfile(
        state="WA", publication="likely", shape="socrata",
        source_page="https://data.wa.gov/",
        note="Washington runs a Socrata portal and the Secretary of State "
             "publishes candidate data; which of the two carries the 2026 "
             "list is unconfirmed.",
        office_trap="'Legislative District' numbering is shared between the "
                    "two state chambers, so district alone does not identify "
                    "a seat.",
    ),
    "CO": StateProfile(
        state="CO", publication="unknown", shape="unknown",
        source_page="https://www.coloradosos.gov/pubs/elections/",
        note="Not investigated.",
    ),
    "IA": StateProfile(
        state="IA", publication="unknown", shape="unknown",
        source_page="https://sos.iowa.gov/elections/",
        note="Not investigated.",
    ),
    "NE": StateProfile(
        state="NE", publication="unknown", shape="unknown",
        source_page="https://sos.nebraska.gov/elections/",
        note="Not investigated.",
        office_trap="There is no state house. A Nebraska 'State Senator' is a "
                    "member of the only chamber -- mapping it to State Senate "
                    "is right, but expecting a State House row is not.",
        quirk="The Legislature is UNICAMERAL and officially NONPARTISAN, so "
              "its rows carry no party at all. Harmless here, since party is "
              "discarded by design, but it means a party column being empty is "
              "not evidence the parse failed.",
    ),
    "NM": StateProfile(
        state="NM", publication="unknown", shape="unknown",
        source_page="https://www.sos.nm.gov/voting-and-elections/",
        note="Not investigated.",
    ),
    "ME": StateProfile(
        state="ME", publication="unknown", shape="unknown",
        source_page="https://www.maine.gov/sos/cec/elec/",
        note="Not investigated. Smallest of the toss-up states at 4 seats.",
        quirk="Maine uses ranked-choice voting for federal races. It does not "
              "change who is on the ballot, so it does not change this parse -- "
              "noted so nobody goes looking for a problem that is not here.",
    ),
}


def profile(state: str) -> Optional[StateProfile]:
    return PROFILES.get((state or "").strip().upper())


def status() -> dict[str, object]:
    """Where the worklist stands, read from the live registry.

    "written" means an adapter is registered. "configured" means it will
    actually return rows -- an adapter whose dataset id or document URL is
    still unset is written but not configured, and reporting those as the same
    thing is how a coverage number starts describing reach instead of data.
    """
    sources.load_state_adapters()
    registered = set(sources.STATE_FILING_SOURCES)

    rows: list[dict] = []
    for state in sorted(PROFILES, key=lambda s: -len(races.seats_for_state(s))):
        prof = PROFILES[state]
        seats = races.seats_for_state(state)
        rows.append({
            "state": state,
            "seats": len(seats),
            "house": sum(1 for s in seats if s.office == "U.S. House"),
            "senate": sum(1 for s in seats if s.office == "U.S. Senate"),
            "governor": sum(1 for s in seats if s.office == "Governor"),
            "publication": prof.publication,
            "shape": prof.shape,
            "adapter_written": state in registered,
            "configured": _is_configured(state),
            "office_trap": bool(prof.office_trap),
            "quirk": bool(prof.quirk),
        })

    written = [r["state"] for r in rows if r["adapter_written"]]
    configured = [r["state"] for r in rows if r["configured"]]
    return {
        "states": rows,
        "written": written,
        "configured": configured,
        "reach": races.coverage(written),
        "actual": races.coverage(configured),
        "adapter_load_errors": dict(sources.ADAPTER_LOAD_ERRORS),
    }


def _is_configured(state: str) -> bool:
    """Does this state's adapter have a source to read?

    Asks each module for its own location constant rather than keeping a flag
    here, so filling one in is a one-line edit that this report picks up
    immediately and cannot disagree with.
    """
    import importlib
    locations = {"CA": ("campaigns_ca", "CERTIFIED_LIST_URL"),
                 "PA": ("campaigns_pa", "DATASET_ID"),
                 "OH": ("campaigns_oh", "DOWNLOAD_URL"),
                 "AZ": ("campaigns_az", "DOCUMENT_URL")}
    entry = locations.get(state)
    if not entry:
        return False
    module_name, attribute = entry
    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except Exception:                                 # noqa: BLE001
        return False
    return bool(getattr(module, attribute, ""))


def next_states(limit: int = 5) -> list[dict]:
    """What to do next: unwritten states, most seats first.

    Seats rather than toss-up count, because once an adapter exists the whole
    state comes with it -- Texas is 40 seats behind one filing office whether
    or not its one toss-up is the reason for going there.
    """
    sources.load_state_adapters()
    registered = set(sources.STATE_FILING_SOURCES)
    pending = [s for s in PROFILES if s not in registered]
    pending.sort(key=lambda s: (-len(races.seats_for_state(s)), s))
    return [{"state": s,
             "seats": len(races.seats_for_state(s)),
             "shape": PROFILES[s].shape,
             "publication": PROFILES[s].publication,
             "source_page": PROFILES[s].source_page,
             "office_trap": PROFILES[s].office_trap}
            for s in pending[:limit]]
