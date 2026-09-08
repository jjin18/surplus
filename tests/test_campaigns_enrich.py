"""
The enrichment bridge, and mostly the one rule that makes it safe.

Everything else in this package fails loudly. Enrichment cannot: its job is to
form judgements from prose, and a model asked to do that will invent a fluent
specific answer as readily as a true one, with no exception to throw. So the
defence is that an invented citation is INERT -- dropped, counted, never
scored. These tests attack that gate rather than exercise the happy path,
because the happy path is not where the risk is.
"""
from __future__ import annotations

from datetime import date

import pytest

from backend import campaigns_enrich as en
from backend import campaigns_races as races
from backend.campaigns_score import Contactability, score_campaign
from backend.campaigns_sources import CandidateRecord


def rec(**kw) -> CandidateRecord:
    base = dict(name="Ada Keller", office="U.S. House", state="CA",
                district="9", source_url="https://sos.ca.gov/list",
                found_by="filing:ca")
    base.update(kw)
    return CandidateRecord(**base)


POOL = [
    {"url": "https://adakeller.example/volunteers", "title": "Volunteer",
     "snippet": "Our 400 volunteers knocked 12,000 doors last weekend."},
    {"url": "https://news.example/ca-09-field", "title": "Field operation",
     "snippet": "The campaign has hired a 20-person field staff."},
]


def proposer(*proposals):
    """A stand-in for the model. Tests never need an API key."""
    return lambda record, pool: list(proposals)


# --------------------------------------------------------------------------
# The gate: a citation that was not retrieved is inert
# --------------------------------------------------------------------------

def test_a_proposal_citing_an_unretrieved_url_is_dropped():
    """The whole safety property. An invented source scores nothing."""
    report = en.EnrichmentReport()
    signals = en.verify([
        {"signal": "volunteer_operation", "strength": 1.0,
         "url": "https://adakeller.example/invented-page",
         "observed": "a page that was never retrieved"},
    ], POOL, report)

    assert signals == []
    assert report.rejected_unsourced == 1
    assert report.accepted == 0


def test_a_real_domain_does_not_launder_an_invented_page():
    """Matching on host would let a genuine domain carry a fabricated path,
    which is the failure wearing a disguise."""
    report = en.EnrichmentReport()
    signals = en.verify([
        {"signal": "volunteer_operation", "strength": 1.0,
         "url": "https://adakeller.example/a-page-that-does-not-exist",
         "observed": "plausible but unsourced"},
    ], POOL, report)
    assert signals == [] and report.rejected_unsourced == 1


def test_a_sourced_proposal_is_accepted():
    report = en.EnrichmentReport()
    signals = en.verify([
        {"signal": "volunteer_operation", "strength": 0.8,
         "url": "https://adakeller.example/volunteers",
         "observed": "400 volunteers knocked 12,000 doors"},
    ], POOL, report)

    assert len(signals) == 1
    assert signals[0].name == "volunteer_operation"
    assert signals[0].evidence.url == "https://adakeller.example/volunteers"
    assert report.accepted == 1 and report.rejected == 0


def test_one_invented_proposal_does_not_take_the_sourced_ones_with_it():
    report = en.EnrichmentReport()
    signals = en.verify([
        {"signal": "volunteer_operation", "strength": 1.0,
         "url": "https://adakeller.example/volunteers", "observed": "400 volunteers"},
        {"signal": "campaign_scale", "strength": 1.0,
         "url": "https://invented.example/nope", "observed": "made up"},
    ], POOL, report)

    assert [s.name for s in signals] == ["volunteer_operation"]
    assert report.accepted == 1 and report.rejected_unsourced == 1


