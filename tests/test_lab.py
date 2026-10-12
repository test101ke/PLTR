import os, sys, random, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import strategy_lab as lab

NY = lab.rb.NY


def session(day, start, drift, rnd):
    t0 = dt.datetime.combine(day, dt.time(9, 30), tzinfo=NY)
    out, p = [], start
    for i in range(390):
        o = p; p = p * (1 + drift + rnd.gauss(0, 0.0005))
        out.append((t0 + dt.timedelta(minutes=i), o, max(o, p), min(o, p), p))
    return out


def test_lab_finds_a_planted_drift_and_nothing_in_noise():
    rnd, days, p = random.Random(1), [], 100.0
    d = dt.date(2025, 1, 6)
    for i in range(200):
        s = session(d + dt.timedelta(days=i), p, 0.00002, rnd)    # +0.78% a day, open to close
        days.append(s); p = s[-1][4]
    rows, _ = lab.evaluate(days)
    by = {r["rule"]: r for r in rows}
    assert by["always long, open to close"]["net"] > 0.5 and by["always long, open to close"]["t"] > 2
    assert by["always short, open to close"]["net"] < -0.5
    rnd, days, p = random.Random(2), [], 100.0
    for i in range(200):
        s = session(d + dt.timedelta(days=i), p, 0.0, rnd); days.append(s); p = s[-1][4]
    rows, _ = lab.evaluate(days)
    assert not [r for r in rows if r["train"] > 0 and r["test"] > 0 and r["t"] > 2]   # pure noise: nothing "real"
