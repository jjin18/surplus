"""campaigns_enrich.py : the missing middle — a sourced candidate becomes a
scoreable campaign.

campaigns_sources produces CandidateRecords: a name, a seat, maybe a website.
campaigns_score consumes Campaigns carrying Signals that each cite a URL. Until
this module, nothing turned the first into the second, so the two halves of the
product could not touch. This is that step, and it is where the product's one
real risk lives.

-----------------------------------------------------------------------------
THE RISK, STATED PLAINLY
-----------------------------------------------------------------------------
Every other module here fails loudly. An unconfigured adapter refuses; a parse
that keeps no rows raises; a rating with no date is ignored. Enrichment cannot
work that way, because its job is to form judgements ("this campaign runs a
large volunteer operation") from prose, and a language model asked to do that
will produce a fluent, specific, entirely invented answer as readily as a true
one. There is no exception for it to throw.

So the defence is structural rather than behavioural: A SIGNAL MAY ONLY CITE A
PAGE THAT WAS ACTUALLY RETRIEVED. Retrieval happens first and produces a fixed
pool of results. The model reads that pool and proposes signals, each naming
the URL it relied on. `verify()` then drops every proposal whose URL is not in
the pool -- not down-weights, drops -- and counts the drop.

That makes invention *inert* rather than *unlikely*. A model that fabricates a
source is not producing a slightly-wrong score; it is producing nothing, and
the count of what it tried says so. This is civic.py's rule ("an answer with an
empty ladder is refused, not rendered") applied one layer earlier: there, the
model may not answer without sources; here, it may not score without them.

Note what verify() does NOT do: match on domain. Allowing any URL from a host
in the pool would let a real domain launder an invented page, which is the
failure wearing a disguise. It matches the whole normalised URL.

-----------------------------------------------------------------------------
TWO KINDS OF SIGNAL, AND WHY THE SPLIT MATTERS
-----------------------------------------------------------------------------
DERIVED signals need no model and no network. Whether a race is rated a toss-up
is already known from the ratings snapshot; whether a campaign has a website is
already in the filing record. These are facts we hold, so they are computed
directly, cite the source they came from, and cannot be invented. `race_
competitive` is the heaviest signal in the table at 22 points, and it is
derived -- so the largest single contribution to a score never passes through a
model at all.

RESEARCHED signals -- scale, pain point, technology, volunteers, civic-tech
interest -- genuinely require reading about the campaign, and are the ones the
verification gate exists for.

An enrichment with no proposer supplied still works: it returns the derived
signals and reports that research was not attempted. That is a legitimate mode,
not a degraded one, and it is what makes this module usable before anyone has
wired a model to it.

-----------------------------------------------------------------------------
INJECTABLE THROUGHOUT
-----------------------------------------------------------------------------
`retrieve` and `propose` are both parameters. The defaults reach the network and
a model; every test here passes fakes. That also keeps this surface liftable:
nothing is imported from the CRM, and civic_sources is shared infrastructure
rather than a coupling to the relationship side.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from .campaigns_races import RaceRating, load_ratings
from .campaigns_score import (Campaign, Contactability, Evidence, Signal,
                              SIGNAL_WEIGHTS)
from .campaigns_sources import CandidateRecord

# Signals this module computes without a model. See the docstring: these are
# facts already in hand, so they are never at risk of invention.
DERIVED_SIGNALS = frozenset({"race_competitive", "digital_presence"})

# The rest need someone to read about the campaign.
RESEARCHED_SIGNALS = frozenset(SIGNAL_WEIGHTS) - DERIVED_SIGNALS

# How much of the evidence pool a proposer is shown. Enough for a judgement,
# not so much that one verbose page crowds out five others -- civic_sources
# caps snippets at 700 chars for the same reason.
MAX_POOL = 24

Retriever = Callable[[CandidateRecord], list[dict]]
Proposer = Callable[[CandidateRecord, list[dict]], list[dict]]


@dataclass
class EnrichmentReport:
    """What happened, in numbers that reconcile.

    `rejected_unsourced` is the one to watch. A proposer that scores well on it
    is reading the pool; one that does not is inventing, and the number says so
    before anything downstream is affected.
    """
    retrieved: int = 0
    derived: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected_unsourced: int = 0
    rejected_unknown_signal: int = 0
    rejected_empty_observation: int = 0
    researched: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> int:
        return (self.rejected_unsourced + self.rejected_unknown_signal
                + self.rejected_empty_observation)

    def as_dict(self) -> dict[str, object]:
        return {"retrieved": self.retrieved, "derived": self.derived,
                "proposed": self.proposed, "accepted": self.accepted,
                "rejected": self.rejected,
                "unsourced": self.rejected_unsourced,
                "unknown_signal": self.rejected_unknown_signal,
                "empty_observation": self.rejected_empty_observation,
                "researched": self.researched}


# ---------------------------------------------------------------------------
# URL identity : what "the same page" means to verify()
# ---------------------------------------------------------------------------

def url_key(url: str) -> str:
    """A comparable form of a URL.

    Scheme and a trailing slash are noise; host case is noise; PATH case is
    NOT, because plenty of servers serve different documents from paths that
    differ only in case. Query and fragment are kept, since a page identified
    by a query parameter is a different page.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    tail = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{tail}"


