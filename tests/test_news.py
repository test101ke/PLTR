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
