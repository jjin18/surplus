"""
routes/civic.py : Civic policy search.

    POST /api/civic/ask          question (+ place or dropped pin) -> Answer
    GET  /api/civic/answer/{id}  the cached answer behind a permalink
    GET  /civic                  the map + ladder page
    GET  /civic/r/{id}           the same page, opened on a shared answer

No DB, no auth, no accounts : an answer is a pure function of the question and
the place, cached for 24h by sha256(question|location). The Anthropic key
lives only here, server-side -- the browser never sees it, which is the whole
reason this route exists instead of the prototype's direct fetch to the API.

Civic is a bolt-on, not part of the product, so it is fenced off from the rest
of surplus rather than trusted to behave:

  * It shares the process with the CRM. A synthesis call blocks a threadpool
    thread for 15-25s, and the pool (~40 threads, WEB_CONCURRENCY=1 by
    default) is the same one every sync CRM route runs on. `_SLOTS` caps how
    many of those threads Civic may ever hold ; past the cap it sheds load
    with a 503 instead of queueing behind them.
  * It shares ANTHROPIC_API_KEY and EXA_API_KEY with drafting and prospecting,
    so a busy day here is spend and rate-limit pressure there. `_DAILY_CAP`
    bounds the number of answers it will synthesize per day.
  * CIVIC_ENABLED=0 turns the whole surface off -- page included -- without a
    deploy, if it ever does become someone else's problem.
  * It touches no database, no session, no user. The only thing it imports
    from the app is the per-IP rate limiter, and tests pin that.

The synthesis itself (prompt, evidence ladder, URL grounding) lives in
backend/civic.py; this module is the HTTP skin and the fence around it.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import civic
from ..rate_limit import per_ip_rate_limit

router = APIRouter(prefix="/api/civic", tags=["civic"])

# The page lives outside /api so it can be linked and shared as a plain URL.
pages_router = APIRouter(include_in_schema=False)

_UI_DIR = Path(__file__).resolve().parent.parent / "civic_ui"
_UI_HTML = _UI_DIR / "index.html"

# 10/min/IP: a query costs real money in search and tokens, so this is a cost
# gate as much as an abuse gate. Its own tag, so it doesn't share a window
# with signup or checkout.
_ASK_LIMIT = per_ip_rate_limit(limit=10, window_s=60, tag="civic_ask")


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int((os.environ.get(name) or "").strip() or default))
    except ValueError:
        return default


def enabled() -> bool:
    """CIVIC_ENABLED=0 (or false/off/no) takes the surface down, page included.

    Read per request on purpose: turning Civic off is an env change and a
    restart, not a redeploy, and it must not need one at all if the process
    can be signalled another way later.
    """
    raw = (os.environ.get("CIVIC_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


# At most this many synthesis calls in flight, ever. Two of ~40 shared threads
# is enough to serve a small launch city and small enough that the CRM cannot
# notice. Everything past it is shed immediately -- a queued request would hold
# a thread while it waited, which is the exact harm this prevents.
_MAX_CONCURRENCY = _int_env("CIVIC_MAX_CONCURRENCY", 2)
_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENCY or 1)

# Uncached answers per process per UTC day. The keys are shared with the rest
# of the app, so this is the ceiling on what Civic can spend of them.
_DAILY_CAP = _int_env("CIVIC_DAILY_ANSWERS", 250)
_day_lock = threading.Lock()
_day = ""
_day_count = 0


def _take_daily_slot() -> bool:
    """Count one answer against today's budget. False when the day is spent."""
    global _day, _day_count
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _day_lock:
        if today != _day:
            _day, _day_count = today, 0
        if _DAILY_CAP and _day_count >= _DAILY_CAP:
            return False
        _day_count += 1
        return True


def _reset_daily_budget() -> None:
    """Test seam. Nothing in the request path calls this."""
    global _day, _day_count
    with _day_lock:
        _day, _day_count = "", 0


class AskIn(BaseModel):
    question: str = Field(..., max_length=500)
    location: str = Field("", max_length=160)
    # Set when the answer came from a dropped pin rather than a typed city.
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class AskOut(BaseModel):
    id: str
    question: str
    location: str
    answer: dict
    cached: bool


