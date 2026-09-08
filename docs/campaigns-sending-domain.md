# Campaign sending domain

Everything needed to put `campaigns.surpluslayer.com` into service as the
sending domain for 2026 candidate outreach, and the reasoning for each step
that is not obvious.

The gate that enforces all of this is `backend/campaigns_send.py`. This
document is the operator's half; it holds no rules of its own.

## Why a subdomain, and not surpluslayer.com

`surpluslayer.com` carries `event.` (the CRM), `www.` (the site), `join.` (the
landing page) and `support@`. Sending cold outreach from it is two separate
problems:

1. **A domain holds exactly one SPF record.** Publishing SPF at the apex
   *replaces* whatever is there. Every existing mailbox on the domain —
   `support@`, `jia@` — starts failing SPF the moment the record propagates.
   This is not a reputation argument; it is an outage.
2. **Reputation is scored on the organizational domain.** Cold outreach at ramp
   volume is exactly the traffic that gets a domain filtered, and the filtering
   is never announced. On the apex, that lands on your real business mail.

A subdomain has no existing records to replace, gets its own SPF, DKIM and
DMARC, and keeps the blast radius off the mailboxes people actually use. It
does not eliminate organizational-domain bleed — nothing does short of a
separate registrable domain — but it removes the outage risk entirely and
reduces the rest.

The **unsubscribe page is the opposite call**: put it on `www.surpluslayer.com`.
A real site with a real privacy policy behind the opt-out link is what keeps
recipients from reporting the mail as spam, and complaints are what actually
burn a sending domain. An unsubscribe link pointing at a bare subdomain nobody
can look up reads as phishing.

## Steps

### 1. Publish DNS on the sending subdomain

Generate the records rather than copying them from anywhere:

```
python -m scripts.campaigns_domain records campaigns.surpluslayer.com \
    --provider ses --dmarc-report-to dmarc@surpluslayer.com
```

Four records. SPF and DMARC are computed from the domain. **DKIM is issued by
your provider** — the selector and key come from their dashboard, and a guessed
value fails alignment while looking configured, so nothing here invents one.

`--provider` accepts `ses`, `sendgrid`, `mailgun`, `postmark`, `google`,
`microsoft`. An unrecognised name is refused rather than passed through: an SPF
record naming a host that does not authorise you fails exactly like no SPF at
all, and looks fine in a DNS console.

DMARC starts at `p=none`. It reports without rejecting, so a misconfiguration
shows up in the reports instead of silently dropping the first week of real
mail. Move to `quarantine`, then `reject`, once reports come back clean.

### 2. Decide about inbound mail — this one is a legal question

The MX record is listed as `INBOUND`, and it gates something specific.

The footer can offer two opt-out routes: the unsubscribe URL, and "reply STOP".
**A sending subdomain with no MX receives nothing.** A recipient who replies
STOP has the reply bounce back to them; it reaches nobody here; the opt-out
they were promised never happens. Nothing about that is visible from the
sending side — the run looks clean, and the only party who learns the opt-out
was fiction is the person who used it. CAN-SPAM requires the opt-out to work,
and penalties are assessed per message.

So the default is not to promise it. `finalize()` prints the STOP clause only
when `SURPLUS_CAMPAIGNS_ACCEPTS_REPLIES` is set, and you should set it only
after publishing an MX **and** sending a test reply and watching it arrive.

Leaving inbound unconfigured is a legitimate choice. The footer then routes
opt-outs to the unsubscribe URL alone, which satisfies the requirement on its
own.

### 3. Fill in the identity

The five variables are documented in `.env.example`. Four are mandatory and
the gate refuses to send while any is missing:

| Variable | Notes |
|---|---|
| `SURPLUS_CAMPAIGNS_FROM_ADDRESS` | on the sending subdomain |
| `SURPLUS_CAMPAIGNS_FROM_NAME` | |
| `SURPLUS_CAMPAIGNS_POSTAL_ADDRESS` | a real street address; no default, deliberately |
| `SURPLUS_CAMPAIGNS_UNSUBSCRIBE` | a page on `www.`, not the subdomain |
| `SURPLUS_CAMPAIGNS_ACCEPTS_REPLIES` | only after step 2 is genuinely done |

Then confirm:

```
python -m scripts.campaigns_domain check
```

It names everything missing at once rather than one refusal at a time, and it
warns if the from-address sits on a host known to carry real mail.

### 4. Warm up

```
python -m scripts.campaigns_domain calendar --start 2026-09-09
```

The ramp is in `WARMUP_RAMP`: 20/day at the start, roughly doubling every few
days, 1,500/day by day 30. The numbers are convention — providers publish none
— so they are a table you can edit rather than a formula pretending to be
derived. `approve()` refuses once the day's allowance is spent, because
exceeding it does not bounce; it quietly costs the domain its reputation, and
that is not recoverable on the timescale of one election.

**The ramp is not the binding constraint.** Cumulative capacity passes 3,300
messages by day 18 — more than every candidate for all 504 federal and
gubernatorial seats. What limits reach is contactable addresses, which is a
data problem in `campaigns_states.py`, not a deliverability one. Starting the
warmup a few weeks late still clears the list before November 3; starting with
53 of 504 seats configured does not.

## What this does not cover

Provider selection. Some providers prohibit cold outreach in their terms —
Postmark does explicitly — and an account banned mid-campaign is worse than a
slow one. Amazon SES permits it but requires a production-access request
describing your list and opt-out, which takes a day or two, so start it before
you need it.

Not legal advice. This is CAN-SPAM's uncontroversial core — accurate headers, a
physical address, a working opt-out honoured promptly — and it is a floor
rather than a compliance program. The same caveat `solicitation.py` carries
about Rule 7.3 applies here.
