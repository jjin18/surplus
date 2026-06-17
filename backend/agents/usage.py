"""
agents/usage.py : the LLM token + cost ledger.

The app has ~23 independent Anthropic call sites (each builds its own client
and picks its own model). Rather than edit all of them, `install()` patches
the SDK's `Messages.create` / `AsyncMessages.create` ONCE at startup so every
call — current and future — records a row in the `llm_usage` table.

Design guarantees:
- Recording is best-effort: the entire write path is wrapped in try/except and
  runs in its OWN short-lived session, so a DB hiccup can never break, slow, or
  poison the caller's transaction or its LLM response.
- `feature` is inferred from the call stack ("<module>.<func>") so spend is
  attributable per workflow with zero changes to call sites.
- `cost_usd` is computed here from a static price table and frozen into the row.

Caveats:
- Token cost only. Server-side tool use (web_search_20260209, ~$10/1k searches)
  is NOT in the usage object, so it is not captured here.
- Streaming calls (`messages.stream(...)` / `create(stream=True)`) return a
  stream object with no `.usage`; those are skipped (none of the current call
  sites stream).
"""
from __future__ import annotations

import sys
import time
from typing import Any, Optional

# Per-TOKEN prices (USD), Claude 4.x family. Keyed by family substring because
# model strings carry version/date suffixes (e.g. "claude-haiku-4-5-20251001").
# Tuple = (input, output, cache_write, cache_read). Update when list prices move
# — historical rows keep whatever cost was frozen at write time.
_M = 1_000_000
PRICES: dict[str, tuple[float, float, float, float]] = {
    "opus":   (15.0 / _M, 75.0 / _M, 18.75 / _M, 1.50 / _M),
    "sonnet": (3.0 / _M,  15.0 / _M, 3.75 / _M,  0.30 / _M),
    "haiku":  (1.0 / _M,  5.0 / _M,  1.25 / _M,  0.10 / _M),
}


def _rates(model: str) -> Optional[tuple[float, float, float, float]]:
    m = (model or "").lower()
    for family, rates in PRICES.items():
        if family in m:
            return rates
    return None


def compute_cost(model: str, in_tok: int, out_tok: int,
                 cache_read: int, cache_write: int) -> float:
    rates = _rates(model)
    if rates is None:
        return 0.0
    r_in, r_out, r_cw, r_cr = rates
    return (in_tok * r_in + out_tok * r_out
            + cache_write * r_cw + cache_read * r_cr)


def _infer_feature() -> str:
    """Walk the stack for the first app frame that isn't this module or the SDK.

    Returns "<module-after-'backend.'>.<func>", e.g.
    "agents.relationship_agent.draft_outreach". A stack walk costs microseconds
    against an LLM round trip of hundreds of ms, so it's free in practice.
    """
    try:
        frame = sys._getframe(2)  # 0=_infer_feature, 1=_record_*, 2=patched create
    except ValueError:
        return ""
    while frame is not None:
        mod = frame.f_globals.get("__name__", "")
        if (mod.startswith("backend.")
                and not mod.endswith("agents.usage")):
            short = mod[len("backend."):]
            return f"{short}.{frame.f_code.co_name}"
        frame = frame.f_back
    return ""


def _record(model: str, usage: Any, latency_ms: int, feature: str) -> None:
    """Write one ledger row. Never raises — logging must not break a call."""
    try:
        # Imported lazily so importing this module never drags in the ORM/engine
        # at SDK-patch time, and so tests can import it without a DB.
        from ..db import SessionLocal
        from .. import models

        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cost = compute_cost(model, in_tok, out_tok, cache_read, cache_write)

        db = SessionLocal()
        try:
            db.add(models.LlmUsage(
                model=model[:60],
                feature=feature[:120],
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost_usd=cost,
                latency_ms=latency_ms,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 : ledger is best-effort only
        print(f"  [usage] record failed (ignored): {type(exc).__name__}: {exc}")


def _record_from_response(resp: Any, kwargs: dict, latency_ms: int) -> None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return  # streaming object or unexpected shape : nothing to bill
    model = getattr(resp, "model", None) or kwargs.get("model") or ""
    _record(str(model), usage, latency_ms, _infer_feature())


_INSTALLED = False


def install() -> None:
    """Patch the Anthropic SDK's create methods. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from anthropic.resources.messages import Messages, AsyncMessages
    except Exception as exc:  # noqa: BLE001 : SDK missing/renamed : log + skip
        print(f"  [usage] install skipped (no SDK): {type(exc).__name__}: {exc}")
        return

    _orig_create = Messages.create
    _orig_acreate = AsyncMessages.create

    def _patched_create(self, *args, **kwargs):
        t0 = time.perf_counter()
        resp = _orig_create(self, *args, **kwargs)
        try:
            _record_from_response(resp, kwargs, int((time.perf_counter() - t0) * 1000))
        except Exception:  # noqa: BLE001 : never let the ledger break a call
            pass
        return resp

    async def _patched_acreate(self, *args, **kwargs):
        t0 = time.perf_counter()
        resp = await _orig_acreate(self, *args, **kwargs)
        try:
            _record_from_response(resp, kwargs, int((time.perf_counter() - t0) * 1000))
        except Exception:  # noqa: BLE001
            pass
        return resp

    Messages.create = _patched_create
    AsyncMessages.create = _patched_acreate
    _INSTALLED = True
    print("  [usage] LLM token ledger installed (Messages.create patched)")
