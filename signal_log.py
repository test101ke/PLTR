"""
Append-only record of what the desk actually called, and how it turned out.
===========================================================================

Everything in the dashboard is live state that gets overwritten every second, so
until now there was no way to ask "what did it say on Tuesday, and was it right?"
Backtests had to RECONSTRUCT calls from candles, which only works for the pure
price rules — the composite signal blends order flow and news that nobody stored,
so it could never be scored at all.

This writes one JSONL row per call, plus a separate row when the outcome lands.
Two files, both append-only, both safe to lose (the desk runs fine without them):

  signals.jsonl   one row each time the call CHANGES (not every tick)
  outcomes.jsonl  one row per resolved AMD setup (target or invalidation hit)
"""
import os, json, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.getenv("LOG_DIR", os.path.join(HERE, "logs"))
SIGNALS = os.path.join(DIR, "signals.jsonl")
OUTCOMES = os.path.join(DIR, "outcomes.jsonl")

_last = {}          # kind -> the fingerprint we last wrote, so we log changes only


def _ensure():
    try:
        os.makedirs(DIR, exist_ok=True)
        return True
    except Exception:
        return False


def _append(path, row):
    if not _ensure():
        return False
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return True
    except Exception:
        return False


def log_signal(kind: str, payload: dict, fingerprint=None):
    """Record a call. Writes only when `fingerprint` changes, so the file stays
    a log of DECISIONS rather than 86,400 rows a day of unchanged state."""
    fp = fingerprint if fingerprint is not None else json.dumps(payload, sort_keys=True, default=str)
    if _last.get(kind) == fp:
        return False
    _last[kind] = fp
    row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "kind": kind, **payload}
    return _append(SIGNALS, row)


def log_outcome(payload: dict):
    row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **payload}
    return _append(OUTCOMES, row)


def tail(path, n=200):
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def read_all():
    return {"signals": tail(SIGNALS, 500), "outcomes": tail(OUTCOMES, 500),
            "dir": DIR,
            "counts": {"signals": _count(SIGNALS), "outcomes": _count(OUTCOMES)}}


def _count(p):
    try:
        with open(p) as f:
            return sum(1 for _ in f)
    except Exception:
        return 0
