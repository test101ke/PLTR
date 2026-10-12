import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reel_backtest as rb

NY = rb.NY
DAY = dt.date(2026, 10, 6)


def bars(path):
    """path: list of (o, h, l, c) per minute from 09:30."""
    t0 = dt.datetime.combine(DAY, dt.time(9, 30), tzinfo=NY)
    return [(t0 + dt.timedelta(minutes=i), *p) for i, p in enumerate(path)]


RANGE = [(100, 101, 99, 100)] * 15            # range 99 - 101


def test_long_breakout_hits_2r_target():
    path = RANGE + [(100.5, 101.6, 100.4, 101.5)] + [(101.5, 101.6, 101.4, 101.5)] + \
           [(101.5, 106.0, 101.4, 105.5)] + [(105.5, 105.6, 105.4, 105.5)] * 400
    t = rb.simulate(bars(path), target_r=2)
    # entry 101.5 at next open, stop 99 (risk 2.5), target 106.5 not reached -> held to close
    assert t["side"] == "long" and t["entry"] == 101.5 and t["why"] == "close"
    t = rb.simulate(bars(path), target_r=1)    # target 104.0 is reached
    assert t["why"] == "target" and abs(t["gross"] - (104 / 101.5 - 1) * 100) < 1e-9
    assert abs(t["net"] - (t["gross"] - rb.FEE_PCT)) < 1e-12


def test_short_stop_counts_first_when_both_touched():
    path = RANGE + [(99.5, 99.6, 98.4, 98.5)] + [(98.5, 101.2, 95.0, 98.0)] + [(98, 98, 98, 98)] * 400
    t = rb.simulate(bars(path), target_r=1)
    assert t["side"] == "short" and t["why"] == "stop" and t["exit"] == 101


def test_no_breakout_before_noon_means_no_trade():
    assert rb.simulate(bars(RANGE + [(100, 100.5, 99.5, 100)] * 400), target_r=2) is None
