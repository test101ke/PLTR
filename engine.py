"""
Live open-session engine: the trade the backtest studies, run for real each day.
===============================================================================
Two layers, because an AI model cannot react in milliseconds (a call takes
seconds) and should not be free to rewrite a trading rule on a whim.

FAST LAYER (plain code, every tick, ~1s):
  * ranks the open-session rules with recency weights (adaptive.py), and
    re-ranks the moment a new open's outcome is known, so the newest days
    always count most;
  * at 09:30 + SIG_MIN New York (16:32 EAT, 17:32 in US winter) computes
    today's features with the backtest's own code and takes the top rule's call;
  * while in the trade, checks every tick against a stop and exits if hit;
  * at 09:30 + WIN_MIN records the outcome and feeds it back into the ranking.

AI SUPERVISOR (Claude, event-driven, only around the open):
  Wakes when something changes (new headline, decision made, stop close) or
  every 60s from 09:00 to the exit. It sees the plan, rule ranking, recent
  outcomes, fresh headlines and live market, and may only make the trade
  SMALLER or SAFER: stand aside, reduce size, tighten/widen the stop within
  bounds, or exit early. It can never flip direction or add size. Every
  decision is logged. With no API key the fast layer runs alone.
"""
import os, json, time, asyncio, datetime as dt
import backtest as bt
import adaptive
import newsfeed
import signal_log

NY = bt.NY
SUP_MODEL = os.getenv("SUPERVISOR_MODEL", "claude-opus-5-5")
DEFAULT_HL = 20
STOP_PCT = float(os.getenv("GUARD_STOP_PCT", "1.0"))        # adverse move that forces an exit
STOP_BOUNDS = (0.3, 1.5)
SUP_EVERY = float(os.getenv("SUPERVISOR_INTERVAL", "60"))
SUP_MIN_GAP = 10.0                                          # seconds between event-driven calls

SUP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["go", "reduce", "stand_aside", "exit"]},
        "size": {"type": "number"},
        "stopPct": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["action", "size", "stopPct", "reason"],
    "additionalProperties": False,
}

SUP_SYSTEM = (
    "You supervise one automated trade: the first 15 minutes after the NASDAQ open in a PLTR "
    "perpetual contract. A rule chosen from recency-weighted backtests gives the direction. "
    "You cannot change the direction or increase size. You may: 'go' (let the rule trade), "
    "'reduce' (trade smaller), 'stand_aside' (skip today, before entry), or 'exit' (close an open "
    "trade now). size is 0 to 1 of normal size. stopPct is the adverse move in percent that forces "
    "an exit, between 0.3 and 1.5. Reduce or stand aside when fresh, material news conflicts with "
    "the trade direction, when the rule's recent record is weak, or when the market is disorderly "
    "(wide spread, big basis to the stock). Otherwise let the rule trade: do not second-guess it "
    "on noise. Keep reason under 25 words."
)


def next_open(now=None):
    now = now or dt.datetime.now(NY)
    d = now.date()
    while True:
        op = dt.datetime.combine(d, dt.time(9, 30), tzinfo=NY)
        # keep today until the outcome has had time to be recorded
        if bt.is_session(d) and op + dt.timedelta(minutes=bt.WIN_MIN + 10) > now:
            return op
        d += dt.timedelta(days=1)


