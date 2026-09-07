"""
AMD / Power-of-3 session model
==============================
Accumulation (Asia) -> Manipulation (London) -> Distribution (New York).

  * Asia builds a range: its high and low are the liquidity pools.
  * London "manipulates": price sweeps OUT of that range (stop-hunt) and reverses.
    A sweep of the Asia LOW is bullish (sellers trapped); a sweep of the HIGH is bearish.
  * New York "distributes": price expands in the true direction. Entry on the reclaim
    back into the range, invalidation beyond the sweep extreme, target the opposite side
    of the range and then a measured move.

This runs on the TOKENIZED PLTR perp series, because that market trades ~24/5 and so
actually has Asia and London sessions. The real NASDAQ stock only trades the NY leg.

Sessions (UTC), with the New York open anchored to 09:30 America/New_York so it stays
correct across US daylight-saving changes:
  Asia    00:00 -> 07:00 UTC
  London  07:00 -> NY open
  NY      NY open -> +6h30m
"""
import datetime as dt
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ASIA_START_H, ASIA_END_H = 0, 7


def _ms(d):
    return int(d.timestamp() * 1000)


def session_windows(day):
    """Return (asia, london, ny) windows as (start_ms, end_ms) for a UTC date."""
    d0 = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
    asia = (_ms(d0 + dt.timedelta(hours=ASIA_START_H)), _ms(d0 + dt.timedelta(hours=ASIA_END_H)))
    ny_open_local = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
    ny_open = _ms(ny_open_local)
    ny = (ny_open, _ms(ny_open_local + dt.timedelta(hours=6, minutes=30)))
    london = (asia[1], ny_open)
    return asia, london, ny


def _slice(candles, w):
    return [c for c in candles if w[0] <= c["t"] < w[1]]


