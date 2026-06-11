"""metrics.py : tiny in-process metrics so you can monitor health from ONE URL
instead of scrolling Railway logs.

The request-log middleware feeds request outcomes here; the book LLM helper feeds
Claude-call outcomes here; the rate-gate reports its live in-flight state. An
admin endpoint (/api/book/_status) returns a snapshot: counts, recent errors /
slow requests, per-route latency, and whether the relationship layer is
throttling right now.

Caveat: in-memory and PER-REPLICA. Prod runs ~2 replicas, so a snapshot reflects
the one replica that answered -- hit it a couple times for a fuller picture. Good
enough to answer "is it erroring / slow / throttling" without log-diving; a real
multi-replica view needs an external collector (Sentry / Better Stack) later.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_BOOT = time.time()

# Rolling counters.
_req = defaultdict(int)          # total, 2xx, 3xx, 4xx, 5xx, slow
_llm = defaultdict(int)          # ok, err
# Recent notable events (errors + slow), newest last.
_recent = deque(maxlen=60)       # {ts, kind, method, path, status, ms, detail}
# Per-route recent latencies for p50/p95.
_route_ms = defaultdict(lambda: deque(maxlen=120))
_llm_ms = deque(maxlen=200)      # recent Claude-call durations (ms)


def record_request(method: str, path: str, status: int, ms: float) -> None:
    with _lock:
        _req["total"] += 1
        _req[f"{(status // 100) if status else 0}xx"] += 1
        if ms >= 5000:
            _req["slow"] += 1
        _route_ms[f"{method} {path}"].append(ms)
        if status >= 400 or ms >= 5000:
            _recent.append({"ts": time.time(), "kind": "req", "method": method,
                            "path": path, "status": status, "ms": round(ms)})


def record_llm(label: str, ms: float, ok: bool, detail: str = "") -> None:
    with _lock:
        _llm["ok" if ok else "err"] += 1
        _llm_ms.append(ms)
        if not ok:
            _recent.append({"ts": time.time(), "kind": "llm", "label": label,
                            "ms": round(ms), "detail": detail})


def _pct(samples, q):
    if not samples:
        return 0
    s = sorted(samples)
    i = min(len(s) - 1, int(len(s) * q))
    return round(s[i])


def snapshot() -> dict:
    # Pull the gate's live state without a hard import dependency.
    try:
        from .agents import rategate
        gate = rategate.stats()
    except Exception:  # noqa: BLE001
        gate = {}
    with _lock:
        now = time.time()
        routes = {}
        for r, d in _route_ms.items():
            if d:
                routes[r] = {"n": len(d), "p50": _pct(d, 0.50),
                             "p95": _pct(d, 0.95), "max": round(max(d))}
        recent = [{**e, "age_s": round(now - e["ts"], 1)} for e in _recent]
        return {
            "uptime_s": round(now - _BOOT),
            "requests": dict(_req),
            "llm": {**dict(_llm), "p50_ms": _pct(_llm_ms, 0.50),
                    "p95_ms": _pct(_llm_ms, 0.95)},
            "gate": gate,
            "recent": list(reversed(recent)),  # newest first
        }
