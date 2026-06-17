"""agents/drafting.py : the ONE follow-up-message composer, shared by every
surface (BookApp's /draft tap, the relationship chat, future surfaces).

Why this exists
---------------
We had two drafters: the rich one inside relationship_agent.py (voice-matched,
continues the real message thread, strips em dashes) and a stripped-down one in
book.py (name + a `next_step` string, no voice, em dashes leaking through). The
surface users actually see ("Your book today") ran the dumb one. This module is
the consolidation: a single composer that pulls the host's voice and the real
prior-message thread, so a follow-up reads like the same person continuing the
same conversation, on whichever surface drafts it.

It reuses the relationship agent's building blocks (voice examples, the
timeline->thread distiller, the dash scrub) so there is one source of truth for
"how a follow-up is written," and book.py's generic Claude-JSON caller so all
LLM calls share the same client + [book] tracing.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from typing import Optional

from . import relationships
from .book import _btrace, _llm_json, stream_text  # shared Claude helpers + trace
from .relationship_agent import (
    _host_voice_examples,
    _strip_dashes,
    _thread_from_timeline,
    _voice_block,
)

# Foreground per-person draft fan-out width for compose_batch. Bounded so a
# multi-person /ask can't open a flood of Anthropic connections at once (same
# pool-saturation lesson as the book background gate). Tunable live.
_DRAFT_CONCURRENCY = max(1, int(os.environ.get("DRAFT_CONCURRENCY", "6")))


_FOLLOWUP_SYSTEM = (
    "You write a short follow-up message for an event host reconnecting with "
    "someone they know. If prior messages are provided, CONTINUE that "
    "conversation: pick up where it left off and reference what was actually "
    "said, then add the reason to reach out now. If there are NO prior messages "
    "(the list is empty), write a warm, natural note built around the reason to "
    "reach out (e.g. congratulate them on the news) -- do NOT refuse, do NOT ask "
    "for more context, and do NOT mention the absence of prior messages; just "
    "write the message. "
    "Rules: 2-4 sentences, warm and specific, never salesy. NEVER use em dashes "
    "(—) or en dashes (–); use a comma, a period, or restructure. If a "
    "<style_examples> block is provided, write in that exact voice (greeting, "
    "sign-off, sentence length, punctuation, emoji habits), matching the voice "
    "not the content. If channel is email, also return a 3-5 word subject. "
    "Return ONLY JSON: {\"subject\":\"<email only, else null>\","
    "\"body\":\"<the message>\"}"
)


# ── two-phase split: DB read (serial, thread-unsafe) vs LLM call (concurrent) ──
#
# A multi-person /ask must draft many people, but a SQLAlchemy Session isn't
# thread-safe, so we can't touch the DB from the fan-out threads. Split the work:
#   build_context(db, ...)  -- all DB reads, on the request thread
#   compose_from_context()  -- pure LLM call, safe to run concurrently
# compose_followup() chains both for the single-draft (/draft tap) caller.


def _email_thread_prior(db, user_id: int, contact) -> list[dict]:
    """The contact's REAL email conversation (bodies), shaped like the timeline
    thread ({when, who, channel, text}) so an email follow-up continues what was
    actually written. Uses the linked thread (Contact.email_thread_id) if set,
    else finds the newest thread with the contact's address. Best-effort: any
    missing piece (no mailbox, no address, Unipile error) returns []."""
    import os
    from .. import models
    from . import email_sync
    try:
        user = db.get(models.User, user_id)
        account_id = getattr(user, "unipile_email_account_id", None)
        own = (getattr(user, "email_account_address", None) or "").strip().lower()
        addr = (getattr(contact, "email", None) or "").strip().lower()
        dsn = (os.environ.get("UNIPILE_DSN") or "").strip()
        api_key = (os.environ.get("UNIPILE_API_KEY") or "").strip()
        if not (account_id and dsn and api_key):
            return []
        thread_id = getattr(contact, "email_thread_id", None)
        if not thread_id and addr:
            threads = email_sync.list_threads_for_address(
                dsn=dsn, api_key=api_key, account_id=account_id,
                address=addr, own_address=own)
            thread_id = threads[0]["thread_id"] if threads else None
        if not thread_id:
            return []
        msgs = email_sync.thread_messages(
            dsn=dsn, api_key=api_key, account_id=account_id,
            thread_id=str(thread_id), own_address=own, with_bodies=True)
        prior = []
        for m in msgs:
            text = (m.get("body") or "").strip()
            if not text:
                continue
            prior.append({
                "when": m.get("date"),
                "who": "host" if m.get("direction") == "out" else "them",
                "channel": "email",
                "text": text[:600],
            })
        return prior
    except Exception:  # noqa: BLE001 : email grounding is best-effort
        return []


def build_context(db, user_id: int, contact, voice_block: Optional[str] = None,
                  *, channel: str = "email") -> dict:
    """Gather everything the composer needs for `contact` via the DB (the host's
    voice + this person's real prior-message thread). Runs on the request thread;
    `voice_block` can be passed in pre-rendered to avoid re-loading it per person
    in a batch (it's the same for every contact of one host).

    For the EMAIL channel we also pull the real email-thread bodies so the draft
    continues the actual email conversation, not just a 'N messages' rollup."""
    name = (getattr(contact, "name", None) or "there").strip() or "there"
    try:
        prior = _thread_from_timeline(relationships.contact_timeline(db, contact))
    except Exception:  # noqa: BLE001 : a timeline read failure must not break drafting
        prior = []
    if channel == "email":
        email_prior = _email_thread_prior(db, user_id, contact)
        if email_prior:
            # Merge cross-channel history, oldest-first; email bodies are the
            # substance for an email follow-up. Dedup is unnecessary (the
            # timeline only carries an email ROLLUP, not these message bodies).
            prior = sorted(prior + email_prior,
                           key=lambda m: str(m.get("when") or ""))
    if voice_block is None:
        voice_block = _voice_block(_host_voice_examples(db, user_id))
    return {"name": name, "prior": prior, "voice_block": voice_block}


def compose_from_context(ctx: dict, reason: str, channel: str = "email") -> Optional[dict]:
    """The pure-LLM half: compose from a context dict (no DB), so it's safe to
    fan out across threads. Returns {"subject", "body"} or None on failure."""
    system = _FOLLOWUP_SYSTEM + (ctx.get("voice_block") or "")
    user = (
        f"Follow up with {ctx.get('name') or 'there'}.\n"
        f"Prior conversation (oldest first; [] means no prior messages):\n"
        f"{json.dumps(ctx.get('prior') or [], default=str)}\n"
        f"Reason to reach out now: {reason}\n"
        f"Channel: {channel}\n"
    )
    out = _llm_json(system, user, max_tokens=500)
    if not out or not (out.get("body") or "").strip():
        return None
    body = _strip_dashes(out["body"])
    subject = out.get("subject")
    subject = _strip_dashes(subject) if (channel == "email" and subject) else None
    return {"subject": subject, "body": body}


_FOLLOWUP_STREAM_SYSTEM = (
    "You write a short follow-up message for an event host reconnecting with "
    "someone they met. CONTINUE the existing conversation in the prior messages "
    "below: pick up where it left off and reference what was actually said, then "
    "add the reason to reach out now. Never a generic cold restart. "
    "Rules: 2-4 sentences, warm and specific, never salesy. NEVER use em dashes "
    "(—) or en dashes (–); use a comma, a period, or restructure. If a "
    "<style_examples> block is provided, write in that exact voice (greeting, "
    "sign-off, sentence length, punctuation, emoji habits). "
    "Write ONLY the message body as plain text: no subject line, no JSON, no "
    "surrounding quotes, no preamble or labels. Just the message to send."
)


def stream_from_context(ctx: dict, reason: str, channel: str = "email"):
    """The pure-LLM half of streamed drafting: yield body tokens from a prebuilt
    context dict (no DB), so the agent can build all contexts serially then fan
    out token streams across threads. Mirrors compose_from_context, streamed."""
    system = _FOLLOWUP_STREAM_SYSTEM + (ctx.get("voice_block") or "")
    user = (
        f"Follow up with {ctx.get('name') or 'there'}.\n"
        f"Prior conversation (oldest first; [] means no prior messages):\n"
        f"{json.dumps(ctx.get('prior') or [], default=str)}\n"
        f"Reason to reach out now: {reason}\n"
        f"Channel: {channel}\n"
    )
    yield from stream_text(system, user, max_tokens=500)


def compose_stream(db, user_id: int, contact, *, reason: str,
                   channel: str = "email"):
    """Yield the follow-up body token-by-token (live 'typing'). Same voice + real
    prior-thread context as compose_followup, but streamed as plain text (no JSON
    wrapper, so deltas render directly). For the streamed /draft tap. Yields
    nothing when no key is set -- the caller falls back to compose_followup."""
    yield from stream_from_context(build_context(db, user_id, contact),
                                   reason, channel)


def compose_followup(db, user_id: int, contact, *, reason: str,
                     channel: str = "email") -> Optional[dict]:
    """One voice-matched, thread-aware follow-up to `contact` (a Contact ORM row).
    Returns {"subject", "body"} or None on failure (caller falls back). Loads the
    thread + voice, then composes -- the single-draft contract used by /draft."""
    return compose_from_context(
        build_context(db, user_id, contact, channel=channel), reason, channel)


def compose_batch(db, user_id: int, jobs: list[dict],
                  *, concurrency: int = _DRAFT_CONCURRENCY) -> list[Optional[dict]]:
    """Draft a follow-up for each job, returned in input order. Each job is
    {"contact": <Contact ORM>, "reason": str, "channel"?: str}. DB context is
    built SERIALLY here (session not thread-safe), then the LLM calls fan out
    under a bounded thread pool. Used by /ask to draft every selected person
    inline (voice + their real thread + dash scrub) without one-at-a-time waits."""
    if not jobs:
        return []
    # Voice is per-host, identical across contacts: load once, reuse.
    voice_block = _voice_block(_host_voice_examples(db, user_id))
    ctxs = [build_context(db, user_id, j["contact"], voice_block,
                          channel=(j.get("channel") or "email")) for j in jobs]
    results: list[Optional[dict]] = [None] * len(jobs)

    def _one(i: int) -> None:
        results[i] = compose_from_context(
            ctxs[i], jobs[i].get("reason") or "following up",
            jobs[i].get("channel") or "email")

    import time as _t
    t0 = _t.monotonic()
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, concurrency)) as ex:
        list(ex.map(_one, range(len(jobs))))
    _btrace(f"compose_batch {len(jobs)} drafts (concurrency={concurrency}) "
            f"in {_t.monotonic()-t0:.2f}s")
    return results
