"""campaigns_send.py : the gate every outbound campaign email passes, and the
things that must be true before one exists.

WHAT THIS IS NOT. It does not send. It holds no transport, no SMTP client, no
API key for a provider, and it deliberately does not import the relationship
side's sender -- that path goes through Unipile and belongs to the CRM. This is
the layer that decides whether a message is ALLOWED to be sent, and refuses
otherwise.

Three separate things can make an outbound campaign email a mistake, and they
fail in three different ways:

  UNLAWFUL      no physical postal address, no working opt-out, or sent to
                somebody who already opted out. Penalties under CAN-SPAM are
                assessed PER MESSAGE, so a 500-address run gets this wrong five
                hundred times rather than once.

  SELF-HARMING  more volume than a young sending domain can carry. Nothing
                rejects the message; the mailbox providers quietly start
                filing everything from that domain as spam, and the damage is
                to every future send rather than this one.

  UNATTRIBUTED  a from-address nobody can reply to, on a domain with no
                alignment, which is the same thing as the previous one on a
                slower fuse.

None of the three raises anything on its own. So each is checked here, and
`approve()` refuses rather than returning a flag a caller can ignore -- the
same posture the rest of this package takes toward a dataset it has not been
given, because a compliance requirement that lives in a checklist is one
somebody eventually forgets under time pressure.

THE FOOTER IS APPENDED BY CODE, NOT WRITTEN IN A TEMPLATE. A template can be
edited to remove it and nothing notices; a function that appends it cannot be
bypassed without deleting the call. `validate()` then checks the RENDERED text,
so a hand-written message is held to the same standard as a generated one.

NOT LEGAL ADVICE. The requirements encoded here are the uncontroversial core of
CAN-SPAM -- accurate headers, a physical address, a working opt-out honoured
promptly. It is a floor, not a compliance program, and the same caveat
solicitation.py carries applies: get the reading confirmed by counsel before
this matters. The warmup numbers below are convention rather than law.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# A young domain that sends like an old one gets filtered, and the filtering is
# not announced. This ramp is the widely-used shape: a couple of dozen a day to
# start, roughly doubling every few days, four weeks to full volume. The exact
# numbers are convention -- providers publish none -- so they are a table you
# can edit rather than a formula pretending to be derived.
WARMUP_RAMP: tuple[tuple[int, int], ...] = (
    (1, 20), (3, 40), (5, 75), (7, 120), (10, 200),
    (14, 350), (18, 500), (22, 750), (26, 1000), (30, 1500),
)

# Placed at the end of every message, by code. See the module docstring.
FOOTER_TEMPLATE = ("\n\n--\n{from_name}\n{postal_address}\n\n"
                   "You received this because you are a candidate or campaign "
                   "in the 2026 election. To stop receiving these, reply STOP "
                   "or use {unsubscribe}.")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotConfigured(RuntimeError):
    """The sending identity is incomplete, so nothing may be sent yet."""


class Refused(RuntimeError):
    """This particular message may not be sent, and why."""


@dataclass(frozen=True)
class SendingIdentity:
    """Who the mail is from, and the two things law requires it carry.

    `postal_address` is a real street address. There is no default and no
    placeholder: a fake one is worse than a missing one, because a missing one
    stops the send and a fake one does not.
    """
    from_address: str
    from_name: str
    postal_address: str
    unsubscribe: str                 # a URL, or "mailto:..."
    reply_to: str = ""

    def problems(self) -> list[str]:
        """Everything wrong with this identity, so one call names them all
        rather than making a caller fix them one refusal at a time."""
        found: list[str] = []
        if not _EMAIL.match(self.from_address or ""):
            found.append("from_address is not an email address")
        if not (self.from_name or "").strip():
            found.append("from_name is empty")
        address = (self.postal_address or "").strip()
        if len(address) < 12 or not any(ch.isdigit() for ch in address):
            # A street address has a number in it. This catches "TBD" and
            # "Our Office" without pretending to validate an address.
            found.append("postal_address does not look like a street address")
        unsubscribe = (self.unsubscribe or "").strip()
        if not (unsubscribe.startswith("http") or unsubscribe.startswith("mailto:")):
            found.append("unsubscribe is not a URL or mailto:")
        return found

    @property
    def domain(self) -> str:
        return (self.from_address or "").rsplit("@", 1)[-1].lower()


def load_identity(env: Optional[dict] = None) -> SendingIdentity:
    """The sending identity from the environment, or a refusal naming what is
    missing. Never returns a partially-filled identity."""
    env = os.environ if env is None else env
    identity = SendingIdentity(
        from_address=(env.get("SURPLUS_CAMPAIGNS_FROM_ADDRESS") or "").strip(),
        from_name=(env.get("SURPLUS_CAMPAIGNS_FROM_NAME") or "").strip(),
        postal_address=(env.get("SURPLUS_CAMPAIGNS_POSTAL_ADDRESS") or "").strip(),
        unsubscribe=(env.get("SURPLUS_CAMPAIGNS_UNSUBSCRIBE") or "").strip(),
        reply_to=(env.get("SURPLUS_CAMPAIGNS_REPLY_TO") or "").strip(),
    )
    problems = identity.problems()
    if problems:
        raise NotConfigured(
            "campaign sending identity is incomplete, so nothing may be sent: "
            + "; ".join(problems)
            + ". Set SURPLUS_CAMPAIGNS_FROM_ADDRESS, _FROM_NAME, "
              "_POSTAL_ADDRESS and _UNSUBSCRIBE.")
    return identity


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------

def warmup_allowance(day: int) -> int:
    """How many messages this domain may send on day `day` of warmup.

    Day 1 is the first day of sending. Below the first step the first step's
    cap applies; past the last, the last. Interpolation between steps is
    deliberately absent -- a step function is what you can reason about when
    deciding whether today's run is safe.
    """
    if day < 1:
        return 0
    allowed = WARMUP_RAMP[0][1]
    for threshold, cap in WARMUP_RAMP:
        if day >= threshold:
            allowed = cap
    return allowed


def warmup_schedule() -> list[dict]:
    """The ramp, for printing. Cumulative totals included because the useful
    question is usually "when can I have contacted 500 campaigns", not "what
    is today's cap"."""
    rows: list[dict] = []
    total = 0
    for day in range(1, WARMUP_RAMP[-1][0] + 1):
        cap = warmup_allowance(day)
        total += cap
        rows.append({"day": day, "cap": cap, "cumulative": total})
    return rows


