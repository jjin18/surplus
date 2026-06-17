"""
Live, offline-data harness for the propose-only relationship agent.

Loads pasted prod rows into a throwaway SQLite, then runs the REAL agent
(real Anthropic client driving the loop) over that spine. Prod is never
touched; the agent is propose-only so nothing is sent or written anywhere.

Usage:
    python3 scripts/run_relationship_agent_live.py --deep-dives 1
    python3 scripts/run_relationship_agent_live.py --deep-dives 5 --instruction "who should I follow up with?"

Data goes in CONTACTS / OUTREACH below (filled from the two SQL exports).
Run with --smoke to use a tiny built-in fixture instead (proves the wiring).
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.db import Base
from backend.agents import relationships as rel
from backend.agents import relationship_agent as ragent


# ── .env loader (so _api_key() sees ANTHROPIC_API_KEY) ─────────────────────
def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        # Override blanks too: the shell may export an EMPTY value (e.g. an
        # unset ANTHROPIC_API_KEY=""), which setdefault would wrongly keep.
        if not (os.environ.get(k) or "").strip():
            os.environ[k] = v


def _now():
    return datetime.now(timezone.utc)


# ── pasted prod data ──────────────────────────────────────────────────────
# Paste the Railway row-detail dump (the "Row N" vertical blocks) verbatim into
# RAW below. The combined query joins contact+prospect+event+outreach, so each
# block is one (contact, prospect, outreach) tuple; multiple blocks per person
# (multiple events / multiple messages) are grouped automatically.
#
# The host's captured voice (what /admin/voice-examples + the LinkedIn sync
# populate on User.voice_examples). Paste a few of the host's REAL sent messages
# here to see follow-ups written in their voice. Empty = generic-but-warm.
VOICE_EXAMPLES: list[str] = []

# Leave RAW empty to fall back to --smoke.
RAW = r"""
contact_id
1
contact_name
mianya
prospect_id
223
event_id
52
note
I have five siblings
next_step
NULL
contact_type
NULL
private_note
NULL
role
Unknown
company
Unknown
headline
NULL
bio
NULL
recent_activity
NULL
event_name
event_label
Tech week opening after party
event_brief
outreach_id
2
state
invite_sent
sent_at
"2026-06-02T09:11:30.680Z"
sent_text
Hey Mianya, great meeting you at Tech Week! Loved hearing about your siblings – that's a big crew. Would be fun to grab coffee and keep the conversation going.

contact_id
2
contact_name
vinita-sinha
prospect_id
224
event_id
52
note
NULL
next_step
grab a coffee
contact_type
follow_up
private_note
NULL
role
Unknown
company
Unknown
headline
NULL
bio
NULL
recent_activity
NULL
event_name
event_label
Tech week opening after party
event_brief
outreach_id
3
state
invite_sent
sent_at
"2026-06-02T09:32:35.224Z"
sent_text
Great meeting you at the Tech week opening party. Let's grab coffee and keep the conversation going.

contact_id
3
contact_name
sadri-dridi-178809200
prospect_id
225
event_id
52
note
Both at Founders Inc
next_step
NULL
contact_type
NULL
private_note
NULL
role
Unknown
company
Unknown
headline
NULL
bio
NULL
recent_activity
NULL
event_name
event_label
Tech week opening after party
event_brief
outreach_id
6
state
message_sent
sent_at
"2026-06-02T12:18:06.745Z"
sent_text
Hey! Really enjoyed our conversation at the opening party. Would be great to grab a quick call sometime and continue the chat—let me know if you're open to it.

contact_id
4
contact_name
pam-kavalam
prospect_id
226
event_id
53
note
NULL
next_step
NULL
contact_type
NULL
private_note
NULL
role
Unknown
company
Unknown
headline
NULL
bio
NULL
recent_activity
NULL
event_name
event_label
Big snow
event_brief
outreach_id
9
state
invite_sent
sent_at
"2026-06-02T20:29:49.618Z"
sent_text
Great meeting you at Big Snow! Let's stay in touch.

contact_id
5
contact_name
alexwelcing
prospect_id
227
event_id
53
note
Alex did the redbull soapbox race and showed me where the drinks were
next_step
NULL
contact_type
NULL
private_note
NULL
role
Unknown
company
Unknown
headline
NULL
bio
NULL
recent_activity
NULL
event_name
event_label
Big snow
event_brief
outreach_id
10
state
invite_sent
sent_at
"2026-06-02T21:57:50.019Z"
sent_text
Great meeting you at Big Snow! Thanks for the redbull soapbox intel and the drink tour—made the day way better. Let's stay in touch.