@pytest.mark.parametrize("url,ok", [
    ("https://adakeller.example/volunteers", True),
    ("http://adakeller.example/volunteers", True),       # scheme is noise
    ("https://www.adakeller.example/volunteers", True),  # www is noise
    ("https://ADAKELLER.example/volunteers", True),      # host case is noise
    ("https://adakeller.example/volunteers/", True),     # trailing slash is noise
    ("https://adakeller.example/VOLUNTEERS", False),     # path case is NOT
    ("https://adakeller.example/volunteers?x=1", False),  # a query is a page
    ("https://adakeller.example", False),
    ("", False),
])
def test_url_matching_is_forgiving_about_noise_and_strict_about_identity(url, ok):
    report = en.EnrichmentReport()
    signals = en.verify([{"signal": "volunteer_operation", "strength": 1.0,
                          "url": url, "observed": "x"}], POOL, report)
    assert bool(signals) is ok


def test_an_unknown_signal_name_is_dropped():
    report = en.EnrichmentReport()
    signals = en.verify([{"signal": "vibes", "strength": 1.0,
                          "url": POOL[0]["url"], "observed": "x"}], POOL, report)
    assert signals == [] and report.rejected_unknown_signal == 1


def test_a_proposal_with_no_observation_is_dropped():
    """A citation with nothing said about it is not evidence, it is a link."""
    report = en.EnrichmentReport()
    signals = en.verify([{"signal": "campaign_scale", "strength": 1.0,
                          "url": POOL[0]["url"], "observed": "   "}], POOL, report)
    assert signals == [] and report.rejected_empty_observation == 1


def test_garbage_proposals_do_not_raise():
    report = en.EnrichmentReport()
    assert en.verify(["not a dict", None, 42], POOL, report) == []
    assert report.rejected_unknown_signal == 3


def test_strength_is_clamped_and_a_bad_one_does_not_drop_the_signal():
    report = en.EnrichmentReport()
    signals = en.verify([{"signal": "campaign_scale", "strength": "loads",
                          "url": POOL[1]["url"], "observed": "20 staff"}],
                        POOL, report)
    assert len(signals) == 1 and 0.0 <= signals[0].strength <= 1.0


def test_a_repeated_signal_is_counted_once():
    report = en.EnrichmentReport()
    signals = en.verify([
        {"signal": "campaign_scale", "strength": 1.0, "url": POOL[0]["url"],
         "observed": "first"},
        {"signal": "campaign_scale", "strength": 1.0, "url": POOL[1]["url"],
         "observed": "second"},
    ], POOL, report)
    assert len(signals) == 1 and signals[0].evidence.observed == "first"


def test_an_empty_pool_accepts_nothing():
    """Retrieval found nothing, so there is nothing anything could cite."""
    report = en.EnrichmentReport()
    assert en.verify([{"signal": "campaign_scale", "strength": 1.0,
                       "url": POOL[0]["url"], "observed": "x"}], [], report) == []
    assert report.rejected_unsourced == 1


# --------------------------------------------------------------------------
# Derived signals: facts we already hold
# --------------------------------------------------------------------------

def test_a_rated_seat_derives_race_competitive_citing_the_rater():
    ratings = races.load_ratings()
    signals = en.derived_signals(rec(state="CA", office="U.S. House"),
                                 ratings=ratings)
    race = next(s for s in signals if s.name == "race_competitive")
    assert race.strength == 1.0
    assert race.evidence.url.startswith("http")
    assert "cook" in race.evidence.observed.lower()


def test_the_heaviest_signal_never_passes_through_a_model():
    """race_competitive is 22 points, the largest single contribution, and it
    is derived -- so the biggest part of a score cannot be invented."""
    assert "race_competitive" in en.DERIVED_SIGNALS
    from backend.campaigns_score import SIGNAL_WEIGHTS
    assert SIGNAL_WEIGHTS["race_competitive"] == max(SIGNAL_WEIGHTS.values())


def test_an_unrated_seat_derives_no_race_signal():
    signals = en.derived_signals(rec(state="WY"), ratings=races.load_ratings())
    assert not [s for s in signals if s.name == "race_competitive"]


def test_a_website_derives_digital_presence_citing_itself():
    signals = en.derived_signals(rec(campaign_url="https://adakeller.example"),
                                 ratings=[])
    digital = next(s for s in signals if s.name == "digital_presence")
    assert digital.evidence.url == "https://adakeller.example"


