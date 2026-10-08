"""
Offline checks for news collection and scoring. No network.

    python tests/test_news.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import newsfeed as nf

H = 3_600_000


def test_keywords_match_whole_words_only():
    assert nf.keyword_dir("Palantir wins $480M Army contract") == "up"
    assert nf.keyword_dir("Analyst downgrades Palantir, shares slide") == "down"
    # used to score as bullish/bearish through substrings: against->gain, window->win, execute->cut, shortly->short
    assert nf.keyword_dir("Palantir trades against a busy window, will execute shortly") == "flat"
    assert nf.keyword_dir("Should you buy Palantir stock?") == "flat"


def test_same_story_folds_rewording_not_different_news():
    assert nf.same_story("Palantir wins $480 million Army contract", "Palantir Technologies wins $480M US Army contract")
    assert not nf.same_story("Palantir shares fall as CEO sells stock", "Palantir shares rise on new NATO deal")
    assert not nf.same_story("Palantir wins Army contract", "Palantir misses earnings")


def test_merge_counts_outlets_and_keeps_earliest_time():
    m = nf.merge([
        {"headline": "Palantir wins $480 million Army contract", "src": "Reuters", "ts": 5 * H, "kind": "news"},
        {"headline": "Palantir Technologies wins $480M US Army contract", "src": "Yahoo", "ts": 4 * H, "kind": "news"},
    ], now_ms=6 * H)
    assert len(m) == 1 and m[0]["sources"] == 2 and m[0]["ts"] == 4 * H


def test_social_cannot_crowd_out_news():
    social = [{"headline": f"PLTR to the moon number {i} lets go", "src": "StockTwits", "ts": 10 * H + i, "kind": "social"}
              for i in range(40)]
    news = [{"headline": "Palantir raises full-year revenue guidance", "src": "Nasdaq", "ts": 1 * H, "kind": "news"}]
    m = nf.merge(social + news, now_ms=11 * H)
    assert any(x["kind"] == "news" for x in m), "real headline was pushed out by chatter"
    assert sum(1 for x in m if x["kind"] == "social") <= 10


def test_score_decays_ignores_social_and_weights_outlets():
    now = 100 * H
    fresh = {"headline": "a", "dir": "up", "ts": now, "kind": "news", "sources": 1}
    old = {"headline": "b", "dir": "down", "ts": now - 6 * H, "kind": "news", "sources": 1}
    chat = {"headline": "c", "dir": "down", "ts": now, "kind": "social"}
    assert nf.score([fresh, old, chat], now) == 0.5          # +1 fresh, -0.5 six hours old, social ignored
    wide = dict(fresh, sources=4)
    assert nf.score([wide], now) == 2.0                        # four outlets: sqrt(4)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, "-", e)
    sys.exit(1 if fails else 0)


def test_direction_reads_real_headlines_about_palantir_only():
    # real headlines from 8 Oct 2026; the old rules got the first six wrong
    cases = [("Palantir Stock Rises as Goldman Turns Buy With $230 Target", "up"),
             ("Palantir Stocks Rise With Ives's 2027 AI Endorsement", "up"),
             ("Why Is Palantir Stock Rising Today While Tech Stocks Fall? (October 8)", "up"),
             ("Premarket movers: Palantir gains on bullish call, NXP tumbles on downgrade", "up"),
             ("Palantir has been on a tear. Goldman Sachs sees more momentum ahead", "up"),
             ("Palantir in focus as Goldman Sachs upgrades on recent underperformance (PLTR:NASDAQ)", "up"),
             ("Palantir falls after Morgan Stanley downgrade", "down"),
             ("Palantir misses revenue estimates, shares sink", "down"),
             ("What Is Going on With Palantir Tech Stock on Thursday?", "flat")]
    for h, want in cases:
        assert nf.keyword_dir(h) == want, h


def test_off_topic_wire_items_are_dropped():
    items = [{"headline": "Bronx Divorce Mediation Attorney Explains How to Divide a Business", "src": "Nasdaq", "ts": 2},
             {"headline": "PepsiCo Tops Q3 Estimates As International Growth Accelerates", "src": "Yahoo Finance", "ts": 3},
             {"headline": "Goldman Sachs upgrades Palantir to Buy", "src": "Yahoo Finance", "ts": 4},
             {"headline": "8-K current report", "src": "SEC EDGAR", "ts": 1},
             {"headline": "$PLTR to the moon", "src": "StockTwits @x", "ts": 5, "kind": "social"}]
    heads = [m["headline"] for m in nf.merge(items, now_ms=10)]
    assert heads == ["Goldman Sachs upgrades Palantir to Buy", "8-K current report", "$PLTR to the moon"]
