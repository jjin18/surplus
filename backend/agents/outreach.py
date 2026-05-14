"""
agents/outreach.py — stage 03b, message composition + simulated funnel.

Two responsibilities, kept separate so the provider layer can reuse compose()
without dragging the simulator along:

  compose(prospect, event, peers=?, host_bio=?) -> Message
      Produces both the LinkedIn-format connection note (≤280 chars, hard
      LinkedIn cap is 300) and the longer post-accept first message.

      The signature is LLM-ready — adding an Anthropic-backed implementation
      later is a pure function-body change. `host_bio` is accepted now so
      call sites don't churn when the LLM upgrade lands.

  run_outreach(prospects, event, rng=?) -> [(prospect, events, status)]
      The original RNG-seeded simulator. Used in DRY_RUN mode for demo
      continuity (so /match and /roi still have RSVPs to work with). Picks
      `msg.note` as the "first touch" body for OutreachLog.

The provider layer (backend/providers/*) calls compose() directly and does
NOT call run_outreach(). The simulator is strictly the local-only fallback.
"""
from __future__ import annotations
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config


# LinkedIn allows up to 300 chars in a connection note. We aim for 280 to
# leave room for unicode / smart-quote expansion and to keep things tight.
NOTE_CHAR_LIMIT = 280
NOTE_HARD_CAP = 300


@dataclass(frozen=True)
class Message:
    """Both halves of an outreach: the connection note + the post-accept DM."""
    note: str       # ≤NOTE_CHAR_LIMIT chars, fits in a LinkedIn connection request
    message: str    # longer follow-up, sent after the connection is accepted


def _truncate_note(text: str, limit: int = NOTE_CHAR_LIMIT) -> str:
    """Clip a note to `limit` chars cleanly on a sentence/word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    # try to end on a sentence boundary first
    cut = text[:limit]
    for sep in (". ", "? ", "! "):
        i = cut.rfind(sep)
        if i >= limit - 80:  # only accept a cut near the end
            return cut[: i + 1].strip()
    # otherwise end on a word boundary
    i = cut.rfind(" ")
    if i > 0:
        return cut[:i].rstrip(",. ") + "…"
    return cut.rstrip() + "…"


def _peer_reveal(peers: list[str], n: int = 2) -> str:
    """' Theo and Nadia are already in.' (or empty string if no peers)."""
    if not peers:
        return ""
    firsts = [p.split()[0] for p in peers[:n]]
    if len(firsts) == 1:
        names = firsts[0]
    elif len(firsts) == 2:
        names = f"{firsts[0]} and {firsts[1]}"
    else:
        names = ", ".join(firsts[:-1]) + f", and {firsts[-1]}"
    return f" {names} are already in."


def compose(
    prospect,
    event,
    peers: list[str] | None = None,
    host_bio: str | None = None,
) -> Message:
    """
    Build the connection note + follow-up message for a prospect.

    Parameters
    ----------
    prospect : the Prospect ORM row (or any object with the same attrs)
    event    : the Event ORM row
    peers    : confirmed peer names (for the composition reveal)
    host_bio : optional host's blurb — used by the longer follow-up when
               available. Accepted now so the LLM upgrade is a no-churn swap.

    Returns
    -------
    Message(note, message)
    """
    peers = peers or []
    first = (prospect.name or "there").split()[0]
    domain = (prospect.works_on or "your space").replace("-", " ")
    framing = config.goal_cfg(event.goal)["outreach"].format(
        headcount=event.headcount,
        format=event.format.lower(),
        city=event.city,
        seniority=event.seniority.lower(),
        role=event.role.lower(),
        co_stage=event.co_stage,
    )
    reveal = _peer_reveal(peers)

    # --- connection note: short, specific, no hard pitch --------------------
    note_body = (
        f"Hi {first} — pulling together {framing}. "
        f"Your {domain} work caught my eye.{reveal} "
        f"Worth your time?"
    )
    note = _truncate_note(note_body)

    # --- post-accept first message: longer, can pitch a bit -----------------
    msg_lines = [
        f"Thanks for connecting, {first}.",
        "",
        f"Quick context: we're putting together {framing}.",
    ]
    if host_bio:
        msg_lines.append("")
        msg_lines.append(host_bio.strip())
    if prospect.offers:
        msg_lines.append("")
        msg_lines.append(
            f"Given your {domain} background ({prospect.offers}), "
            f"there's a clear fit on this side of the room."
        )
    if reveal:
        msg_lines.append("")
        msg_lines.append(f"Confirmed so far:{reveal.strip()}")
    msg_lines.append("")
    msg_lines.append("Worth a closer look? Happy to share details.")

    return Message(note=note, message="\n".join(msg_lines).strip())


def run_outreach(prospects, event, rng: random.Random | None = None):
    """
    Simulated outreach funnel (used in DRY_RUN mode for demo continuity).

    Returns: list of (prospect, outreach_events, status) where outreach_events
    is a list of {"state", "body", "ts"} dicts in send order.
    """
    rng = rng or random.Random(event.id or 0)
    # callers (pipeline.run_outreach_stage) gate by status; we accept whoever
    # they hand us. status='approved' is the post-split contract.
    confirmed = [p for p in prospects if p.status == "approved"]
    all_names = [p.name for p in confirmed]
    results = []

    for p in confirmed:
        peers = [n for n in all_names if n != p.name]
        msg = compose(p, event, peers=peers)
        now = datetime.now(timezone.utc)
        events = [{"state": "sent", "body": msg.note, "ts": now}]

        # higher fit -> higher open + reply rates
        if rng.random() < min(0.97, 0.55 + p.fit_score / 200):
            events.append({"state": "opened", "body": "", "ts": now + timedelta(hours=2)})
            if rng.random() < min(0.90, 0.30 + p.fit_score / 160):
                events.append(
                    {"state": "replied", "body": "RSVP confirmed", "ts": now + timedelta(hours=6)}
                )

        status = "rsvp" if any(e["state"] == "replied" for e in events) else "contacted"
        results.append((p, events, status))

    return results
