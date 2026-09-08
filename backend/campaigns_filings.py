"""campaigns_filings.py : the shared half of every state filing adapter.

WHY THIS EXISTS. Fifty election offices publish candidate filings in fifty
formats, and the differences that matter are almost never the interesting part.
Every one of them needs the same things done: work out which office a row is
for, pull a district number out of a string a human typed, split a name that
may arrive as "DOE, PATRICIA A. (PAT)", decide whether the row is a real
candidacy or a withdrawal, and refuse rows that cannot be checked. Written
per-state, that is fifty chances to get the same problem slightly wrong.

So an adapter's job shrinks to: fetch the state's file, map its column names
onto FieldMap, hand the rows to `from_rows()`. Roughly fifteen lines, and the
awkward parts are here where they are tested once against the cases that
actually turn up in this data rather than fifty times against whatever each
author happened to think of.

TWO RULES THIS LAYER ENFORCES ON EVERY ADAPTER.

An unverifiable row is dropped, not guessed at. No `source_url` means nothing
downstream can check the row, and campaigns_score.py will refuse to score on it
anyway, so admitting it just moves the failure somewhere less obvious.

A withdrawal is not a candidate. Filing files keep withdrawn, disqualified and
superseded rows in place -- that is what makes them an audit trail -- and a
"withdrew in June" row read as a live candidacy is an email to somebody who is
not running. `WITHDRAWN_STATUSES` is the list, and `from_rows()` drops them by
default while `keep_withdrawn=True` exists for the caller reconciling a count
against the state's own published total.

Deliberately NOT here: HTTP. Adapters fetch; this parses. Keeping the split
means every one of these functions is testable against a string literal, which
is the only reason the ugly name cases below have real coverage.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .campaigns_sources import CandidateRecord

# The offices this product cares about, and the many ways states name them.
# Order matters: the first pattern that matches wins, so the more specific
# patterns ("state senate") must precede the looser ones ("senate").
_OFFICE_PATTERNS: list[tuple[str, str]] = [
    ("U.S. House", r"\b(u\.?\s?s\.?|united states|federal)?\s*"
                   r"(house|representative|congress(ional)?|congressman|congresswoman)\b"),
    ("U.S. Senate", r"\b(u\.?\s?s\.?|united states|federal)\s*senat(e|or)\b"),
    ("Governor", r"\bgovernor\b"),
    ("State Senate", r"\bstate\s+senat(e|or)\b"),
    ("State House", r"\bstate\s+(house|assembly|representative|delegate)\b"),
]

# A row in one of these states is an audit-trail entry, not a candidacy.
WITHDRAWN_STATUSES: frozenset[str] = frozenset({
    "withdrawn", "withdrew", "disqualified", "removed", "declined",
    "not qualified", "failed to qualify", "superseded", "deceased",
    "write-in withdrawn", "inactive",
})

# A joint ticket arrives as one name cell: "Jane Doe and John Roe". Split on a
# standalone conjunction, never inside a word -- "Alexander Anderson" and
# "Fernando Castillo" both contain the letters and must survive untouched.
_JOINT_TICKET = re.compile(r"\s+(?:and|&|/)\s+", re.IGNORECASE)

# Suffixes that are part of a name rather than part of the surname.
_SUFFIXES: frozenset[str] = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq", "dds", "cpa",
})


@dataclass(frozen=True)
class FieldMap:
    """Which column in this state's file holds what.

    Each value is a column name, or a tuple of candidates tried in order --
    states rename columns between cycles and a tuple costs nothing.
    """
    name: Any = "name"
    office: Any = "office"
    district: Any = ("district", "district_number", "dist")
    status: Any = ("status", "filing_status")
    email: Any = ("email", "contact_email")
    website: Any = ("website", "url", "campaign_website")
    contact: Any = ("committee", "contact", "treasurer")
    notes: Any = ("notes", "ballot_designation", "designation", "occupation")
    first_name: Any = ("first_name", "firstname", "given_name")
    last_name: Any = ("last_name", "lastname", "surname", "family_name")


@dataclass
class ParseReport:
    """What happened to the rows. A count that does not reconcile against the
    state's own published total is the earliest signal an adapter has drifted,
    so every drop is counted by reason rather than silently skipped."""
    seen: int = 0
    kept: int = 0
    dropped_no_name: int = 0
    dropped_no_source: int = 0
    dropped_unknown_office: int = 0
    dropped_withdrawn: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.seen - self.kept

    def as_dict(self) -> dict[str, object]:
        return {"seen": self.seen, "kept": self.kept, "dropped": self.dropped,
                "no_name": self.dropped_no_name,
                "no_source": self.dropped_no_source,
                "unknown_office": self.dropped_unknown_office,
                "withdrawn": self.dropped_withdrawn}


def split_ticket(name: str) -> tuple[str, str]:
    """A joint-ticket name into (head of ticket, running mate).

    Shared rather than per-state because it is not one state's quirk: Ohio has
    run governor and lieutenant governor as a joint ticket for decades, and
    Arizona joins it in 2026 -- Proposition 131 created the office and November
    3, 2026 is its first election, with running mates due by September 4.
    Several more states elect their two executives jointly.

    Keeping both names glued into one cell is the failure to avoid: "Jane Doe
    and John Roe" matches no campaign, no search, and no dedup key, so the row
    is present and useless. The head of the ticket is the candidate; the running
    mate is worth recording but is not who a software decision runs through.

    Returns the name unchanged with an empty mate when there is no split, which
    is every office except the joint executive ones.
    """
    text = " ".join((name or "").split())
    if not text:
        return "", ""
    parts = _JOINT_TICKET.split(text, maxsplit=1)
    if len(parts) == 2 and all(part.strip() for part in parts):
        return parts[0].strip(), parts[1].strip()
    return text, ""


def override(row: dict, fields: FieldMap, **values: str) -> dict:
    """Replace a field's value in a row, removing every alias that would win.

    THIS EXISTS BECAUSE OF A BUG WORTH REMEMBERING. FieldMap resolves a field
    by trying its alias columns IN ORDER, so setting row["office"] = "State
    House" does nothing when the map tries "office_name" first and the raw
    "Representative in the General Assembly" is still sitting there. The
    adapter looks correct, the mapping table looks correct, and every
    Pennsylvania statehouse candidate is silently filed as a candidate for the
    U.S. House.

    An adapter that has already canonicalised a value must therefore clear the
    alternatives rather than just add its answer alongside them. Doing that by
    hand in each adapter is the same bug waiting fifty times, so it is here.
    """
    updated = dict(row)
    for field_name, value in values.items():
        spec = getattr(fields, field_name, field_name)
        aliases = {spec} if isinstance(spec, str) else set(spec)
        aliases.add(field_name)
        lowered = {alias.strip().lower() for alias in aliases}
        for key in [k for k in updated if str(k).strip().lower() in lowered]:
            updated.pop(key)
        updated[field_name] = value
    return updated


def _pick(row: dict, spec: Any) -> str:
    """First non-empty value among the candidate column names, case-insensitively."""
    if spec is None:
        return ""
    keys = (spec,) if isinstance(spec, str) else tuple(spec)
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).strip().lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_office(raw: str) -> str:
    """Map a state's wording onto one of our office names. '' when unrecognised.

    Unrecognised is a real answer: filing files carry county coroner and soil
    and water conservation district, and silently bucketing those into
    'State House' would be worse than dropping them.
    """
    text = " ".join((raw or "").lower().split())
    if not text:
        return ""
    for office, pattern in _OFFICE_PATTERNS:
        if re.search(pattern, text):
            # "State Representative" must not read as U.S. House.
            if office == "U.S. House" and re.search(r"\bstate\b", text):
                continue
            if office == "U.S. Senate" and re.search(r"\bstate\b", text):
                continue
            return office
    return ""


def extract_district(raw: str) -> str:
    """Pull a district number out of whatever the state wrote.

    Handles "District 12", "12th", "CD-3", "003", "HD 45A". Returns "" when
    there is no number, which is correct for statewide offices. Leading zeros
    are stripped so "03" and "3" are the same district; a letter suffix is
    kept because in several states 45A and 45B are different seats.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d+)\s*([A-Za-z])?", text)
    if not match:
        return ""
    number = match.group(1).lstrip("0") or "0"
    suffix = (match.group(2) or "").upper()
    # A trailing ordinal ("12th") is not a sub-district letter.
    if suffix and text.lower()[match.start(2):match.start(2) + 2] in ("st", "nd", "rd", "th"):
        suffix = ""
    return f"{number}{suffix}"