def compute(candles, venue=None, symbol=None, now_ms=None):
    """Build the AMD read from a list of {'t','o','h','l','c','v'} candles (oldest->newest)."""
    out = {"venue": venue, "symbol": symbol, "ok": False, "note": ""}
    if not candles or len(candles) < 20:
        out["note"] = "not enough candle history yet"
        return out
    now_ms = now_ms or int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    today = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).date()

    asia_w, london_w, ny_w = session_windows(today)
    # Before Asia has produced anything today, fall back to yesterday's cycle.
    if not _slice(candles, asia_w):
        today = today - dt.timedelta(days=1)
        asia_w, london_w, ny_w = session_windows(today)

    a = _slice(candles, asia_w); l = _slice(candles, london_w); n = _slice(candles, ny_w)
    if not a:
        out["note"] = "no Asia-session candles in range"
        return out

    asia_high = max(c["h"] for c in a if c["h"])
    asia_low = min(c["l"] for c in a if c["l"])
    rng = asia_high - asia_low

    # ---- phase ----
    if now_ms < asia_w[1]:
        phase, phase_label = "A", "Accumulation (Asia)"
    elif now_ms < london_w[1]:
        phase, phase_label = "M", "Manipulation (London)"
    elif now_ms < ny_w[1]:
        phase, phase_label = "D", "Distribution (New York)"
    else:
        phase, phase_label = "-", "Session closed"

    # ---- manipulation: did London sweep either side? ----
    swept, sweep_px, sweep_t = None, None, None
    if l:
        low_hits = [c for c in l if c["l"] is not None and c["l"] < asia_low]
        high_hits = [c for c in l if c["h"] is not None and c["h"] > asia_high]
        deep_low = min((c["l"] for c in low_hits), default=None)
        deep_high = max((c["h"] for c in high_hits), default=None)
        if deep_low is not None and deep_high is not None:
            # both sides taken — treat the more extreme excursion as the manipulation
            if (asia_low - deep_low) >= (deep_high - asia_high):
                swept, sweep_px = "low", deep_low
                sweep_t = min(c["t"] for c in low_hits)
            else:
                swept, sweep_px = "high", deep_high
                sweep_t = min(c["t"] for c in high_hits)
        elif deep_low is not None:
            swept, sweep_px, sweep_t = "low", deep_low, min(c["t"] for c in low_hits)
        elif deep_high is not None:
            swept, sweep_px, sweep_t = "high", deep_high, min(c["t"] for c in high_hits)

    bias = "bullish" if swept == "low" else "bearish" if swept == "high" else "none"

    # ---- reclaim confirmation: close back inside the Asia range after the sweep ----
    reclaimed, reclaim_t = False, None
    if swept and sweep_t:
        after = [c for c in candles if c["t"] >= sweep_t]
        for c in after:
            if swept == "low" and c["c"] and c["c"] > asia_low:
                reclaimed, reclaim_t = True, c["t"]; break
            if swept == "high" and c["c"] and c["c"] < asia_high:
                reclaimed, reclaim_t = True, c["t"]; break

    last = candles[-1]["c"]

    # ---- levels ----
    levels = None
    if swept and rng > 0:
        if bias == "bullish":
            entry, stop = asia_low, sweep_px
            t1, t2 = asia_high, asia_high + rng
        else:
            entry, stop = asia_high, sweep_px
            t1, t2 = asia_low, asia_low - rng
        risk = abs(entry - stop)
        levels = {"side": "long" if bias == "bullish" else "short",
                  "entry": round(entry, 2), "stop": round(stop, 2),
                  "t1": round(t1, 2), "t2": round(t2, 2),
                  "risk": round(risk, 2),
                  "rr1": round(abs(t1 - entry) / risk, 2) if risk else None,
                  "rr2": round(abs(t2 - entry) / risk, 2) if risk else None}

    # ---- has NY actually expanded? ----
    expanded = False
    if n and levels:
        if bias == "bullish":
            expanded = max(c["h"] for c in n if c["h"]) >= levels["t1"]
        else:
            expanded = min(c["l"] for c in n if c["l"]) <= levels["t1"]

    # ---- plain-English status ----
    if not swept:
        status = ("London has not swept either side of the Asia range yet — no manipulation leg, so no setup."
                  if phase in ("M", "D") else "Asia is still building its range. Wait for London to sweep a side.")
    elif not reclaimed:
        status = (f"London swept the Asia {swept} at {sweep_px:.2f}. Waiting for price to reclaim back inside "
                  f"the range to confirm the {bias} leg.")
    elif expanded:
        status = (f"Full AMD played out: Asia ranged, London swept the {swept}, New York expanded "
                  f"{'up' if bias=='bullish' else 'down'} through {levels['t1']:.2f}.")
    else:
        status = (f"Sweep of the Asia {swept} confirmed and reclaimed — {bias} bias into New York. "
                  f"Target {levels['t1']:.2f}, invalid below {levels['stop']:.2f}."
                  if bias == "bullish" else
                  f"Sweep of the Asia {swept} confirmed and reclaimed — {bias} bias into New York. "
                  f"Target {levels['t1']:.2f}, invalid above {levels['stop']:.2f}.")

    # ---- forward projection: potential upside / downside from here ----
    up_measured = asia_high + rng          # measured move up after a low sweep
    dn_measured = asia_low - rng           # measured move down after a high sweep
    def _pct(target):
        return round((target / last - 1) * 100, 2) if (last and target) else None
    if levels and bias == "bullish":
        projection = {"mode": "directional", "bias": bias, "from": last,
                      "up": {"t1": levels["t1"], "t2": levels["t2"]}, "down": None,
                      "invalid": levels["stop"],
                      "upPct": _pct(levels["t1"]), "downPct": _pct(levels["stop"]),
                      "note": "Sweep of the Asia low is done — the model looks for expansion UP through the range. "
                              "Invalidated if price loses the sweep low."}
    elif levels and bias == "bearish":
        projection = {"mode": "directional", "bias": bias, "from": last,
                      "up": None, "down": {"t1": levels["t1"], "t2": levels["t2"]},
                      "invalid": levels["stop"],
                      "upPct": _pct(levels["stop"]), "downPct": _pct(levels["t1"]),
                      "note": "Sweep of the Asia high is done — the model looks for expansion DOWN through the range. "
                              "Invalidated if price reclaims the sweep high."}
    else:
        projection = {"mode": "undecided", "bias": "none", "from": last,
                      "up": {"t1": round(asia_high, 2), "t2": round(up_measured, 2)},
                      "down": {"t1": round(asia_low, 2), "t2": round(dn_measured, 2)},
                      "invalid": None,
                      "upPct": _pct(up_measured), "downPct": _pct(dn_measured),
                      "note": "No manipulation leg yet, so both sides are live: if London sweeps the LOW expect "
                              "expansion up; if it sweeps the HIGH expect expansion down."}

    out.update({
        "ok": True, "day": today.isoformat(),
        "dayStart": asia_w[0], "dayEnd": ny_w[1], "nowMs": now_ms, "projection": projection,
        "phase": phase, "phaseLabel": phase_label,
        "asiaHigh": round(asia_high, 2), "asiaLow": round(asia_low, 2), "range": round(rng, 2),
        "swept": swept, "sweepPx": round(sweep_px, 2) if sweep_px else None, "sweepT": sweep_t,
        "bias": bias, "reclaimed": reclaimed, "reclaimT": reclaim_t, "expanded": expanded,
        "levels": levels, "last": last, "status": status,
        "sessions": {"asia": asia_w, "london": london_w, "ny": ny_w},
        "candles": [{"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
                    for c in candles if c["c"]][-320:],
    })
    return out
