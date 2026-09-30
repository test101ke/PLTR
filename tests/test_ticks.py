"""
Offline checks for the 1-second, trade-built ORB backtest. No network.

    python tests/test_ticks.py
"""
import os, sys, asyncio, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tick_backtest as tb
import orb_backtest as ob
import backtest as bt

A = 1_759_239_000_000                          # a whole second, in ms


class Client:
    """Returns the queued payloads in order, then empty."""
    def __init__(self, *payloads):
        self.payloads, self.urls = list(payloads), []

    async def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        p = self.payloads.pop(0) if self.payloads else []

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return p
        return R()


def test_bitget_trades_parse_and_window():
    c = Client({"data": [{"tradeId": "2", "price": "30.5", "size": "3", "side": "Sell", "ts": str(A + 500)},
                         {"tradeId": "1", "price": "30.4", "size": "2", "side": "Buy", "ts": str(A + 100)},
                         {"tradeId": "0", "price": "30.0", "size": "9", "side": "Buy", "ts": str(A - 5)}]})
    t = asyncio.run(tb.bitget_trades(c, "PLTRUSDT", A, A + 1000))
    assert sorted(t) == [(A + 100, 30.4, 2.0, 1), (A + 500, 30.5, 3.0, -1)], t   # outside-window trade dropped


def test_okx_trades_parse():
    c = Client({"data": [{"ts": str(A + 700), "px": "30.1", "sz": "4", "side": "buy"},
                         {"ts": str(A - 10), "px": "30.0", "sz": "1", "side": "sell"}]})
    t = asyncio.run(tb.okx_trades(c, "PLTR-USDT-SWAP", A, A + 1000))
    assert t == [(A + 700, 30.1, 4.0, 1)], t
    assert "type=2" in c.urls[0] and f"after={A + 1000}" in c.urls[0]


def test_gate_trades_parse_signed_size():
    c = Client([{"id": 1, "create_time": (A + 300) / 1000, "create_time_ms": A + 300, "price": "30.2", "size": -5}])
    t = asyncio.run(tb.gate_trades(c, "PLTR_USDT", A, A + 1000))
    assert t == [(A + 300, 30.2, 5.0, -1)], t


def test_one_second_bars_and_taker_flow():
    trades = [(A + 100, 10.0, 1, 1), (A + 900, 10.2, 3, 1), (A + 1500, 10.1, 4, -1), (A + 3200, 10.3, 2, 1)]
    bars = tb.to_bars(trades, A, A + 4000)
    assert bars[A][:6] == [A, 10.0, 10.2, 10.0, 10.2, 4], bars[A]
    assert bars[A][6] == 100.0                                    # all buying so far
    assert bars[A + 1000][6] == 50.0                              # 4 buy / 4 sell over the window
    assert bars[A + 2000][1:5] == [10.1] * 4 and bars[A + 2000][5] == 0.0   # empty second carries last price
    assert list(bars) == [A, A + 1000, A + 2000, A + 3000]


def _day_bars(day, path):
    """path: list of (seconds after 09:30, price, buyShare). Fills every second in between."""
    op = int(dt.datetime.combine(day, dt.time(9, 30), tzinfo=bt.NY).timestamp() * 1000)
    bars, pts = {}, sorted(path)
    for i, (sec, px, share) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else sec + 1
        for s in range(sec, nxt):
            t = op + s * 1000
            bars[t] = [t, px, px, px, px, 1.0, share]
    return bars


def test_flow_filter_only_trades_with_the_buyers():
    day = dt.date(2026, 9, 29)
    rng = [(s, 100.0 + (0.1 if s % 2 else -0.1), 50) for s in range(0, 120)]         # 2-minute range 99.9-100.1
    up_with_buyers = rng + [(121, 100.3, 80), (125, 100.6, 80), (130, 101.0, 80), (135, 100.9, 80), (900, 100.9, 50)]
    up_with_sellers = rng + [(121, 100.3, 20), (125, 100.6, 20), (130, 101.0, 20), (900, 101.0, 50)]
    p = {k: v for k, v in ob.tr.PRESETS["runner"].items() if k not in ("label", "note")}
    buy = ob.replay(p, [(day, _day_bars(day, up_with_buyers), None)], span_ms=1000, use_flow=True)
    sell = ob.replay(p, [(day, _day_bars(day, up_with_sellers), None)], span_ms=1000, use_flow=True)
    assert buy and buy[0]["side"] == "long" and buy[0]["pnl"] > 1.0, buy
    assert sell == [], "a breakout against the taker flow must be skipped"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)