class Engine:
    def __init__(self):
        self.samples, self.hl = [], DEFAULT_HL
        self.rules = bt.rule_book()
        self.ranking, self.rule = [], None
        self.day = None                 # NY date of the plan in progress
        self.fetch = None
        self.dirty = set()
        self.last_ai = 0.0
        self.ai = None                  # latest supervisor decision
        self.plan = {"phase": "warming up"}
        self._busy = False

    # -------------------------------------------------------------- ranking
    def load(self, bt_out):
        """Seed from the backtest: its samples and the half-life chosen on train."""
        if not bt_out or not bt_out.get("samples"):
            return
        self.samples = [dict(s) for s in bt_out["samples"] if s.get("r_rem") is not None]
        ad = bt_out.get("adaptive") or {}
        if "halfLife" in ad:
            self.hl = ad["halfLife"]
        self.rerank()

    def rerank(self):
        if not self.samples:
            self.ranking, self.rule = [], None; return
        _, pnl = adaptive.matrix(self.samples, self.rules, bt.FEE_PCT)
        self.ranking = adaptive.rank(pnl, len(self.samples), self.hl, self.rules)
        self.rule = adaptive.pick(self.ranking)

    def poke(self, reason):
        self.dirty.add(reason)

    # -------------------------------------------------------------- data
    async def _features(self, client, day, partial):
        if self.fetch is None:
            _, _, self.fetch = await bt.resolve_source(client)
            if self.fetch is None:
                return None
        raw = await bt.load_days(self.fetch, bt.sessions_through(day, 3))
        rows = bt.build_samples(raw, bt.open_news_log(), partial=partial)
        return rows[-1] if rows and rows[-1]["day"] == day.isoformat() else None

    # -------------------------------------------------------------- the day
    async def tick(self, client, state, now=None):
        """Called every ~1s. Cheap unless a scheduled step is due."""
        if self._busy:
            return
        now = now or dt.datetime.now(NY)
        op = next_open(now)
        decide_at = op + dt.timedelta(minutes=bt.SIG_MIN, seconds=5)
        exit_at = op + dt.timedelta(minutes=bt.WIN_MIN, seconds=30)
        p = self.plan
        if p.get("day") != op.date().isoformat():    # new session: fresh plan
            self.day, self.ai = op.date(), None
            self.plan = p = {"day": op.date().isoformat(), "phase": "waiting"}
        p.update({"openEAT": op.astimezone(bt.EAT).strftime("%a %d %b %H:%M"),
                  "openInSec": int((op - now).total_seconds()),
                  "halfLife": self.hl, "ranking": self.ranking[:3],
                  "rule": self.rule["name"] if self.rule else None,
                  "ai": self.ai, "aiEnabled": bool(os.getenv("ANTHROPIC_API_KEY"))})
        try:
            self._busy = True
            if now < op:
                p["phase"] = "waiting"
            elif now < decide_at:
                p["phase"] = "watching first minutes"
            elif "dir" not in p:
                await self._decide(client, now)
            elif now < exit_at:
                self._guard(state)
            elif not p.get("resolved"):
                await self._resolve(client)
            else:
                p["phase"] = "done"
        finally:
            self._busy = False

    async def _decide(self, client, now):
        p = self.plan
        s = await self._features(client, self.day, partial=True)
        if not s:
            if (now - dt.datetime.combine(self.day, dt.time(9, 30), tzinfo=NY)).total_seconds() > 300:
                p.update({"phase": "no data", "dir": 0})
            return
        rule = self.rule
        d = next((fn(s) for _, name, fn in self.rules if rule and name == rule["name"]), 0)
        size, why = 1.0, "rule call"
        if not rule:
            why = "no rule is convincingly positive: stand aside"
        ai = self.ai or {}
        if d and ai.get("action") == "stand_aside":
            d, why = 0, "AI: " + ai.get("reason", "stand aside")
        elif d and ai.get("action") == "reduce":
            size, why = ai["size"], "AI: " + ai.get("reason", "reduced")
        p.update({"phase": "in trade" if d else "standing aside", "dir": d, "size": round(size, 2),
                  "entry": s["entry"], "features": {"overnightPct": round(s["overnight"] * 100, 2),
                                                    "prevDayPct": round(s["prevDay"] * 100, 2),
                                                    "first2mPct": round(s["r_sig"] * 100, 3),
                                                    "event": s["event"], "news": s["newsNet"]},
                  "why": why, "stopPct": (ai.get("stopPct") or STOP_PCT), "decidedAt": _iso()})
        signal_log.log_signal("open_plan", {k: p.get(k) for k in ("day", "rule", "dir", "size", "entry",
                                                                  "features", "why", "stopPct", "halfLife")},
                              fingerprint=p["day"])
        self.poke("decision")

    def _guard(self, state):
        """Tick-level risk check while in the trade: stop, and AI exit requests."""
        p = self.plan
        if not p.get("dir") or p.get("exited"):
            return
        c = state.get("crypto") or {}
        mark = c.get("mark") or c.get("last")
        if not mark or not p.get("entry"):
            return
        move = (mark / p["entry"] - 1) * 100 * p["dir"]
        p["livePct"] = round(move, 3)
        stop = (self.ai or {}).get("stopPct") or p.get("stopPct") or STOP_PCT
        p["stopPct"] = stop
        if move <= -stop:
            p.update({"exited": "stop", "exitPx": mark, "phase": "stopped out"})
        elif (self.ai or {}).get("action") == "exit":
            p.update({"exited": "ai", "exitPx": mark, "phase": "exited by AI"})
        elif move <= -0.6 * stop:
            self.poke("near stop")
        if p.get("exited"):
            signal_log.log_signal("open_exit", {"day": p["day"], "why": p["exited"], "pct": round(move, 3)},
                                  fingerprint=p["day"])

    async def _resolve(self, client):
        p = self.plan
        s = await self._features(client, self.day, partial=False)
        if not s:
            return
        p["resolved"] = True
        if p.get("dir"):
            if p.get("exited"):
                pct = (p["exitPx"] / p["entry"] - 1) * 100 * p["dir"]
            else:
                pct = s["r_rem"] * 100 * p["dir"]
            p["resultPct"] = round((pct - bt.FEE_PCT) * p.get("size", 1), 3)
        p["phase"] = "done"
        self.samples.append(bt._compact(s))           # the newest day now counts the most
        self.samples = self.samples[-400:]
        self.rerank()
        signal_log.log_outcome({"kind": "open", "day": p["day"], "rule": p.get("rule"), "dir": p.get("dir"),
                                "size": p.get("size"), "resultPct": p.get("resultPct"),
                                "exited": p.get("exited"), "moveAfterSignalPct": round(s["r_rem"] * 100, 3)})

    # -------------------------------------------------------------- AI supervisor
    def _sup_due(self, now=None):
        now = now or dt.datetime.now(NY)
        op = next_open(now)
        if not (op - dt.timedelta(minutes=30) <= now <= op + dt.timedelta(minutes=bt.WIN_MIN)):
            return False
        gap = time.time() - self.last_ai
        return (self.dirty and gap >= SUP_MIN_GAP) or gap >= SUP_EVERY

    def context(self, state):
        """Everything the supervisor sees, as compact JSON-able data."""
        p, c, sig = self.plan, state.get("crypto") or {}, state.get("signal") or {}
        now_ms = int(time.time() * 1000)
        fresh = [n for n in state.get("news") or [] if n.get("kind", "news") == "news"
                 and now_ms - (n.get("ts") or n.get("seen") or 0) < 18 * 3.6e6][:12]
        return {
            "plan": {k: p.get(k) for k in ("phase", "rule", "dir", "size", "entry", "livePct", "stopPct",
                                           "features", "openInSec")},
            "ruleRanking": self.ranking[:3], "halfLifeSessions": self.hl,
            "lastOutcomes": [{"day": s["day"], "moveAfterSignalPct": round(s["r_rem"] * 100, 3),
                              "overnightPct": round(s["overnight"] * 100, 2)} for s in self.samples[-5:]],
            "news": [{"headline": n["headline"], "dir": n.get("dir"), "outlets": n.get("sources", 1),
                      "ageMin": int((now_ms - (n.get("ts") or n.get("seen") or now_ms)) / 60000)} for n in fresh],
            "newsScore": newsfeed.score(fresh, now_ms),
            "market": {"mark": c.get("mark"), "spreadPct": c.get("spreadPct"), "basisPct": sig.get("basis"),
                       "bookImbalancePct": c.get("imbalance"), "tapeBuyPct": c.get("buyPct"),
                       "moves": [(t.get("tf"), t.get("note")) for t in (sig.get("timeframes") or [])]},
        }

    async def supervise(self, ai_client, state, now=None):
        if ai_client is None or not self._sup_due(now):
            return
        reasons = sorted(self.dirty) or ["scheduled"]
        self.dirty.clear()
        self.last_ai = time.time()
        ctx = self.context(state)
        try:
            resp = await ai_client.beta.messages.create(
                model=SUP_MODEL, max_tokens=2000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SUP_SCHEMA}},
                system=SUP_SYSTEM,
                messages=[{"role": "user", "content": "Wake reason: " + ", ".join(reasons)
                           + "\nState:\n" + json.dumps(ctx, default=str)}])
        except Exception as e:
            self.ai = {"action": "go", "size": 1.0, "stopPct": STOP_PCT,
                       "reason": f"supervisor unavailable ({type(e).__name__}); rule runs alone", "at": _iso()}
            return
        if resp.stop_reason == "refusal":
            return
        text = next((b.text for b in resp.content if b.type == "text"), "")
        self.ai = clamp(text, self.plan)
        self.ai["at"], self.ai["wake"] = _iso(), reasons
        signal_log.log_signal("ai_supervisor", {"day": self.plan.get("day"), **self.ai},
                              fingerprint=json.dumps([self.ai["action"], self.ai["size"], self.ai["stopPct"]]))


def clamp(text, plan):
    """Parse and bound the supervisor's answer. It may only shrink risk."""
    try:
        d = json.loads(text)
    except Exception:
        return {"action": "go", "size": 1.0, "stopPct": STOP_PCT, "reason": "unparseable answer ignored"}
    action = d.get("action") if d.get("action") in ("go", "reduce", "stand_aside", "exit") else "go"
    if action == "exit" and not plan.get("dir"):
        action = "stand_aside"
    if action == "stand_aside" and plan.get("dir") and "decidedAt" in plan:
        action = "exit"                       # already in: standing aside means getting out
    try:
        size = min(1.0, max(0.0, float(d.get("size", 1.0))))
    except (TypeError, ValueError):
        size = 1.0
    if action == "go":
        size = 1.0
    try:
        stop = min(STOP_BOUNDS[1], max(STOP_BOUNDS[0], float(d.get("stopPct", STOP_PCT))))
    except (TypeError, ValueError):
        stop = STOP_PCT
    return {"action": action, "size": round(size, 2), "stopPct": round(stop, 2),
            "reason": str(d.get("reason", ""))[:200]}


def _iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
