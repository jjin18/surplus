"""campaigns_races.py : what is actually on the ballot in November 2026.

THIS IS THE DENOMINATOR. "Find every candidate in the midterm" is not a
meaningful goal until you can say how many seats there are to find candidates
for -- otherwise a list of 900 people is indistinguishable from a list that is
missing half the country, and there is no way to tell a thin result from a
broken backend. Everything here exists so that campaigns_sources.gather() can
be checked against a known total rather than trusted.

-----------------------------------------------------------------------------
THE SPLIT THAT MATTERS: SCHEDULE FACTS vs RATINGS
-----------------------------------------------------------------------------
Two kinds of fact get confused in every "target list" spreadsheet, and mixing
them is why those spreadsheets rot.

SCHEDULE FACTS are law. Which Senate class is up, how many House seats a state
has, whether a state elects its governor in midterms -- these are set by the
Constitution, by the 2020 census apportionment (fixed until the 2030 one), and
by state constitutions. They do not move during a cycle. They are hardcoded
below and covered by arithmetic tests: if apportionment does not sum to 435,
something is wrong with this file rather than with the world.

RATINGS are somebody's opinion on a Tuesday. Which races are "competitive"
depends entirely on where you draw the band -- as of this writing Cook rates
18 House toss-ups while a wider band gives roughly 50 -- and it changes as
candidates file, drop out, and get redistricted. So ratings are NOT hardcoded
here. They are injected, they carry the date they were published, and
`stale_after_days` decides when they stop counting as current. A ratings table
baked into source is a target list that silently goes wrong.

The practical consequence: `priority_states()` derives a build order from
whatever ratings you hand it. Point it at a different band, or a fresher pull,
and the order re-derives. Nothing in this module needs editing when the map
changes, which is the entire design goal.

-----------------------------------------------------------------------------
THE SCALE PROBLEM NOBODY BUDGETS FOR
-----------------------------------------------------------------------------
Federal and gubernatorial races are 506 seats. State legislative races in the
same election are several thousand. So "all candidates in any part of the
midterm" is, numerically, a state-legislature problem wearing a congressional
costume -- and those are the races least covered by any national data source,
scattered across the same fifty filing offices.

`LEGISLATIVE_2026` records which states hold them so the gap is visible rather
than discovered late. It deliberately does NOT carry seat counts: the fraction
of a chamber up for election varies by state and by chamber (staggered senates
elect half), and a made-up precise number is worse than an honest absence.
Coverage reporting therefore reports federal + gubernatorial precisely and
flags legislative separately, rather than blending them into one reassuring
percentage.

Pure data + pure functions. No DB, no network, no clock beyond what a caller
passes in.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

_DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"
RATINGS_FILE = _DATA_DIR / "house_ratings_2026.json"

# Election day.
ELECTION_DAY = date(2026, 11, 3)

# --- Schedule fact: House apportionment ------------------------------------
# From the 2020 census, in force for the elections of 2022 through 2030. Sums
# to 435 -- test_house_apportionment_sums_to_435 is the guard on typos here.
HOUSE_SEATS: dict[str, int] = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5, "DE": 1,
    "FL": 28, "GA": 14, "HI": 2, "ID": 2, "IL": 17, "IN": 9, "IA": 4, "KS": 4,
    "KY": 6, "LA": 6, "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8, "MS": 4,
    "MO": 8, "MT": 2, "NE": 3, "NV": 4, "NH": 2, "NJ": 12, "NM": 3, "NY": 26,
    "NC": 14, "ND": 1, "OH": 15, "OK": 5, "OR": 6, "PA": 17, "RI": 2, "SC": 7,
    "SD": 1, "TN": 9, "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10, "WV": 2,
    "WI": 8, "WY": 1,
}

# --- Schedule fact: Senate Class 2 -----------------------------------------
# Class 2 was last elected in 2020 and is up in 2026: 33 regular seats. Special
# elections (a resignation or death mid-term) are NOT here -- they are events,
# not schedule, so they arrive through `specials` on the ballot query.
SENATE_CLASS_2: frozenset[str] = frozenset({
    "AL", "AK", "AR", "CO", "DE", "GA", "ID", "IL", "IA", "KS", "KY", "LA",
    "ME", "MA", "MI", "MN", "MS", "MT", "NE", "NH", "NJ", "NM", "NC", "OK",
    "OR", "RI", "SC", "SD", "TN", "TX", "VA", "WV", "WY",
})

# --- Schedule fact: governorships ------------------------------------------
# 36 states elect a governor in 2026. (Three territories also do; they are out
# of scope here because none of the filing sources cover them.)
GOVERNOR_2026: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL",
    "IA", "KS", "ME", "MD", "MA", "MI", "MN", "NE", "NV", "NH", "NM", "NY",
    "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "VT", "WI", "WY",
})

# --- Schedule fact: state legislative elections ----------------------------
# Every state except the four that run their legislative elections in odd years
# (Louisiana, Mississippi, New Jersey, Virginia). No seat counts: see the
# module docstring on why a plausible number would be worse than none.
_ODD_YEAR_LEGISLATURES: frozenset[str] = frozenset({"LA", "MS", "NJ", "VA"})
LEGISLATIVE_2026: frozenset[str] = frozenset(HOUSE_SEATS) - _ODD_YEAR_LEGISLATURES

STATES: frozenset[str] = frozenset(HOUSE_SEATS)


@dataclass(frozen=True)
class Seat:
    """One seat on the 2026 ballot."""
    state: str
    office: str                # "U.S. House" | "U.S. Senate" | "Governor"
    district: str = ""         # House district number; "" for statewide
    is_special: bool = False

    @property
    def key(self) -> str:
        base = f"{self.state}-{self.office}"
        if self.district:
            base = f"{base}-{self.district}"
        return f"{base}-special" if self.is_special else base


@dataclass(frozen=True)
class RaceRating:
    """Somebody's competitiveness call, with the date it was made.

    `band` is the rater's own word ("toss-up", "lean", "likely") kept verbatim
    rather than normalised, because raters do not mean the same thing by them
    and flattening the vocabulary loses the only signal that distinguishes
    an 18-race list from a 50-race one.
    """
    state: str
    office: str
    district: str = ""
    band: str = ""
    source: str = ""           # who rated it, e.g. "cook"
    # The rater's own page. Carried so a signal derived from this rating can
    # cite something a reader can open -- campaigns_score refuses to score on
    # evidence with no URL, and a rating is evidence like any other.
    source_url: str = ""
    as_of: Optional[date] = None

    def is_current(self, today: date, stale_after_days: int = 45) -> bool:
        if self.as_of is None:
            return False       # undated is not current, it is unknown
        age = (today - self.as_of).days
        return 0 <= age <= stale_after_days


def house_seats(state: str) -> int:
    return HOUSE_SEATS.get((state or "").upper(), 0)


def seats_for_state(state: str, *, specials: Iterable[Seat] = ()) -> list[Seat]:
    """Every federal + gubernatorial seat on the 2026 ballot in one state."""
    state = (state or "").upper()
    if state not in STATES:
        return []

    seats = [Seat(state, "U.S. House", str(n))
             for n in range(1, HOUSE_SEATS[state] + 1)]
    if state in SENATE_CLASS_2:
        seats.append(Seat(state, "U.S. Senate"))
    if state in GOVERNOR_2026:
        seats.append(Seat(state, "Governor"))
    seats.extend(s for s in specials if s.state.upper() == state)
    return seats


def all_seats(*, specials: Iterable[Seat] = ()) -> list[Seat]:
    """The national 2026 ballot for federal + gubernatorial office.

    At-large states get district "1" rather than "": the state's own filing
    office numbers it, and matching that avoids a join failing on a blank.
    """
    specials = list(specials)
    out: list[Seat] = []
    for state in sorted(STATES):
        out.extend(seats_for_state(state, specials=specials))
    return out


def ballot_totals(*, specials: Iterable[Seat] = ()) -> dict[str, int]:
    seats = all_seats(specials=specials)
    return {
        "house": sum(1 for s in seats if s.office == "U.S. House"),
        "senate": sum(1 for s in seats if s.office == "U.S. Senate"),
        "governor": sum(1 for s in seats if s.office == "Governor"),
        "total": len(seats),
        "states_with_legislative_elections": len(LEGISLATIVE_2026),
    }


def priority_states(ratings: Iterable[RaceRating], *,
                    today: Optional[date] = None,
                    bands: Iterable[str] = ("toss-up", "tossup", "toss up"),
                    stale_after_days: int = 45) -> list[tuple[str, int]]:
    """Build order for filing adapters: states with the most competitive races
    first, ties broken by House delegation size then alphabetically.

    Ratings that are undated or older than `stale_after_days` are ignored --
    building this quarter's target list on last quarter's map is the failure
    this guards against, and a silently stale list looks exactly like a fresh
    one. Widen `bands` to include "lean"/"likely" for the larger target set;
    the band vocabulary is the rater's, so pass what your source actually says.
    """
    today = today or date.today()
    wanted = {b.strip().lower() for b in bands}

    counted: dict[str, int] = {}
    for rating in ratings:
        if not rating.is_current(today, stale_after_days):
            continue
        if (rating.band or "").strip().lower() not in wanted:
            continue
        state = (rating.state or "").upper()
        if state in STATES:
            counted[state] = counted.get(state, 0) + 1

    return sorted(counted.items(),
                  key=lambda pair: (-pair[1], -house_seats(pair[0]), pair[0]))


def load_ratings(path: Optional[pathlib.Path] = None) -> list[RaceRating]:
    """Read a dated ratings snapshot off disk.

    The file's own `as_of` is stamped onto every rating it carries, so a
    snapshot nobody has refreshed ages out of `priority_states()` on its own
    rather than being trusted indefinitely. A missing file is an empty list --
    no ratings is a legitimate state and should not stop a run.
    """
    path = path or RATINGS_FILE
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError):
        return []

    as_of: Optional[date]
    try:
        as_of = date.fromisoformat(str(blob.get("as_of", "")))
    except ValueError:
        as_of = None            # undated : is_current() will reject these

    source = str(blob.get("source") or "")
    source_url = str(blob.get("source_url") or "")
    out: list[RaceRating] = []
    for row in blob.get("ratings") or []:
        state = str(row.get("state") or "").upper()
        if state not in STATES:
            continue
        out.append(RaceRating(
            state=state,
            office=str(row.get("office") or ""),
            district=str(row.get("district") or ""),
            band=str(row.get("band") or ""),
            source=source,
            source_url=str(row.get("source_url") or source_url),
            as_of=as_of,
        ))
    return out


def coverage(filing_states: Iterable[str], *,
             specials: Iterable[Seat] = ()) -> dict[str, object]:
    """How much of the 2026 ballot the candidate list can actually see.

    Reports federal + gubernatorial coverage precisely, and reports the
    legislative gap separately rather than folding it into one number -- a
    single blended percentage would let "we cover 40% of the midterm" mean
    either "most of Congress" or "a handful of statehouses", which are not the
    same claim to make to anyone.
    """
    covered = {(s or "").upper() for s in filing_states} & STATES
    seats = all_seats(specials=specials)
    reachable = [s for s in seats if s.state in covered]

    return {
        "states_covered": sorted(covered),
        "state_count": len(covered),
        "seats_total": len(seats),
        "seats_reachable": len(reachable),
        "seats_by_office": {
            office: sum(1 for s in reachable if s.office == office)
            for office in ("U.S. House", "U.S. Senate", "Governor")
        },
        "pct_federal_and_gubernatorial": (
            round(100 * len(reachable) / len(seats), 1) if seats else 0.0),
        # Reported, never blended into the percentage above.
        "legislative_states_total": len(LEGISLATIVE_2026),
        "legislative_states_covered": len(covered & LEGISLATIVE_2026),
        "legislative_seat_counts_known": False,
    }
