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

The synthesis itself (prompt, evidence hierarchy, URL grounding) lives in
backend/civic.py; this module is the HTTP skin over it.
"""
from __future__ import annotations

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

# 10/min/IP: a query costs ~$0.05-0.15 in search + tokens, so this is a cost
# gate as much as an abuse gate. Its own tag, so it doesn't share a window
# with signup or checkout.
_ASK_LIMIT = per_ip_rate_limit(limit=10, window_s=60, tag="civic_ask")


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
    question = (payload.question or "").strip()
    location = (payload.location or "").strip()
    if not question:
        raise HTTPException(400, {"code": "empty_question",
                                  "message": "Ask a question first."})

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

    A miss is a 404 and the page re-asks the question : answers are cheap to
    reproduce and nothing here is worth a database.
    """
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
        """The map. Pan the globe, drop a pin, ask."""
        return _page()

    @pages_router.get("/civic/r/{answer_id}")
    def civic_permalink(answer_id: str):
        """A shared answer. Same page; the client fetches the id on load."""
        return _page()