# ---------------------------------------------------------------------------
# Derived signals : facts already in hand
# ---------------------------------------------------------------------------

def _rating_for(record: CandidateRecord,
                ratings: Iterable[RaceRating]) -> Optional[RaceRating]:
    """The rating covering this seat, if one exists.

    Matched on state and office. District is compared only when the rating
    carries one -- the shipped snapshot records state-level toss-ups without
    district numbers, and treating a blank district as "no match" would throw
    away the whole rating rather than the part of it that is known.
    """
    state = (record.state or "").upper()
    office = (record.office or "").strip().lower()
    for rating in ratings:
        if (rating.state or "").upper() != state:
            continue
        if (rating.office or "").strip().lower() != office:
            continue
        wanted = (rating.district or "").strip()
        if wanted and wanted != (record.district or "").strip():
            continue
        return rating
    return None


def derived_signals(record: CandidateRecord, *,
                    ratings: Optional[Iterable[RaceRating]] = None,
                    ) -> list[Signal]:
    """Signals computable from what is already known. No model, no network."""
    ratings = list(ratings if ratings is not None else load_ratings())
    out: list[Signal] = []

    rating = _rating_for(record, ratings)
    if rating and rating.source_url:
        # Strength by band: a toss-up is the full signal, a lean or likely
        # race is real but less urgent. An unrecognised band scores nothing
        # rather than a guessed middle.
        band = (rating.band or "").strip().lower().replace("-", " ")
        strength = {"toss up": 1.0, "tossup": 1.0,
                    "lean": 0.6, "likely": 0.3}.get(band, 0.0)
        if strength:
            out.append(Signal(
                "race_competitive", strength,
                Evidence(url=rating.source_url,
                         observed=f"{rating.source or 'rater'} rates this seat "
                                  f"{rating.band!r}",
                         retrieved=rating.as_of.isoformat() if rating.as_of else "")))

    if record.campaign_url:
        out.append(Signal(
            "digital_presence", 0.5,
            Evidence(url=record.campaign_url,
                     observed="campaign website listed on the state filing")))
    return out


def contactability_of(record: CandidateRecord) -> Contactability:
    """How reachable this campaign is, from what the filing carried.

    A named staffer with an address beats a general inbox beats a website with
    a form. campaigns_score treats this as a gate rather than a signal, so
    getting it wrong suppresses an otherwise good campaign entirely.
    """
    if record.contact_email and record.contact_name:
        return Contactability.NAMED_CONTACT
    if record.contact_email:
        return Contactability.CAMPAIGN_GENERAL
    if record.campaign_url:
        return Contactability.FORM_ONLY
    return Contactability.NONE


# ---------------------------------------------------------------------------
# Retrieval : the pool a proposal must cite
# ---------------------------------------------------------------------------

def research_question(record: CandidateRecord) -> str:
    """What to go and read about, in the words a search index matches on."""
    seat = record.office or "candidate"
    if record.district:
        seat = f"{seat} district {record.district}"
    return " ".join(f"{record.name} {seat} {record.state} 2026 campaign".split())


def retrieve(record: CandidateRecord, *,
             gather: Optional[Callable[..., list[dict]]] = None) -> list[dict]:
    """The evidence pool for one campaign.

    Defaults to civic_sources.gather -- every backend at once, tier-classified,
    failure-isolated, already built. Injectable so nothing here needs a network
    to be tested.
    """
    if gather is None:
        from .civic_sources import gather as gather  # noqa: PLW0127
    found = gather(research_question(record), record.state or "")
    return [row for row in (found or []) if isinstance(row, dict) and row.get("url")]


# ---------------------------------------------------------------------------
# Verification : the gate
# ---------------------------------------------------------------------------

def verify(proposals: Iterable[dict], pool: Iterable[dict],
           report: Optional[EnrichmentReport] = None) -> list[Signal]:
    """Turn proposals into Signals, dropping every one that is not sourced.

    A proposal survives only if its URL is in the retrieved pool, its signal
    name is one the scorer knows, and it says what was observed. Anything else
    is dropped and counted -- never repaired, never down-weighted, because a
    proposal citing a page that was not retrieved is not a weaker claim, it is
    a claim about a document nobody has seen.
    """
    report = report or EnrichmentReport()
    allowed = {url_key(row.get("url", "")) for row in pool}
    allowed.discard("")

    kept: dict[str, Signal] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            report.rejected_unknown_signal += 1
            continue
        report.proposed += 1

        name = str(proposal.get("signal") or "").strip()
        if name not in SIGNAL_WEIGHTS:
            report.rejected_unknown_signal += 1
            continue

        url = str(proposal.get("url") or "")
        if url_key(url) not in allowed:
            report.rejected_unsourced += 1
            continue

        observed = " ".join(str(proposal.get("observed") or "").split())
        if not observed:
            report.rejected_empty_observation += 1
            continue

        try:
            strength = max(0.0, min(1.0, float(proposal.get("strength", 1.0))))
        except (TypeError, ValueError):
            strength = 1.0

        # First accepted proposal per signal wins; a second is not additional
        # evidence for the scorer, which counts each signal once.
        if name in kept:
            continue
        kept[name] = Signal(name, strength, Evidence(url=url, observed=observed))
        report.accepted += 1

    return list(kept.values())


