"""campaigns_sources.py : where the 2026 candidate list comes from, and the one
source it deliberately does not come from.

THE SHAPE IS BORROWED FROM civic_sources.py ON PURPOSE. Every backend is
queried at once, failure-isolated, and cached per process; a backend that is
down, rate-limited or has changed its response shape contributes nothing, is
recorded in LAST_RUN, and the list is built from whatever did come back.
Breadth costs threads rather than round-trips, and one dead source never takes
the run down. If you are changing this file, read that one first -- the two
should stay recognisably the same machine.

-----------------------------------------------------------------------------
WHY THE FEC IS NOT A BACKEND HERE
-----------------------------------------------------------------------------
The FEC is the obvious place to get a national candidate list with committee
contact details and fundraising totals, and it is the one source this module
must not use for this purpose.

    52 U.S.C. 30111(a)(4), implemented at 11 CFR 104.15: information copied
    from reports filed with the Commission "may not be sold or used by any
    person for the purpose of soliciting contributions or for commercial
    purposes". The single carve-out is using the name and address of a
    political committee to solicit *contributions from that committee* --
    which is the opposite of selling that committee software.

Building a prospect list for commercial outreach out of FEC filings is the
named prohibited use, not a grey area, and the FEC has enforcement history on
it. That covers the derived cases too: ranking prospects by a fundraising
figure copied from a filing is FEC data used for a commercial purpose even
though the number never appears in the email.

So: no FEC backend, and no FEC-derived field on CandidateRecord. There is a
test asserting both, because this is exactly the kind of constraint that gets
re-added by someone who just wants the fundraising number for a scoring signal.

This is not legal advice and this docstring is not a substitute for counsel
signing off before launch -- the same posture solicitation.py takes about Rule
7.3. Ship the shape now; get the reading confirmed before it matters.

-----------------------------------------------------------------------------
WHAT THAT LEAVES, AND WHY IT IS ENOUGH
-----------------------------------------------------------------------------
State election offices are the authoritative source for ballot access anyway --
they are who decides whether a candidate is actually on the ballot, which the
FEC does not -- and they carry no such restriction. They are also the only
source that has CHALLENGERS. That last point is the real constraint on this
whole product and it is worth stating plainly:

    Roster APIs (GovTrack, OpenStates) answer "who currently holds this seat".
    A challenger holds no seat, so they appear in no roster. The incumbent
    backends below are enrichment and race context, NOT the candidate list.
    The candidate list comes from state filings, and there is no unified
    national API for them -- fifty offices, fifty formats.

That ingest is the actual work of this product, and pretending otherwise (as a
"just pull it from the FEC" plan does) hides the only genuinely hard part.
STATE_FILING_SOURCES below is the registry it goes in; it ships empty, with the
adapter contract documented, because fifty speculative scrapers written against
response shapes nobody has looked at is fifty things that break silently.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

# Someone else's free API: be identifiable, and never wait on one long enough
# to hurt the run. Same numbers as civic_sources for the same reasons.
BACKEND_TIMEOUT_S = 8.0
USER_AGENT = "surplus-campaigns/1.0 (+https://event.surpluslayer.com)"

_CACHE_TTL_S = 300.0
_RESULT_CACHE: dict[str, tuple[float, list]] = {}

# Diagnostics for the last gather(): backend name -> count, or the error that
# stopped it. Read it when a run comes back thin -- a quiet zero and a 403 look
# identical in the output and completely different here.
LAST_RUN: dict[str, object] = {}


class RateLimited(RuntimeError):
    """A free API asking us to slow down. Weather, not a fault."""


@dataclass
class CandidateRecord:
    """One candidate for one seat, as sourced.

    Deliberately absent: party, and any figure copied from an FEC filing. The
    first because backend/campaigns_score.py must not be able to see it (that
    module's rule 1); the second because of 11 CFR 104.15 above. Both omissions
    are load-bearing and both are covered by tests.
    """
    name: str
    office: str                      # "U.S. House", "U.S. Senate", "Governor"
    state: str                       # two-letter, upper
    district: str = ""               # "12", "" for statewide
    status: str = ""                 # "filed", "on_ballot", "incumbent", ...
    campaign_url: str = ""
    contact_email: str = ""
    contact_name: str = ""           # a named staffer, when public
    source_url: str = ""             # the page this came from
    found_by: str = ""               # which backend
    notes: str = ""

    def identity(self) -> tuple[str, str, str, str]:
        """The dedup key. Mirrors the spirit of the relationship side's
        identity-key kernel: normalise hard, then compare, so 'Pat O'Doe' and
        'Pat O Doe' from two sources merge instead of double-contacting a
        campaign under two rows."""
        return (_fold(self.name), self.state.upper(),
                _fold(self.office), self.district.strip().lstrip("0"))

    def is_contactable(self) -> bool:
        return bool(self.contact_email or self.campaign_url)


def _fold(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    raw = unicodedata.normalize("NFKD", text or "")
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = re.sub(r"[^\w\s]", " ", raw.lower())
    return " ".join(raw.split())


def _cached(key: str, produce: Callable[[], list]) -> list:
    now = time.time()
    hit = _RESULT_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    found = produce()
    if len(_RESULT_CACHE) > 300:
        for stale in sorted(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k][0])[:100]:
            _RESULT_CACHE.pop(stale, None)
    _RESULT_CACHE[key] = (now, found)
    return found


def _get_json(url: str, params: dict, headers: Optional[dict] = None) -> dict:
    import httpx
    head = {"user-agent": USER_AGENT, "accept": "application/json"}
    head.update(headers or {})
    with httpx.Client(timeout=BACKEND_TIMEOUT_S, follow_redirects=True) as client:
        resp = client.get(url, params=params, headers=head)
    if resp.status_code == 429:
        raise RateLimited(url)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"not JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Incumbent / race-context backends.
#
# These answer "who holds this seat now". They are NOT the candidate list --
# see the module docstring. Every field access is guarded: an upstream that has
# changed shape should contribute nothing, not raise halfway through a run.
# ---------------------------------------------------------------------------

def _govtrack_incumbents(state: str, office: str) -> list[CandidateRecord]:
    """Sitting members of Congress for a state. Keyless."""
    if office not in ("U.S. House", "U.S. Senate", ""):
        return []
    role = {"U.S. House": "representative", "U.S. Senate": "senator"}.get(office)
    params: dict = {"current": "true", "state": state.upper(), "limit": 60}
    if role:
        params["role_type"] = role
    data = _get_json("https://www.govtrack.us/api/v2/role", params)

    out: list[CandidateRecord] = []
    for row in (data.get("objects") or []):
        person = row.get("person") or {}
        name = (person.get("name") or "").strip()
        if not name:
            continue
        kind = "U.S. Senate" if row.get("role_type") == "senator" else "U.S. House"
        out.append(CandidateRecord(
            name=name,
            office=kind,
            state=(row.get("state") or state).upper(),
            district=str(row.get("district") or "").strip(),
            status="incumbent",
            campaign_url=(person.get("link") or "").strip(),
            source_url=(person.get("link") or "https://www.govtrack.us/").strip(),
            found_by="govtrack",
            notes="sitting member; seat context only, not a ballot listing",
        ))
    return out


def _openstates_incumbents(state: str, office: str) -> list[CandidateRecord]:
    """Sitting state legislators. Needs a free OPENSTATES_API_KEY; skipped
    without one -- a key nobody has set is not a failure worth reporting."""
    key = (os.environ.get("OPENSTATES_API_KEY") or "").strip()
    if not key or not state:
        return []
    data = _get_json("https://v3.openstates.org/people",
                     {"jurisdiction": state.lower(), "per_page": 50},
                     headers={"X-API-KEY": key})

    out: list[CandidateRecord] = []
    for row in (data.get("results") or []):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        current = (row.get("current_role") or {})
        out.append(CandidateRecord(
            name=name,
            office=(current.get("title") or "State legislator").strip(),
            state=state.upper(),
            district=str(current.get("district") or "").strip(),
            status="incumbent",
            campaign_url=(row.get("openstates_url") or "").strip(),
            contact_email=(row.get("email") or "").strip(),
            source_url=(row.get("openstates_url") or "").strip(),
            found_by="openstates",
            notes="sitting legislator; seat context only, not a ballot listing",
        ))
    return out


INCUMBENT_BACKENDS: dict[str, Callable[[str, str], list[CandidateRecord]]] = {
    "govtrack": _govtrack_incumbents,
    "openstates": _openstates_incumbents,
}


# ---------------------------------------------------------------------------
# State candidate filings -- the actual candidate list.
# ---------------------------------------------------------------------------
#
# THE ADAPTER CONTRACT. A filing source is:
#
#     def source(state: str, office: str) -> list[CandidateRecord]
#
#   - It returns candidates who have FILED or QUALIFIED for the 2026 ballot,
#     challengers included. That is the whole reason it exists.
#   - It sets `status` to what the state office actually calls it ("filed",
#     "qualified", "on_ballot", "withdrawn"), not a normalised guess -- the
#     distinction between filed and qualified decides whether the race is real.
#   - It sets `source_url` to the page a human can open to check the row. A
#     record nobody can verify is not usable as evidence downstream, and
#     campaigns_score.py will refuse to score on it.
#   - It raises on transport failure and returns [] on "nothing here". gather()
#     tells those apart; collapsing them hides an outage as an empty state.
#   - It must not read from FEC filings. See the module docstring.
#
# Ships EMPTY. Fill it one state at a time, verifying each against the live
# endpoint, starting with the states holding the races you actually care about.
# A speculative adapter written against a response shape nobody has looked at
# fails silently and is worse than an obviously missing one.
#
STATE_FILING_SOURCES: dict[str, Callable[[str, str], list[CandidateRecord]]] = {}


def filing_coverage() -> dict[str, object]:
    """What the candidate list can currently cover. Call this before promising
    anyone a national list -- with an empty registry the honest answer is
    'incumbents only', and that should be visible rather than inferred from a
    thin result set."""
    states = sorted(STATE_FILING_SOURCES)
    return {
        "states_with_filing_source": states,
        "state_count": len(states),
        "has_challenger_coverage": bool(states),
        "incumbent_backends": sorted(INCUMBENT_BACKENDS),
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def merge(records: list[CandidateRecord]) -> list[CandidateRecord]:
    """Collapse duplicates on identity, preferring the record that carries more.

    A filing row beats a roster row for the same person: the filing is the
    ballot-access fact and the roster is context. Within that, prefer whichever
    has a contact route, then whichever has more fields filled.
    """
    def rank(record: CandidateRecord) -> tuple:
        from_filing = record.found_by.startswith("filing:")
        filled = sum(1 for value in (record.campaign_url, record.contact_email,
                                     record.contact_name, record.district,
                                     record.status, record.source_url) if value)
        return (from_filing, record.is_contactable(), filled)

    best: dict[tuple, CandidateRecord] = {}
    for record in records:
        key = record.identity()
        if key not in best or rank(record) > rank(best[key]):
            best[key] = record
    return sorted(best.values(),
                  key=lambda r: (r.state, r.office, r.district, _fold(r.name)))


def gather(state: str, office: str = "", *,
           include_incumbents: bool = True,
           filing_sources: Optional[dict] = None,
           incumbent_backends: Optional[dict] = None) -> list[CandidateRecord]:
    """Ask every source for this state at once and return what came back.

    An empty list is a legitimate outcome and means nothing was found -- which
    the caller should say, rather than invent around. Check LAST_RUN to tell
    "nothing found" from "everything failed": they produce the same [] and mean
    entirely different things.
    """
    state = (state or "").strip().upper()
    office = (office or "").strip()
    if not state:
        return []

    filings = STATE_FILING_SOURCES if filing_sources is None else filing_sources
    roster = INCUMBENT_BACKENDS if incumbent_backends is None else incumbent_backends

    jobs: list[tuple[str, Callable[[], list[CandidateRecord]]]] = []
    for name, fn in filings.items():
        if name.upper() not in (state, "*"):
            continue
        jobs.append((f"filing:{name}",
                     lambda fn=fn: fn(state, office)))
    if include_incumbents:
        for name, fn in roster.items():
            jobs.append((name, lambda fn=fn: fn(state, office)))

    outcomes: dict[str, object] = {}

    def attempt(job) -> list[CandidateRecord]:
        name, call = job
        try:
            found = _cached(f"{name}|{state}|{office}", call) or []
            outcomes[name] = len(found)
            return found
        except RateLimited:
            outcomes[name] = "rate-limited"
            return []
        except Exception as exc:                      # noqa: BLE001
            # One backend's bad day is not the run's. Record and carry on.
            outcomes[name] = f"{type(exc).__name__}: {exc}"[:160]
            return []

    collected: list[CandidateRecord] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            for found in pool.map(attempt, jobs):
                collected.extend(found)

    for record in collected:
        if record.found_by.startswith("filing:") or not record.found_by:
            record.found_by = record.found_by or "filing"

    LAST_RUN.clear()
    LAST_RUN.update({"state": state, "office": office, "backends": outcomes,
                     "raw": len(collected)})
    merged = merge(collected)
    LAST_RUN["merged"] = len(merged)
    return merged
