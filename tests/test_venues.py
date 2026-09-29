"""
Offline checks that each exchange's 1-minute candle format is parsed right.
Sample payloads follow each exchange's documented response shape.

    python tests/test_venues.py
"""
import os, sys, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest as bt

T = 1_759_152_600_000                       # a minute boundary, in ms


class Client:
    def __init__(self, payload):
        self.payload, self.urls = payload, []

    async def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        p = self.payload

        class R:
            def raise_for_status(self): pass
            def json(self): return p
        return R()


def run(fn, payload, *a):
    c = Client(payload)
    return asyncio.run(fn(c, *a)), c.urls[0]


def check(rows):
    assert rows and rows[0] == [T, 10.0, 11.0, 9.0, 10.5, 7.0], rows


def test_gate():
    rows, url = run(bt.gate_kl, [{"t": T // 1000, "o": "10", "h": "11", "l": "9", "c": "10.5", "v": 7}],
                    "PLTR_USDT", T, T + 60000)
    check(rows); assert f"from={T // 1000}" in url, "Gate takes seconds"


def test_bitget():
    rows, url = run(bt.bitget_kl, {"data": [[str(T), "10", "11", "9", "10.5", "7", "70"]]}, "PLTRUSDT", T, T + 60000)
    check(rows); assert f"startTime={T}" in url


def test_mexc():
    rows, url = run(bt.mexc_kl, {"data": {"time": [T // 1000], "open": [10], "high": [11], "low": [9],
                                          "close": [10.5], "vol": [7]}}, "PLTR_USDT", T, T + 60000)
    check(rows); assert f"start={T // 1000}" in url, "MEXC takes seconds"


def test_okx():
    rows, url = run(bt.okx_kl, {"data": [[str(T), "10", "11", "9", "10.5", "7", "0", "0", "1"]]},
                    "PLTR-USDT-SWAP", T, T + 60000)
    check(rows); assert "history-candles" in url


def test_kucoin_uses_milliseconds():
    rows, url = run(bt.kucoin_kl, {"data": [[T, 10, 11, 9, 10.5, 7]]}, "PLTRUSDTM", T, T + 60000)
    check(rows); assert f"from={T}&" in url, "KuCoin futures takes milliseconds"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)