def clean_name(raw: str, *, first: str = "", last: str = "") -> str:
    """Normalise a candidate name to 'First Last' order.

    Filing files use every convention there is. The cases below are the ones
    that actually appear: "DOE, PATRICIA A. (PAT)", all-caps, a nickname in
    quotes or parens, a suffix that must not become the surname.
    """
    if first or last:
        joined = f"{first.strip()} {last.strip()}".strip()
        if joined:
            return _titlecase(joined)

    text = " ".join((raw or "").split())
    if not text:
        return ""

    # Drop nicknames: (Pat) or "Pat".
    text = re.sub(r"[\(\"“][^\)\"”]*[\)\"”]", " ", text)
    text = " ".join(text.split())

    if "," in text:
        parts = [part.strip() for part in text.split(",") if part.strip()]
        surname, rest = parts[0], parts[1:]
        # A trailing suffix arrives as its own comma-separated part in some
        # files ("DOE, PATRICIA, III") and glued to the surname in others.
        suffix = ""
        if len(rest) > 1 and _strip_dots(rest[-1]).lower() in _SUFFIXES:
            suffix = rest.pop()
        given = " ".join(rest).strip()
        # "DOE, PATRICIA A." -> "Patricia A. Doe"; but keep "Doe, Jr." intact.
        if given and _strip_dots(given.split()[0]).lower() not in _SUFFIXES:
            text = f"{given} {surname} {suffix}".strip()
        else:
            text = f"{surname} {given} {suffix}".strip()

    return _titlecase(" ".join(text.split()))


