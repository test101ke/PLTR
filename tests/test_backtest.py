"""
Offline checks for backtest.py. No network needed.

    python tests/test_backtest.py

A random walk must never "pass"; a planted open-session edge must be found;
holidays, early closes and missing days must be handled.
"""
import os, sys, random, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest as bt

NY = bt.NY


def synth(edge, seed, days=200, drop=()):
    """Fake 1m candles: a random first move, then `edge` drift in its direction."""
    rnd = random.Random(seed); px = 100.0; raw = []
    for day in bt.trading_days(days):
        o = int(dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY).timestamp() * 1000)
        px *= 1 + rnd.gauss(0, 0.015)
        win, first = {}, rnd.choice([1, -1])
        for m in range(-5, bt.WIN_MIN + 2):
            drift = first * 0.0015 if m in (1, 2) else first * edge if m > 2 else 0
            px *= 1 + drift + rnd.gauss(0, 0.0015)
            win[o + m * 60000] = [o + m * 60000, px, px, px, px, 1]
        cl = int(dt.datetime.combine(day, bt.close_time(day), tzinfo=NY).timestamp() * 1000)
        px *= 1 + rnd.gauss(0, 0.02)
        if day.isoformat() in drop:
            raw.append((day, {}, {}))
        else:
            raw.append((day, win, {cl: [cl, px, px, px, px, 1]}))
    return raw


def test_random_walk_never_passes():
    for seed in range(8):
        out = bt.analyse(bt.build_samples(synth(0.0, seed), {}), {"source": "t"})
        assert not out["passed70"], f"seed {seed}: random data passed ({out['best']})"


def test_planted_edge_is_found():
    for seed in range(4):
        out = bt.analyse(bt.build_samples(synth(0.0012, seed), {}), {"source": "t"})
        assert out["passed70"], f"seed {seed}: missed a real edge ({out['best']})"
        assert out["best"]["family"] == "momentum"


def test_holidays_and_weekends_skipped():
    days = bt.trading_days(400)
    assert all(d.weekday() < 5 for d in days)
    assert not {d.isoformat() for d in days} & bt.NYSE_HOLIDAYS


def test_early_close_time():
    assert bt.close_time(dt.date(2026, 11, 27)) == dt.time(12, 59)
    assert bt.close_time(dt.date(2026, 11, 30)) == dt.time(15, 59)


def test_missing_day_breaks_overnight_chain():
    raw = synth(0.0, 1, days=60)
    gap = raw[30][0].isoformat()
    after = raw[31][0].isoformat()
    samples = bt.build_samples(synth(0.0, 1, days=60, drop={gap}), {})
    got = {s["day"] for s in samples}
    assert gap not in got and after not in got, "overnight measured across a missing day"


def test_eat_clock():
    s = bt.build_samples(synth(0.0, 2, days=250), {})
    clocks = {x["eat"] for x in s}
    assert clocks <= {"16:30", "17:30"}, clocks


def test_news_join():
    raw = synth(0.0, 3, days=60)
    d = raw[40][0].isoformat()
    s = bt.build_samples(raw, {d: {"net": 3, "n": 5}})
    row = next(x for x in s if x["day"] == d)
    assert row["newsNet"] == 3 and row["newsN"] == 5


def regime_switch(seed, days=300):
    """Momentum edge for the first 55% of days, then the same-size reversal edge."""
    raw = synth(0.0012, seed, days); rnd = random.Random(seed + 99); k = int(len(raw) * 0.55)
    for i, (day, win, _) in enumerate(raw):
        if i < k or not win:
            continue
        o = int(dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY).timestamp() * 1000)
        first = 1 if win[o + 2 * 60000][4] > win[o][1] else -1
        px = win[o + bt.SIG_MIN * 60000][4]
        for t in sorted(win):
            if (t - o) // 60000 > bt.SIG_MIN:
                px *= 1 - first * 0.0012 + rnd.gauss(0, 0.0015)
                win[t] = [t, px, px, px, px, 1]
    return raw


def test_adaptive_random_never_passes():
    for seed in range(6):
        ad = bt.analyse(bt.build_samples(synth(0.0, seed, 300), {}), {})["adaptive"]
        assert not ad["passed"], f"seed {seed}: adaptive passed on random data"


def test_adaptive_follows_regime_switch():
    for seed in range(3):
        out = bt.analyse(bt.build_samples(regime_switch(seed), {}), {})
        ad, fixed = out["adaptive"], out["best"]
        assert ad["passed"], f"seed {seed}: adaptive missed the new regime"
        assert ad["test"]["total"] > 0 > fixed["testTotalPct"], "recency weighting should beat the stale fixed rule"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)
