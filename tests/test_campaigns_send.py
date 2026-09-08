"""
The send gate. Three ways an outbound campaign email is a mistake, none of
which raises on its own, all of which are refused here.

The tests are mostly attempts to get something past the gate, because the gate
is the whole module. A checklist that lives in a docstring is one somebody
skips under time pressure; these are the assertions that make it not a
checklist.
"""
from __future__ import annotations

import pytest

from backend import campaigns_send as snd


IDENTITY = snd.SendingIdentity(
    from_address="jia@outreach.surpluslayer.com",
    from_name="Jia at Surplus",
    postal_address="2261 Market St #5000, San Francisco, CA 94114",
    unsubscribe="https://outreach.surpluslayer.com/unsubscribe",
)


def msg(**kw) -> snd.Message:
    base = dict(to="team@adakeller.example", subject="AI infrastructure",
                body="Hi Ada, I noticed your volunteer programme.")
    base.update(kw)
    return snd.Message(**base)


# --------------------------------------------------------------------------
# The identity: incomplete means nothing may be sent
# --------------------------------------------------------------------------

def test_a_complete_identity_has_no_problems():
    assert IDENTITY.problems() == []


@pytest.mark.parametrize("field,value,fragment", [
    ("from_address", "not-an-email", "from_address"),
    ("from_name", "  ", "from_name"),
    ("unsubscribe", "just some text", "unsubscribe"),
    ("postal_address", "TBD", "postal_address"),
    ("postal_address", "Our Office", "postal_address"),
])
def test_an_incomplete_identity_names_the_problem(field, value, fragment):
    broken = snd.SendingIdentity(**{**vars(IDENTITY), field: value})
    assert any(fragment in p for p in broken.problems())


def test_a_placeholder_postal_address_is_caught():
    """A fake address is worse than a missing one: a missing one stops the
    send and a fake one does not."""
    for fake in ("TBD", "Our Office", "San Francisco", "n/a"):
        broken = snd.SendingIdentity(**{**vars(IDENTITY), "postal_address": fake})
        assert broken.problems(), fake


def test_problems_are_reported_together_not_one_at_a_time():
    broken = snd.SendingIdentity(from_address="x", from_name="",
                                 postal_address="", unsubscribe="")
    assert len(broken.problems()) == 4


def test_loading_an_unset_environment_refuses_and_says_what_is_missing():
    with pytest.raises(snd.NotConfigured) as caught:
        snd.load_identity(env={})
    message = str(caught.value)
    assert "SURPLUS_CAMPAIGNS_FROM_ADDRESS" in message
    assert "postal_address" in message


def test_loading_a_complete_environment_works():
    identity = snd.load_identity(env={
        "SURPLUS_CAMPAIGNS_FROM_ADDRESS": IDENTITY.from_address,
        "SURPLUS_CAMPAIGNS_FROM_NAME": IDENTITY.from_name,
        "SURPLUS_CAMPAIGNS_POSTAL_ADDRESS": IDENTITY.postal_address,
        "SURPLUS_CAMPAIGNS_UNSUBSCRIBE": IDENTITY.unsubscribe,
    })
    assert identity.domain == "outreach.surpluslayer.com"


def test_a_partially_set_environment_never_returns_a_half_identity():
    with pytest.raises(snd.NotConfigured):
        snd.load_identity(env={"SURPLUS_CAMPAIGNS_FROM_ADDRESS": "a@b.com"})


# --------------------------------------------------------------------------
# The footer, appended by code
# --------------------------------------------------------------------------

def test_finalize_adds_the_address_and_the_opt_out():
    ready = snd.finalize(msg(), IDENTITY)
    assert IDENTITY.postal_address in ready.body
    assert IDENTITY.unsubscribe in ready.body


def test_finalize_is_idempotent():
    once = snd.finalize(msg(), IDENTITY)
    twice = snd.finalize(once, IDENTITY)
    assert once.body == twice.body
    assert twice.body.count(IDENTITY.postal_address) == 1


def test_finalize_does_not_touch_the_subject_or_recipient():
    ready = snd.finalize(msg(), IDENTITY)
    assert ready.to == "team@adakeller.example"
    assert ready.subject == "AI infrastructure"


# --------------------------------------------------------------------------
# validate() reads the rendered body, not the template
# --------------------------------------------------------------------------

def test_a_message_missing_the_postal_address_is_invalid():
    """The case that matters: someone wrote the email by hand."""
    problems = snd.validate(msg(body="Hi Ada, quick question."), IDENTITY)
    assert any("postal address" in p for p in problems)


def test_a_message_missing_the_unsubscribe_is_invalid():
    body = f"Hi Ada.\n\n{IDENTITY.postal_address}"
    assert any("unsubscribe" in p for p in snd.validate(msg(body=body), IDENTITY))


def test_a_finalized_message_validates():
    assert snd.validate(snd.finalize(msg(), IDENTITY), IDENTITY) == []