# ---------------------------------------------------------------------------
# Suppression : the opt-out, made structural
# ---------------------------------------------------------------------------

def normalize_address(address: str) -> str:
    """Lowercased and trimmed. Deliberately NOT clever: gmail dot-and-plus
    folding is not applied, because treating a+b@x as a@x would suppress an
    address the recipient never opted out with, and over-suppressing is the
    safe direction only until it silently drops real people."""
    return (address or "").strip().lower()


@dataclass
class Suppression:
    """Addresses that must never be contacted again.

    CAN-SPAM requires honouring an opt-out promptly. The way to be sure of that
    is to make the send path incapable of reaching a suppressed address, rather
    than to remember to filter the list.
    """
    addresses: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, addresses: Iterable[str]) -> "Suppression":
        return cls({normalize_address(a) for a in addresses if normalize_address(a)})

    def add(self, address: str) -> None:
        cleaned = normalize_address(address)
        if cleaned:
            self.addresses.add(cleaned)

    def __contains__(self, address: object) -> bool:
        return normalize_address(str(address)) in self.addresses

    def __len__(self) -> int:
        return len(self.addresses)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str

    @property
    def to_domain(self) -> str:
        return (self.to or "").rsplit("@", 1)[-1].lower()


def finalize(message: Message, identity: SendingIdentity) -> Message:
    """The message as it will actually be sent, footer appended.

    Idempotent: finalising an already-finalised message does not append a
    second footer, because a caller that finalises twice should get a correct
    message rather than a doubled one.
    """
    if identity.postal_address.strip() in message.body:
        return message
    footer = FOOTER_TEMPLATE.format(
        from_name=identity.from_name,
        postal_address=identity.postal_address,
        unsubscribe=identity.unsubscribe)
    return Message(to=message.to, subject=message.subject,
                   body=message.body.rstrip() + footer)


