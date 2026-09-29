"""
Offline checks for the live engine and the AI supervisor's bounds. No network.

    python tests/test_engine.py

Replays one synthetic trading day through engine.tick with a fake clock and a
fake candle feed, and feeds the supervisor fake Claude answers.
"""
import os, sys, json, asyncio, tempfile, datetime as dt
os.environ["LOG_DIR"] = tempfile.mkdtemp()
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt
import engine as eng
from test_backtest import synth

NY = bt.NY


def setup(edge=0.0012, seed=5):
    raw = synth(edge, seed, days=200)
    today = raw[-1][0]
    store = {}
    for _, win, close in raw:
        store.update(win); store.update(close)

    async def fetch(a, b):
        lo, hi = int(a.timestamp() * 1000), int(b.timestamp() * 1000)
        return {t: r for t, r in store.items() if lo <= t <= hi}

    past = bt.build_samples(raw[:-1], {})
    e = eng.Engine()
    e.load(bt.analyse(past, {}))
    e.fetch = fetch
    return e, today, store


def at(day, h, m, s=0):
    return dt.datetime.combine(day, dt.time(h, m, s), tzinfo=NY)


def run(coro):
    return asyncio.run(coro)


def test_full_day_with_stop():
    e, day, store = setup()
    assert e.rule and e.rule["family"] == "momentum", e.rule
    run(e.tick(None, {}, now=at(day, 9, 0)))
    assert e.plan["phase"] == "waiting" and e.plan["openInSec"] == 1800
    run(e.tick(None, {}, now=at(day, 9, 31)))
    assert e.plan["phase"] == "watching first minutes"
    run(e.tick(None, {}, now=at(day, 9, 32, 10)))
    p = e.plan
    assert p["dir"] in (1, -1) and p["phase"] == "in trade", p
    # price runs 2% against the position: the 1% stop must fire on the next tick
    bad = p["entry"] * (1 - p["dir"] * 0.02)
    run(e.tick(None, {"crypto": {"mark": bad}}, now=at(day, 9, 36)))
    assert p["exited"] == "stop" and p["phase"] == "stopped out"
    n0 = len(e.samples)
    run(e.tick(None, {}, now=at(day, 9, 46)))
    assert p["resolved"] and p["phase"] == "done"
    assert abs(p["resultPct"] - (-2.0 - bt.FEE_PCT)) < 1e-6, p["resultPct"]
    assert len(e.samples) == n0 + 1 and e.samples[-1]["day"] == day.isoformat(), "outcome not fed back"


def test_ai_can_veto_before_entry():
    e, day, _ = setup()
    e.day = day
    run(e.tick(None, {}, now=at(day, 9, 10)))
    e.ai = {"action": "stand_aside", "size": 0.0, "stopPct": 1.0, "reason": "CEO resigned overnight"}
    run(e.tick(None, {}, now=at(day, 9, 32, 10)))
    assert e.plan["dir"] == 0 and "CEO resigned" in e.plan["why"]


def test_ai_reduce_scales_result():
    e, day, _ = setup()
    run(e.tick(None, {}, now=at(day, 9, 10)))
    e.ai = {"action": "reduce", "size": 0.5, "stopPct": 1.0, "reason": "mixed news"}
    run(e.tick(None, {}, now=at(day, 9, 32, 10)))
    assert e.plan["size"] == 0.5
    run(e.tick(None, {}, now=at(day, 9, 46)))
    full = (e.samples[-1]["r_rem"] * 100 * e.plan["dir"] - bt.FEE_PCT)
    assert abs(e.plan["resultPct"] - round(full * 0.5, 3)) < 1e-3


def test_clamp_never_adds_risk():
    in_trade = {"dir": 1, "decidedAt": "x"}
    assert eng.clamp('{"action":"go","size":5,"stopPct":9,"reason":""}', {})["size"] == 1.0
    assert eng.clamp('{"action":"go","size":5,"stopPct":9,"reason":""}', {})["stopPct"] == 1.5
    assert eng.clamp('{"action":"reduce","size":-3,"stopPct":0,"reason":""}', {})["size"] == 0.0
    assert eng.clamp('{"action":"stand_aside","size":0,"stopPct":1,"reason":""}', in_trade)["action"] == "exit"
    assert eng.clamp('{"action":"exit","size":0,"stopPct":1,"reason":""}', {})["action"] == "stand_aside"
    assert eng.clamp('{"action":"flip_short","size":1,"stopPct":1,"reason":""}', {})["action"] == "go"
    assert eng.clamp("not json", {})["action"] == "go"


class _Block:
    def __init__(self, text): self.type, self.text = "text", text


class _Resp:
    def __init__(self, text): self.stop_reason, self.content = "end_turn", [_Block(text)]


class FakeClaude:
    """Stands in for anthropic.AsyncAnthropic: records calls, returns a canned answer."""
    def __init__(self, answer):
        self.calls, self.answer = [], answer
        outer = self

        class _M:
            async def create(self, **kw):
                outer.calls.append(kw); return _Resp(outer.answer)

        class _B:
            messages = _M()
        self.beta = _B()


def test_supervisor_window_and_bounds():
    e, day, _ = setup()
    ai = FakeClaude(json.dumps({"action": "reduce", "size": 3, "stopPct": 0.1, "reason": "headline conflicts"}))
    run(e.tick(None, {}, now=at(day, 8, 0)))
    run(e.supervise(ai, {}, now=at(day, 8, 0)))
    assert not ai.calls, "supervisor must sleep outside the open window"
    run(e.supervise(ai, {"news": [], "crypto": {}, "signal": {}}, now=at(day, 9, 5)))
    assert len(ai.calls) == 1
    kw = ai.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["model"] == eng.SUP_MODEL
    assert e.ai["action"] == "reduce" and e.ai["size"] == 1.0 and e.ai["stopPct"] == 0.3
    run(e.supervise(ai, {"news": [], "crypto": {}, "signal": {}}, now=at(day, 9, 5)))
    assert len(ai.calls) == 1, "no second call without a new event or 60s passing"
    e.poke("news"); e.last_ai -= 11
    run(e.supervise(ai, {"news": [], "crypto": {}, "signal": {}}, now=at(day, 9, 5)))
    assert len(ai.calls) == 2, "a fresh headline should wake it"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as ex:
                fails += 1; print("FAIL", name, "-", ex)
    sys.exit(1 if fails else 0)