# ---------------------------------------------------------------------------
# The proposer contract
# ---------------------------------------------------------------------------

def build_prompt(record: CandidateRecord, pool: list[dict]) -> str:
    """What a model is asked. Pure, so the wording is reviewable and testable.

    The pool is numbered and the model is told to cite a URL from it verbatim.
    It is also told, in as many words, that a citation it invents will be
    discarded -- not to make it behave, but because a model that knows the rule
    tends to say "no signal" instead of reaching, and an empty answer here is a
    correct one.
    """
    lines = [
        "You are assessing whether a 2026 campaign is a good fit for a "
        "software product. Judge ONLY from the sources listed below.",
        "",
        f"Candidate: {record.name}",
        f"Seat: {record.office}"
        + (f", district {record.district}" if record.district else "")
        + f", {record.state}",
        "",
        "Sources:",
    ]
    for index, row in enumerate(pool[:MAX_POOL], start=1):
        lines.append(f"[{index}] {row.get('title', '')}".rstrip())
        lines.append(f"    url: {row.get('url', '')}")
        snippet = " ".join(str(row.get("snippet") or "").split())
        if snippet:
            lines.append(f"    {snippet[:700]}")
    lines += [
        "",
        "Signals you may report, and what each means:",
        "  campaign_scale       staff or budget beyond a handful of people",
        "  stated_pain_point    an operational problem they describe having",
        "  technology_signal    a tool they adopted that required a decision",
        "  volunteer_operation  an organised volunteer effort",
        "  civic_tech_interest  stated interest in civic technology or AI",
        "",
        "Return JSON only: a list of "
        '{\"signal\", \"strength\" (0-1), \"url\", \"observed\"}.',
        "`url` MUST be copied exactly from a source above. `observed` must be "
        "what that page says, not your conclusion about it.",
        "A signal you cannot source from the list above must be omitted. Any "
        "entry citing a url not listed above is DISCARDED, so an empty list is "
        "a better answer than an unsourced one.",
    ]
    return "\n".join(lines)


def parse_proposals(text: str) -> list[dict]:
    """Proposals out of a model's reply. Tolerant of fences and prose around
    the JSON, strict about the result being a list of objects."""
    raw = (text or "").strip()
    if not raw:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("["):
        bracket = re.search(r"\[.*\]", raw, re.S)
        if not bracket:
            return []
        raw = bracket.group(0)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [row for row in parsed if isinstance(row, dict)] if isinstance(parsed, list) else []


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

def enrich(record: CandidateRecord, *,
           retriever: Optional[Retriever] = None,
           propose: Optional[Proposer] = None,
           ratings: Optional[Iterable[RaceRating]] = None,
           ) -> tuple[Campaign, EnrichmentReport]:
    """A sourced candidate into a scoreable campaign.

    With no `propose`, only the derived signals are produced and the report
    says research was not attempted -- a real mode, not a broken one, and the
    one that works before any model is wired up.
    """
    report = EnrichmentReport()

    signals = derived_signals(record, ratings=ratings)
    report.derived = len(signals)
    if not signals:
        report.notes.append("no derived signals: unrated seat, no website")

    if propose is not None:
        pool = (retriever or retrieve)(record)
        report.retrieved = len(pool)
        report.researched = True
        if pool:
            researched = verify(propose(record, pool), pool, report)
            # Derived signals win: they are facts we hold, and the scorer
            # counts each signal once.
            have = {signal.name for signal in signals}
            signals += [s for s in researched if s.name not in have]
        else:
            report.notes.append("nothing retrieved: no researched signals")
    else:
        report.notes.append("no proposer supplied: derived signals only")

    campaign = Campaign(
        candidate=record.name,
        office=record.office,
        district=record.district,
        state=record.state,
        signals=tuple(signals),
        contactability=contactability_of(record),
    )
    return campaign, report


def enrich_all(records: Iterable[CandidateRecord], **kw
               ) -> list[tuple[Campaign, EnrichmentReport]]:
    """Every record, in order. Sequential on purpose: retrieval already fans
    out across backends per campaign, and a second layer of concurrency here
    would multiply load on the same free APIs civic_sources is careful with."""
    return [enrich(record, **kw) for record in records]