def validate(message: Message, identity: SendingIdentity) -> list[str]:
    """Everything that would make sending this message a mistake.

    Checks the RENDERED body, so a hand-written message is held to the same
    standard as a generated one and a removed footer is caught rather than
    assumed absent.
    """
    problems: list[str] = []
    if not _EMAIL.match(message.to or ""):
        problems.append("recipient is not an email address")
    if not (message.subject or "").strip():
        problems.append("subject is empty")
    if not (message.body or "").strip():
        problems.append("body is empty")
    if identity.postal_address.strip() and identity.postal_address.strip() not in message.body:
        problems.append("body does not carry the physical postal address")
    unsubscribe = identity.unsubscribe.strip()
    if unsubscribe and unsubscribe not in message.body:
        problems.append("body does not carry the unsubscribe route")
    return problems


def approve(message: Message, identity: SendingIdentity, *,
            suppression: Optional[Suppression] = None,
            sent_today: int = 0, warmup_day: int = 1) -> Message:
    """The gate. Returns the message to send, or refuses and says why.

    Refuses rather than returning a boolean, because a caller that ignores a
    False is the failure this exists to prevent, and a caller that ignores an
    exception has to work at it.
    """
    problems = identity.problems()
    if problems:
        raise NotConfigured("sending identity is incomplete: " + "; ".join(problems))

    ready = finalize(message, identity)

    if suppression is not None and ready.to in suppression:
        raise Refused(
            f"{ready.to} has opted out. Honouring that is required, and the "
            f"send path is built so a suppressed address cannot be reached.")

    allowance = warmup_allowance(warmup_day)
    if sent_today >= allowance:
        raise Refused(
            f"day {warmup_day} of warmup allows {allowance} messages and "
            f"{sent_today} have gone already. Sending more will not bounce -- "
            f"it will quietly cost this domain its reputation, which is not "
            f"recoverable on the timescale of one election.")

    faults = validate(ready, identity)
    if faults:
        raise Refused("message would be unlawful to send: " + "; ".join(faults))
    return ready


def dns_records(domain: str, *, dmarc_report_to: str = "") -> list[dict]:
    """The DNS a sending domain needs, as records you can paste.

    DKIM is not here: its selector and public key are issued by whichever
    provider you send through, so a value invented here would be a wrong one.
    Everything else is computable from the domain alone.

    p=none on DMARC is deliberate for a new domain: it reports without
    rejecting, so a misconfiguration shows up in reports rather than silently
    dropping the first week of real mail. Tighten to quarantine, then reject,
    once the reports are clean.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or "." not in domain:
        raise NotConfigured(f"{domain!r} is not a domain")

    rua = f" rua=mailto:{dmarc_report_to};" if dmarc_report_to else ""
    return [
        {"host": domain, "type": "TXT", "purpose": "SPF",
         "value": "v=spf1 include:<your-provider-spf-include> -all",
         "note": "Replace the include with your provider's. -all, not ~all: "
                 "a soft fail teaches nothing."},
        {"host": f"_dmarc.{domain}", "type": "TXT", "purpose": "DMARC",
         "value": f"v=DMARC1; p=none;{rua} fo=1",
         "note": "p=none reports without rejecting. Move to quarantine, then "
                 "reject, once reports are clean."},
        {"host": f"<selector>._domainkey.{domain}", "type": "TXT",
         "purpose": "DKIM",
         "value": "<issued by your sending provider>",
         "note": "Selector and key come from the provider. Nothing here can "
                 "compute them, and a guessed value would fail alignment."},
    ]