def _strip_dots(token: str) -> str:
    return token.replace(".", "").replace(",", "").strip()


def _titlecase(text: str) -> str:
    """Title-case without destroying McDonald, O'Brien, Smith-Jones or III.

    Note what it does NOT preserve: lowercase particles. Both "VAN DER BERG,
    ANA" and "Ana van der Berg" come out as "Ana Van Der Berg". Which particles
    stay lowercase is a per-name fact rather than a per-language rule (the
    family decides), so a rule here would be confidently wrong about as often
    as it was right, and US filing offices mostly print the capitalised form
    anyway.

    The "trust the source" branch below is per TOKEN, not per name: it leaves
    alone a token that is already internally mixed-case ("deLuca", "MacDonald"),
    because that spelling can only have been deliberate. An all-lowercase token
    carries no such signal and gets capitalised. If a state's file turns out to
    preserve particles correctly, the fix is to skip _titlecase for that
    adapter, not to grow a particle list here.
    """
    out: list[str] = []
    for token in text.split():
        bare = _strip_dots(token).lower()
        if bare in _SUFFIXES:
            out.append(token.upper() if bare in {"ii", "iii", "iv", "v"}
                       else token.capitalize())
            continue
        if token.isupper() or token.islower():
            fixed = token.capitalize()
            for sep in ("'", "-", "."):
                if sep in fixed:
                    fixed = sep.join(part[:1].upper() + part[1:]
                                     for part in fixed.split(sep))
            if bare.startswith("mc") and len(bare) > 2:
                fixed = fixed[:2] + fixed[2:3].upper() + fixed[3:]
            out.append(fixed)
        else:
            out.append(token)          # already mixed case : trust the source
    return " ".join(out)


def is_withdrawn(status: str) -> bool:
    text = " ".join((status or "").lower().split())
    if not text:
        return False
    return any(marker in text for marker in WITHDRAWN_STATUSES)


def from_rows(rows: Iterable[dict], *, state: str, source_url: str,
              found_by: str, fields: Optional[FieldMap] = None,
              offices: Optional[Iterable[str]] = None,
              keep_withdrawn: bool = False) -> tuple[list[CandidateRecord], ParseReport]:
    """Turn a state's rows into CandidateRecords, and say what was dropped.

    `offices` filters to the offices you want; omit it to keep every office
    this layer recognises. `source_url` is the page a human can open to check
    the file -- it is required, because a record nobody can verify is not
    usable as evidence downstream.
    """
    fields = fields or FieldMap()
    state = (state or "").strip().upper()
    wanted = set(offices) if offices else None
    report = ParseReport()
    out: list[CandidateRecord] = []

    for row in rows:
        report.seen += 1

        name = clean_name(_pick(row, fields.name),
                          first=_pick(row, fields.first_name),
                          last=_pick(row, fields.last_name))
        if not name:
            report.dropped_no_name += 1
            continue

        row_source = _pick(row, "source_url") or source_url
        if not row_source.startswith("http"):
            report.dropped_no_source += 1
            continue

        office = normalize_office(_pick(row, fields.office))
        if not office or (wanted and office not in wanted):
            report.dropped_unknown_office += 1
            continue

        status = _pick(row, fields.status)
        if is_withdrawn(status) and not keep_withdrawn:
            report.dropped_withdrawn += 1
            continue

        district = extract_district(_pick(row, fields.district))
        if office in ("U.S. Senate", "Governor"):
            district = ""          # statewide : a district here is a data error

        out.append(CandidateRecord(
            name=name,
            office=office,
            state=state,
            district=district,
            status=status.lower() or "filed",
            campaign_url=_pick(row, fields.website),
            contact_email=_pick(row, fields.email),
            contact_name=_pick(row, fields.contact),
            source_url=row_source,
            found_by=found_by,
            notes=_pick(row, fields.notes),
        ))
        report.kept += 1

    return out, report