contact_id
8
contact_name
brianpan
prospect_id
230
event_id
53
note
pharma events
next_step
hop on a quick call
contact_type
follow_up
private_note
NULL
role
Unknown
company
Unknown
headline
NULL
bio
NULL
recent_activity
NULL
event_name
event_label
Big snow
event_brief
outreach_id
17
state
invite_sent
sent_at
"2026-06-02T23:22:37.438Z"
sent_text
Hey Brian, great chatting about pharma events at Big Snow. Would love to pick that conversation back up—open to a quick call?
"""

# Known column keys in the row dump. Anything between a key line and the next
# key line is that field's value (so multi-line sent_text is captured whole).
_FIELDS = {
    "contact_id", "contact_name", "contact_company",
    "prospect_id", "event_id", "note", "next_step", "contact_type",
    "private_note", "role", "company", "headline", "bio", "recent_activity",
    "captured_at", "source", "status", "connection_status",
    "event_name", "event_label", "event_brief",
    "outreach_id", "state", "channel", "sent_at", "sent_text",
}


def _coerce(v: str):
    s = (v or "").strip()
    if s == "" or s.upper() == "NULL":
        return None
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s


def _parse_dt(s):
    s = _coerce(s)
    if s is None:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_rows(raw: str):
    """Turn the vertical 'Row N / key\\nvalue' dump into (contacts, outreach).

    Robust to multi-line values: we only break on a line that is exactly a known
    field key, so a message body spanning several lines stays intact."""
    lines = raw.splitlines()
    blocks: list[dict] = []
    cur: dict | None = None

    def _is_row_header(s: str) -> bool:
        return s.lower().startswith("row ") and s[4:].strip().isdigit()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if _is_row_header(line):           # optional "Row N" separators: skip
            i += 1
            continue
        if line in _FIELDS:
            # Each DB record starts with a `contact_id` key — use it as the
            # record boundary so the dump parses whether or not "Row N" headers
            # are present between records.
            if line == "contact_id":
                cur = {}
                blocks.append(cur)
            if cur is None:
                i += 1
                continue
            key = line
            vals = []
            i += 1
            while i < len(lines) and lines[i].strip() not in _FIELDS \
                    and not _is_row_header(lines[i].strip()):
                vals.append(lines[i])
                i += 1
            cur[key] = "\n".join(vals).strip()
            continue
        i += 1

    contacts: list[dict] = []
    outreach: list[dict] = []
    seen_prospect: set = set()
    for b in blocks:
        cid = _coerce(b.get("contact_id"))
        pid = _coerce(b.get("prospect_id"))
        if cid is None or pid is None:
            continue
        cid, pid = int(cid), int(pid)
        if pid not in seen_prospect:
            seen_prospect.add(pid)
            contacts.append({
                "contact_id": cid,
                "contact_name": _coerce(b.get("contact_name")),
                "contact_company": _coerce(b.get("contact_company")),
                "prospect_id": pid,
                "event_label": _coerce(b.get("event_label")) or _coerce(b.get("event_name")),
                "prospect_name": _coerce(b.get("contact_name")),
                "role": _coerce(b.get("role")),
                "prospect_company": _coerce(b.get("company")),
                "note": _coerce(b.get("note")),
                "contact_type": _coerce(b.get("contact_type")),
                "next_step": _coerce(b.get("next_step")),
                "captured_at": _parse_dt(b.get("captured_at")),
                "source": _coerce(b.get("source")),
                "status": _coerce(b.get("status")),
                "connection_status": _coerce(b.get("connection_status")),
            })
        state = _coerce(b.get("state"))
        if state is not None:
            outreach.append({
                "prospect_id": pid,
                "state": state,
                "channel": _coerce(b.get("channel")) or "linkedin",
                "ts": _parse_dt(b.get("sent_at")),
                "body": _coerce(b.get("sent_text")) or "",
            })
    return contacts, outreach


# A tiny self-contained fixture so we can verify the live loop end-to-end
# before real data arrives. Three people, each hitting a different WHO branch.
def _smoke_data():
    contacts = [
        # Alice: tagged follow_up at capture, host DM'd, no reply -> SURFACE.
        {"contact_id": 1, "contact_name": "Alice Chen", "contact_company": "Vectorize",
         "prospect_id": 11, "event_label": "Seed Dinner", "event_city": "SF",
         "prospect_linkedin": "https://linkedin.com/in/alicechen",
         "prospect_name": "Alice Chen", "role": "Founder", "prospect_company": "Vectorize",
         "note": "Building a vector-DB startup; we talked about RAG eval.",
         "contact_type": "follow_up", "next_step": None,
         "captured_at": _now() - timedelta(days=6), "source": "scan"},
        # Bob: tagged sales, replied, host ALREADY sent an unanswered follow-up
        # yesterday -> SUPPRESS (already acted on).
        {"contact_id": 2, "contact_name": "Bob Ruiz", "contact_company": "Northwind",
         "prospect_id": 21, "event_label": "Founders Mixer", "event_city": "SF",
         "prospect_linkedin": "https://linkedin.com/in/bobruiz",
         "prospect_name": "Bob Ruiz", "role": "VP Eng", "prospect_company": "Northwind",
         "note": "Interested in our infra; asked for pricing.",
         "contact_type": "sales", "next_step": None,
         "captured_at": _now() - timedelta(days=12), "source": "scan"},
        # Carol: no tag, but her last inbound msg asks an open question the host
        # never answered -> SURFACE (conversation context).
        {"contact_id": 3, "contact_name": "Carol Nguyen", "contact_company": "Lumen",
         "prospect_id": 31, "event_label": "Design Salon", "event_city": "NYC",
         "prospect_linkedin": "https://linkedin.com/in/carolnguyen",
         "prospect_name": "Carol Nguyen", "role": "Head of Design", "prospect_company": "Lumen",
         "note": "Wants an intro to a design lead.",
         "contact_type": None, "next_step": None,
         "captured_at": _now() - timedelta(days=10), "source": "scan"},
    ]
    outreach = [
        # Alice: one outbound invite, no reply.
        {"prospect_id": 11, "state": "invite_sent", "channel": "linkedin",
         "ts": _now() - timedelta(days=5),
         "body": "Hey Alice — loved the RAG-eval chat at the Seed Dinner. Keen to keep comparing notes."},
        # Bob: he replied, then host followed up again (unanswered).
        {"prospect_id": 21, "state": "invite_sent", "channel": "linkedin",
         "ts": _now() - timedelta(days=11),
         "body": "Great meeting you at the mixer, Bob — here's that pricing one-pager."},
        {"prospect_id": 21, "state": "message_replied", "channel": "linkedin",
         "ts": _now() - timedelta(days=9),
         "body": "Thanks! Reviewing with my team, will circle back."},
        {"prospect_id": 21, "state": "message_sent", "channel": "linkedin",
         "ts": _now() - timedelta(days=1),
         "body": "Just bumping this — any thoughts from the team?"},
        # Carol: host DM'd, she replied with an open question (unanswered).
        {"prospect_id": 31, "state": "invite_sent", "channel": "linkedin",
         "ts": _now() - timedelta(days=8),
         "body": "Carol — great chatting at the Design Salon."},
        {"prospect_id": 31, "state": "message_replied", "channel": "linkedin",
         "ts": _now() - timedelta(days=7),
         "body": "You too! Could you intro me to your design lead? Would love to chat with them."},
    ]
    return contacts, outreach


# ── build the spine in SQLite ──────────────────────────────────────────────
def _build(db, contacts: list[dict], outreach: list[dict]):
    import json as _json
    u = models.User(name="Host", email="host@x.com", unipile_account_id="acct1",
                    voice_examples=_json.dumps(VOICE_EXAMPLES) if VOICE_EXAMPLES else "")
    db.add(u); db.commit()

    events: dict[str, models.Event] = {}
    prospects: dict[int, models.Prospect] = {}
    contact_rows: dict[int, models.Contact] = {}

    # captured_at fallback: prod query may omit it. Anchor each prospect's
    # capture to its earliest outreach ts (capture precedes the first message);
    # contacts with no outreach AND no captured_at stay None (recency unknown,
    # never auto-stale) rather than getting an invented date.
    earliest_ts: dict[int, datetime] = {}
    for o in outreach:
        ts = o.get("ts")
        pid = o["prospect_id"]
        if ts is not None and (pid not in earliest_ts or ts < earliest_ts[pid]):
            earliest_ts[pid] = ts

    for row in contacts:
        label = row.get("event_label") or "Event"
        ev = events.get(label)
        if ev is None:
            ev = models.Event(user_id=u.id, kind="in_person", label=label,
                              city=row.get("event_city") or "")
            db.add(ev); db.commit()
            events[label] = ev

        pid = row["prospect_id"]
        captured = row.get("captured_at")
        if captured is None and pid in earliest_ts:
            captured = earliest_ts[pid] - timedelta(days=1)

        # Build the durable Contact DIRECTLY (one per contact_id) — prod already
        # has it, and we don't get a linkedin_url/email to satisfy link_contact's
        # identity gate. The agent scopes by user_id, so a synthetic identity key
        # is fine; what matters is the spine shape it reads.
        c = contact_rows.get(row["contact_id"])
        if c is None:
            c = models.Contact(
                id=row["contact_id"],   # preserve prod contact_id in the output
                user_id=u.id,
                primary_identity_key=f"seed:{row['contact_id']}",
                name=row.get("contact_name"),
                company=row.get("contact_company"),
            )
            db.add(c); db.commit()
            contact_rows[row["contact_id"]] = c

        p = models.Prospect(
            event_id=ev.id, identity=str(pid), contact_id=c.id,
            name=row.get("prospect_name") or row.get("contact_name"),
            role=row.get("role") or "", company=row.get("prospect_company") or "",
            note=row.get("note"), contact_type=row.get("contact_type"),
            next_step=row.get("next_step"),
            status=row.get("status") or "pending", source=row.get("source") or "scan",
            captured_at=captured,
            connection_status=row.get("connection_status") or "unknown",
        )
        db.add(p); db.commit()
        prospects[pid] = p

    for o in outreach:
        p = prospects.get(o["prospect_id"])
        if p is None:
            continue
        db.add(models.OutreachLog(
            prospect_id=p.id, channel=o.get("channel") or "linkedin",
            state=o["state"], body=o.get("body") or "",
            ts=o.get("ts") or _now()))
    db.commit()
    return u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep-dives", type=int, default=1)
    ap.add_argument("--instruction", default="")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--mode", choices=["loop", "concurrent", "ab"], default="ab",
                    help="which variant to run: the old sequential loop, the new "
                         "concurrent path, or both back-to-back with a speedup ratio")
    args = ap.parse_args()

    _load_env()
    os.environ.setdefault("UNIPILE_DRY_RUN", "true")
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        raise SystemExit("ANTHROPIC_API_KEY not set (checked env + .env)")

    if args.smoke or not RAW.strip():
        contacts, outreach = _smoke_data()
        src = "smoke fixture"
    else:
        contacts, outreach = parse_rows(RAW)
        src = "pasted RAW dump"
    print(f"[data: {src} — {len(contacts)} people, {len(outreach)} outreach rows]")

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    u = _build(db, contacts, outreach)

    # cap how many people the agent deep-dives + proposes for this run
    ragent.MAX_DEEP_DIVES = args.deep_dives

    print(f"=== running agent: {len(contacts)} contact-rows, "
          f"deep_dives={args.deep_dives}, mode={args.mode}, "
          f"instruction={args.instruction!r} ===\n")

    def _timed(label, runner):
        """Run an agent variant, timing time-to-first-card (when the first
        proposal streams via on_proposal) and time-to-all-cards (wall clock)."""
        import time
        first = {"t": None}
        t0 = time.perf_counter()

        def _on(_p):
            if first["t"] is None:
                first["t"] = time.perf_counter() - t0

        res = runner(_on)
        total = time.perf_counter() - t0
        ttfc = first["t"]
        print(f"--- {label} ---")
        print(f"  time-to-first-card : "
              + (f"{ttfc:5.1f}s" if ttfc is not None else "  (no cards)"))
        print(f"  time-to-all-cards  : {total:5.1f}s   "
              f"({len(res.proposals)} proposals, stop={res.stop_reason}, "
              f"err={res.error})\n")
        return res, total, ttfc

    def _loop(on):
        return ragent.run_relationship_agent(
            db, u.id, instruction=args.instruction,
            max_steps=2 + 3 * args.deep_dives, on_proposal=on)

    def _conc(on):
        return ragent.run_relationship_agent_concurrent(
            db, u.id, instruction=args.instruction, on_proposal=on)

    last = None
    if args.mode in ("loop", "ab"):
        loop_res, loop_t, _ = _timed("LOOP (sequential)", _loop)
        last = loop_res
    if args.mode in ("concurrent", "ab"):
        conc_res, conc_t, _ = _timed("CONCURRENT (triage + fan-out)", _conc)
        last = conc_res
    if args.mode == "ab" and loop_t and conc_t:
        print(f"==> speedup: {loop_t / conc_t:.1f}x "
              f"({loop_t:.1f}s -> {conc_t:.1f}s)\n")

    res = last
    print("--- SUMMARY ---")
    print(res.summary, "\n")
    print(f"--- PROPOSALS ({len(res.proposals)}) ---")
    for i, p in enumerate(res.proposals, 1):
        print(f"\n[{i}] {p.kind}  ->  {p.contact_name} (contact_id={p.contact_id})")
        print(f"    {p.text}")
        if p.rationale:
            print(f"    rationale: {p.rationale}")


if __name__ == "__main__":
    main()