def test_bands_weaker_than_toss_up_score_lower():
    def rating(band):
        return races.RaceRating(state="CA", office="U.S. House", band=band,
                                source="test", source_url="https://r.example/x",
                                as_of=date(2026, 9, 1))
    strengths = {}
    for band in ("toss-up", "lean", "likely"):
        got = en.derived_signals(rec(), ratings=[rating(band)])
        strengths[band] = next(s.strength for s in got
                               if s.name == "race_competitive")
    assert strengths["toss-up"] > strengths["lean"] > strengths["likely"]


def test_an_unrecognised_band_scores_nothing_rather_than_a_guessed_middle():
    rating = races.RaceRating(state="CA", office="U.S. House", band="spicy",
                              source="test", source_url="https://r.example/x",
                              as_of=date(2026, 9, 1))
    assert not [s for s in en.derived_signals(rec(), ratings=[rating])
                if s.name == "race_competitive"]


def test_a_rating_with_no_url_cannot_be_cited_so_is_not_used():
    rating = races.RaceRating(state="CA", office="U.S. House", band="toss-up",
                              source="test", as_of=date(2026, 9, 1))
    assert not [s for s in en.derived_signals(rec(), ratings=[rating])
                if s.name == "race_competitive"]


def test_a_districtless_rating_still_matches_the_seat():
    """The shipped snapshot records state-level toss-ups with no district;
    requiring a match would throw away the part that is known."""
    rating = races.RaceRating(state="CA", office="U.S. House", band="toss-up",
                              source="cook", source_url="https://r.example/x",
                              as_of=date(2026, 9, 1))
    assert [s for s in en.derived_signals(rec(district="47"), ratings=[rating])
            if s.name == "race_competitive"]


def test_a_rating_for_another_office_does_not_match():
    rating = races.RaceRating(state="CA", office="Governor", band="toss-up",
                              source="cook", source_url="https://r.example/x",
                              as_of=date(2026, 9, 1))
    assert not [s for s in en.derived_signals(rec(office="U.S. House"),
                                              ratings=[rating])
                if s.name == "race_competitive"]


# --------------------------------------------------------------------------
# Contactability, which gates the score
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kw,expected", [
    ({"contact_email": "a@b.org", "contact_name": "Sam"}, Contactability.NAMED_CONTACT),
    ({"contact_email": "a@b.org"}, Contactability.CAMPAIGN_GENERAL),
    ({"campaign_url": "https://x.example"}, Contactability.FORM_ONLY),
    ({}, Contactability.NONE),
])
def test_contactability_from_the_filing(kw, expected):
    assert en.contactability_of(rec(**kw)) is expected


# --------------------------------------------------------------------------
# enrich(): the bridge end to end
# --------------------------------------------------------------------------

def test_without_a_proposer_it_returns_derived_signals_and_says_so():
    """A real mode, not a broken one: this is what works before any model is
    wired up."""
    campaign, report = en.enrich(rec(campaign_url="https://adakeller.example"),
                                 ratings=races.load_ratings())
    assert report.researched is False
    assert report.derived == len(campaign.signals) == 2
    assert any("no proposer" in n for n in report.notes)


def test_with_a_proposer_the_sourced_signals_are_added():
    campaign, report = en.enrich(
        rec(campaign_url="https://adakeller.example"),
        retriever=lambda r: POOL,
        propose=proposer({"signal": "volunteer_operation", "strength": 0.9,
                          "url": POOL[0]["url"], "observed": "400 volunteers"}),
        ratings=races.load_ratings())

    names = {s.name for s in campaign.signals}
    assert names == {"race_competitive", "digital_presence", "volunteer_operation"}
    assert report.researched is True and report.accepted == 1
    assert report.retrieved == 2


def test_an_inventing_proposer_changes_the_score_by_nothing():
    """The property that matters, stated as a score rather than a count."""
    honest, _ = en.enrich(rec(), retriever=lambda r: POOL,
                          propose=proposer(), ratings=races.load_ratings())
    lying, report = en.enrich(
        rec(), retriever=lambda r: POOL,
        propose=proposer(
            {"signal": "campaign_scale", "strength": 1.0,
             "url": "https://invented.example/a", "observed": "huge"},
            {"signal": "volunteer_operation", "strength": 1.0,
             "url": "https://invented.example/b", "observed": "thousands"}),
        ratings=races.load_ratings())

    assert score_campaign(honest).score == score_campaign(lying).score
    assert report.rejected_unsourced == 2