@pytest.mark.parametrize("kw,fragment", [
    ({"to": "not-an-address"}, "recipient"),
    ({"subject": "   "}, "subject"),
    ({"body": ""}, "body"),
])
def test_obvious_faults_are_caught(kw, fragment):
    assert any(fragment in p for p in snd.validate(msg(**kw), IDENTITY))


# --------------------------------------------------------------------------
# Warmup: the failure nothing reports
# --------------------------------------------------------------------------

def test_the_ramp_starts_small_and_never_goes_backwards():
    caps = [snd.warmup_allowance(day) for day in range(1, 40)]
    assert caps[0] <= 20
    assert caps == sorted(caps), "the ramp must be monotonic"


def test_day_zero_and_before_send_nothing():
    assert snd.warmup_allowance(0) == 0
    assert snd.warmup_allowance(-5) == 0


def test_past_the_end_of_the_ramp_the_cap_holds():
    last = snd.WARMUP_RAMP[-1]
    assert snd.warmup_allowance(last[0]) == last[1]
    assert snd.warmup_allowance(last[0] + 500) == last[1]


def test_the_schedule_answers_when_five_hundred_are_reachable():
    """The question actually asked is not 'what is today's cap'."""
    schedule = snd.warmup_schedule()
    reached = next(r for r in schedule if r["cumulative"] >= 500)
    assert reached["day"] <= 10, "500 campaigns should be reachable in ~a week"
    assert schedule[0]["cumulative"] == schedule[0]["cap"]


def test_exceeding_todays_allowance_is_refused():
    with pytest.raises(snd.Refused, match="warmup"):
        snd.approve(msg(), IDENTITY, sent_today=20, warmup_day=1)


def test_within_todays_allowance_is_allowed():
    ready = snd.approve(msg(), IDENTITY, sent_today=19, warmup_day=1)
    assert IDENTITY.postal_address in ready.body


def test_the_refusal_explains_that_nothing_will_bounce():
    """The whole hazard is that this failure is silent."""
    with pytest.raises(snd.Refused) as caught:
        snd.approve(msg(), IDENTITY, sent_today=999, warmup_day=1)
    assert "not bounce" in str(caught.value)


# --------------------------------------------------------------------------
# Suppression: the opt-out made structural
# --------------------------------------------------------------------------

def test_a_suppressed_address_cannot_be_reached():
    suppression = snd.Suppression.of(["team@adakeller.example"])
    with pytest.raises(snd.Refused, match="opted out"):
        snd.approve(msg(), IDENTITY, suppression=suppression)


@pytest.mark.parametrize("stored,attempted", [
    ("team@adakeller.example", "TEAM@adakeller.example"),
    ("team@adakeller.example", "  team@adakeller.example  "),
    ("TEAM@ADAKELLER.EXAMPLE", "team@adakeller.example"),
])
def test_suppression_matches_regardless_of_case_or_whitespace(stored, attempted):
    suppression = snd.Suppression.of([stored])
    with pytest.raises(snd.Refused):
        snd.approve(msg(to=attempted), IDENTITY, suppression=suppression)


def test_suppression_does_not_fold_plus_addressing():
    """a+b@x is not a@x. Folding them would suppress an address the recipient
    never opted out with, which silently drops real people."""
    suppression = snd.Suppression.of(["team+news@adakeller.example"])
    assert "team@adakeller.example" not in suppression


def test_an_unsuppressed_address_goes_through():
    suppression = snd.Suppression.of(["someone@else.example"])
    assert snd.approve(msg(), IDENTITY, suppression=suppression)


def test_suppression_can_grow():
    suppression = snd.Suppression()
    assert len(suppression) == 0
    suppression.add("  Team@AdaKeller.example ")
    assert "team@adakeller.example" in suppression and len(suppression) == 1


# --------------------------------------------------------------------------
# approve(): refuses rather than returning a flag
# --------------------------------------------------------------------------

def test_approve_refuses_on_an_incomplete_identity():
    broken = snd.SendingIdentity(from_address="x", from_name="",
                                 postal_address="", unsubscribe="")
    with pytest.raises(snd.NotConfigured):
        snd.approve(msg(), broken)


def test_approve_returns_the_message_that_will_actually_be_sent():
    ready = snd.approve(msg(), IDENTITY)
    assert snd.validate(ready, IDENTITY) == []


def test_approve_raises_rather_than_returning_false():
    """A caller can ignore a False. An exception has to be worked at."""
    import inspect
    source = inspect.getsource(snd.approve)
    assert "return False" not in source and "-> bool" not in source


# --------------------------------------------------------------------------
# DNS
# --------------------------------------------------------------------------

def test_the_records_cover_spf_and_dmarc_for_the_domain():
    records = snd.dns_records("outreach.surpluslayer.com")
    purposes = {r["purpose"] for r in records}
    assert {"SPF", "DMARC", "DKIM"} <= purposes
    dmarc = next(r for r in records if r["purpose"] == "DMARC")
    assert dmarc["host"] == "_dmarc.outreach.surpluslayer.com"


def test_dmarc_starts_at_p_none_so_a_mistake_reports_instead_of_dropping():
    dmarc = next(r for r in snd.dns_records("x.example") if r["purpose"] == "DMARC")
    assert "p=none" in dmarc["value"]
    assert "quarantine" in dmarc["note"]