@router.post("/ask", response_model=AskOut, dependencies=[Depends(_ASK_LIMIT)])
def ask(payload: AskIn) -> AskOut:
    """One question in, one evidence-ranked answer out."""
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})

    question = (payload.question or "").strip()
    location = (payload.location or "").strip()
    if not question:
        raise HTTPException(400, {"code": "empty_question",
                                  "message": "Ask a question first."})

    # A cache hit costs nothing and holds no slot : serve it before the fence.
    key = civic.cache_key(question, location)
    hit = civic.cache_get(key)
    if hit:
        print(f"  [civic] cache hit {key} tiers={''.join(hit['answer']['tiers']) or '-'}")
        return AskOut(id=key, cached=True, **hit)

    if not civic.available():
        # Deliberately explicit: the failure is configuration, not the question.
        # ANTHROPIC_API_KEY is the same key the rest of the app uses; EXA_API_KEY
        # is optional and only changes how the sources are found.
        raise HTTPException(503, {
            "code": "unconfigured",
            "message": ("Search isn't set up on this deployment: the server has "
                        "no ANTHROPIC_API_KEY."),
        })

    # Non-blocking : a request that waits for a slot is a held thread, which is
    # the harm this cap exists to prevent. Shed it instead and let the caller
    # retry. Taken before the daily budget so a shed request costs no quota.
    if not _SLOTS.acquire(blocking=False):
        print("  [civic] busy : all synthesis slots in use")
        raise HTTPException(503, {
            "code": "busy",
            "message": "Civic is answering as many questions as it can at once. Try again in a moment.",
        }, headers={"Retry-After": "20"})

    if not _take_daily_slot():
        # The shared keys are the reason this cap exists : better a bounded
        # surface than a surprise bill and a rate-limited CRM.
        _SLOTS.release()
        print(f"  [civic] daily cap reached ({_DAILY_CAP} answers)")
        raise HTTPException(429, {
            "code": "daily_cap",
            "message": ("Civic has answered as many questions as it can today. "
                        "Try again tomorrow."),
        }, headers={"Retry-After": "3600"})

    try:
        answer, notes = civic.synthesize(
            question, location, lat=payload.lat, lon=payload.lon,
        )
    except civic.CivicError as exc:
        # Log the shape of the failure; the question and place are already in
        # the request log, and we keep nothing else about the caller.
        print(f"  [civic] failed code={exc.code} q={question[:80]!r} loc={location!r}")
        raise HTTPException(422, {
            "code": exc.code,
            "message": str(exc),
            # What the model did say, so the page can offer "show what we found"
            # instead of a dead end.
            "raw": exc.raw[:4000],
        }) from exc
    finally:
        _SLOTS.release()

    print(
        f"  [civic] answered q={question[:80]!r} loc={location!r} "
        f"via={notes.get('sources')} found={notes.get('sources_found')} "
        f"tiers={''.join(answer['tiers']) or '-'} "
        f"evidence={len(answer['evidence'])} "
        f"dropped={notes['evidence_dropped']}/{notes['actions_dropped']} "
        f"retiered={notes.get('tiers_corrected')} "
        f"retried={notes.get('retried')} {notes['latency_ms']}ms"
    )

    record = {"question": question, "location": location, "answer": answer}
    civic.cache_put(key, record)
    return AskOut(id=key, cached=False, **record)


@router.get("/answer/{answer_id}", response_model=AskOut)
def get_answer(answer_id: str) -> AskOut:
    """The answer behind a permalink, while it is still in cache.

    Cache-only : it costs nothing and holds no slot. A miss is a 404 and the
    page re-asks the question, because answers are cheap to reproduce and
    nothing here is worth a database.
    """
    if not enabled():
        raise HTTPException(503, {"code": "disabled",
                                  "message": "Civic search is switched off here."})
    hit = civic.cache_get(answer_id.strip().lower())
    if not hit:
        raise HTTPException(404, {"code": "expired",
                                  "message": "That answer has expired. Ask it again."})
    return AskOut(id=answer_id, cached=True, **hit)


def _page():
    resp = FileResponse(str(_UI_HTML), media_type="text/html")
    # Shell-style no-store, matching the landing page: the HTML is the app, so
    # a deploy must not be served from a stale browser cache.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    return resp


if _UI_HTML.is_file():
    @pages_router.get("/civic")
    def civic_page():
        """The map. Spin the globe, drop a pin, ask."""
        if not enabled():
            raise HTTPException(404, "Not found")
        return _page()

    @pages_router.get("/civic/r/{answer_id}")
    def civic_permalink(answer_id: str):
        """A shared answer. Same page; the client fetches the id on load."""
        if not enabled():
            raise HTTPException(404, "Not found")
        return _page()