def test_a_derived_signal_is_not_overridden_by_a_proposed_one():
    """Facts we hold beat a model's reading of them."""
    campaign, _ = en.enrich(
        rec(campaign_url="https://adakeller.example"),
        retriever=lambda r: POOL + [{"url": "https://p.example/x", "title": "",
                                     "snippet": ""}],
        propose=proposer({"signal": "digital_presence", "strength": 1.0,
                          "url": "https://p.example/x", "observed": "a site"}),
        ratings=races.load_ratings())
    digital = next(s for s in campaign.signals if s.name == "digital_presence")
    assert digital.evidence.url == "https://adakeller.example"


def test_nothing_retrieved_is_reported_not_hidden():
    campaign, report = en.enrich(rec(), retriever=lambda r: [],
                                 propose=proposer(), ratings=races.load_ratings())
    assert report.retrieved == 0
    assert any("nothing retrieved" in n for n in report.notes)


def test_the_campaign_handed_to_the_scorer_carries_no_party():
    """The scorer's rule 1 survives the bridge."""
    campaign, _ = en.enrich(rec(notes="Democratic, Educator"),
                            ratings=races.load_ratings())
    blob = " ".join(str(v).lower() for v in vars(campaign).values())
    for party in ("democratic", "republican"):
        assert party not in blob


def test_the_result_scores_without_further_work():
    campaign, _ = en.enrich(
        rec(campaign_url="https://adakeller.example",
            contact_email="team@adakeller.example", contact_name="Sam Field"),
        retriever=lambda r: POOL,
        propose=proposer({"signal": "volunteer_operation", "strength": 1.0,
                          "url": POOL[0]["url"], "observed": "400 volunteers"}),
        ratings=races.load_ratings())
    result = score_campaign(campaign)
    assert result.score > 0 and result.is_actionable
    assert all(c.evidence.url.startswith("http") for c in result.contributions)


def test_enrich_all_preserves_order():
    records = [rec(name="A"), rec(name="B"), rec(name="C")]
    got = en.enrich_all(records, ratings=races.load_ratings())
    assert [c.candidate for c, _ in got] == ["A", "B", "C"]


# --------------------------------------------------------------------------
# The prompt and the reply
# --------------------------------------------------------------------------

def test_the_prompt_carries_the_pool_and_the_discard_rule():
    prompt = en.build_prompt(rec(), POOL)
    assert POOL[0]["url"] in prompt and POOL[1]["url"] in prompt
    assert "DISCARDED" in prompt
    assert "Ada Keller" in prompt


def test_the_prompt_offers_only_researched_signals():
    """Asking for the derived ones would invite a model to restate a fact we
    already hold, from a worse source."""
    prompt = en.build_prompt(rec(), POOL)
    for name in en.RESEARCHED_SIGNALS:
        assert name in prompt
    for name in en.DERIVED_SIGNALS:
        assert name not in prompt


@pytest.mark.parametrize("reply", [
    '[{"signal":"campaign_scale","strength":1,"url":"u","observed":"o"}]',
    '```json\n[{"signal":"campaign_scale","strength":1,"url":"u","observed":"o"}]\n```',
    'Here you go:\n[{"signal":"campaign_scale","strength":1,"url":"u","observed":"o"}]\nhope that helps',
])
def test_proposals_parse_out_of_the_shapes_a_model_replies_in(reply):
    assert en.parse_proposals(reply) == [
        {"signal": "campaign_scale", "strength": 1, "url": "u", "observed": "o"}]


@pytest.mark.parametrize("reply", ["", "   ", "no signals found", "{}", "not json [",
                                   '{"signal":"x"}'])
def test_an_unparseable_reply_is_no_proposals_rather_than_an_error(reply):
    assert en.parse_proposals(reply) == []