def test_spf_hard_fails():
    spf = next(r for r in snd.dns_records("x.example") if r["purpose"] == "SPF")
    assert "-all" in spf["value"] and "~all" not in spf["value"]


def test_dkim_is_left_to_the_provider_rather_than_invented():
    """A guessed selector and key would fail alignment and look configured."""
    dkim = next(r for r in snd.dns_records("x.example") if r["purpose"] == "DKIM")
    assert "provider" in dkim["value"].lower()


def test_a_report_address_is_included_when_given():
    dmarc = next(r for r in snd.dns_records("x.example", dmarc_report_to="dmarc@x.example")
                 if r["purpose"] == "DMARC")
    assert "rua=mailto:dmarc@x.example" in dmarc["value"]


@pytest.mark.parametrize("bad", ["", "   ", "not-a-domain", "@"])
def test_a_non_domain_is_refused(bad):
    with pytest.raises(snd.NotConfigured):
        snd.dns_records(bad)


def test_a_leading_at_is_tolerated():
    assert snd.dns_records("@x.example")[0]["host"] == "x.example"


# --------------------------------------------------------------------------
# The opt-out has to be one that works
# --------------------------------------------------------------------------

def _identity(**kw) -> snd.SendingIdentity:
    base = dict(from_address="jia@campaigns.example.com", from_name="Jia",
                postal_address="410 Bryant St, San Francisco, CA 94107",
                unsubscribe="https://www.example.com/unsubscribe")
    base.update(kw)
    return snd.SendingIdentity(**base)


def test_the_footer_does_not_offer_reply_stop_by_default():
    """A fresh sending subdomain has no MX, so a STOP reply bounces to the
    recipient and reaches nobody here. Promising it anyway makes the opt-out
    fiction, and the only party who finds out is the person who used it."""
    ready = snd.finalize(snd.Message(to="a@b.example", subject="s", body="hi"),
                         _identity())
    assert "reply STOP" not in ready.body
    assert "https://www.example.com/unsubscribe" in ready.body


def test_the_footer_offers_reply_stop_once_inbound_is_declared():
    ready = snd.finalize(snd.Message(to="a@b.example", subject="s", body="hi"),
                         _identity(accepts_replies=True))
    assert "reply STOP" in ready.body


def test_both_footers_still_carry_the_address_and_the_unsubscribe():
    """The variant that drops the STOP clause must not drop a legal element
    with it -- that would trade one compliance failure for another."""
    for replies in (False, True):
        identity = _identity(accepts_replies=replies)
        ready = snd.finalize(snd.Message(to="a@b.example", subject="s", body="hi"),
                             identity)
        assert not snd.validate(ready, identity)


def test_accepts_replies_is_off_unless_the_environment_says_otherwise():
    env = {"SURPLUS_CAMPAIGNS_FROM_ADDRESS": "jia@campaigns.example.com",
           "SURPLUS_CAMPAIGNS_FROM_NAME": "Jia",
           "SURPLUS_CAMPAIGNS_POSTAL_ADDRESS": "410 Bryant St, SF, CA 94107",
           "SURPLUS_CAMPAIGNS_UNSUBSCRIBE": "https://www.example.com/u"}
    assert snd.load_identity(env).accepts_replies is False
    assert snd.load_identity({**env, "SURPLUS_CAMPAIGNS_ACCEPTS_REPLIES": "true"}
                             ).accepts_replies is True
    assert snd.load_identity({**env, "SURPLUS_CAMPAIGNS_ACCEPTS_REPLIES": "maybe"}
                             ).accepts_replies is False


# --------------------------------------------------------------------------
# DNS: the records that break things if they are wrong
# --------------------------------------------------------------------------

def test_an_mx_requirement_is_stated_rather_than_left_to_be_discovered():
    mx = next(r for r in snd.dns_records("campaigns.example.com")
              if r["type"] == "MX")
    assert "reply STOP" in mx["note"]


def test_a_known_provider_fills_in_its_own_spf_include():
    spf = next(r for r in snd.dns_records("c.example.com", provider="ses")
               if r["purpose"] == "SPF")
    assert spf["value"] == "v=spf1 include:amazonses.com -all"


def test_an_unknown_provider_is_refused_rather_than_passed_through():
    """An SPF record naming a host that does not authorise you fails exactly
    like no SPF at all, while looking configured."""
    with pytest.raises(snd.NotConfigured, match="not a provider"):
        snd.dns_records("c.example.com", provider="sendblaster")


def test_no_provider_leaves_the_include_visibly_unfilled():
    spf = next(r for r in snd.dns_records("c.example.com")
               if r["purpose"] == "SPF")
    assert "<your-provider-spf-include>" in spf["value"]


def test_the_spf_note_warns_about_replacing_an_existing_record():
    """Pasting this onto an apex that already sends mail replaces its SPF and
    breaks every existing mailbox on the domain."""
    spf = next(r for r in snd.dns_records("example.com")
               if r["purpose"] == "SPF")
    assert "merge" in spf["note"].lower()
