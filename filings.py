"""
Who else is trading PLTR — US politicians and company insiders.
===============================================================

Two free, public disclosure streams:

  1. CONGRESS (STOCK Act).  Members of the House and Senate must file a
     Periodic Transaction Report within 45 days of a trade.  The raw filings are
     PDFs, but the community "stock watcher" projects parse them into JSON and
     republish on public S3 buckets, which is what we read.  Refreshed a few
     times a day upstream, so we poll slowly.

  2. INSIDERS (SEC Form 4).  Officers, directors and 10%+ holders must file
     within 2 business days.  This one IS near-real-time.  We read PLTR's own
     EDGAR submissions index (issuer CIK 0001321655), then pull each Form 4's
     XML for the transaction code, share count and price.

SEC requires a descriptive User-Agent with contact info on every request.
Everything degrades to an empty list rather than raising.
"""
import asyncio, json, re, datetime as dt
import xml.etree.ElementTree as ET

TICKER = "PLTR"
CIK = "0001321655"                       # Palantir Technologies Inc. (issuer)
SEC_UA = {"User-Agent": "PLTR-Signal-Desk/1.0 (contact: dashboard@example.com)",
          "Accept-Encoding": "gzip, deflate", "Host": None}

# Congress trade mirrors.  Tried in order; the first that returns wins.
CONGRESS_SOURCES = [
    ("House", ["https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
               "https://house-stock-watcher-data.s3.us-west-2.amazonaws.com/data/all_transactions.json"]),
    ("Senate", ["https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
                "https://senate-stock-watcher-data.s3.us-west-2.amazonaws.com/aggregate/all_transactions.json"]),
]

# transaction codes worth showing on a trading desk
CODE_LABEL = {
    "P": "Open-market buy", "S": "Open-market sell", "A": "Grant / award",
    "M": "Option exercise", "F": "Tax withholding", "G": "Gift",
    "D": "Disposition to issuer", "C": "Conversion", "X": "Option exercise",
}


def _hdr(url):
    h = dict(SEC_UA)
    h.pop("Host", None)
    return h


async def _get(client, url, timeout=25, stream_limit=None):
    try:
        r = await client.get(url, headers=_hdr(url), timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        return None


# ------------------------------------------------------------------ congress

_REC = re.compile(r"\{[^{}]*?\}")


def _scan_records(text, ticker=TICKER):
    """The all_transactions files are ~30-60MB of FLAT objects.  Rather than
    json.loads the whole thing (slow + memory-hungry on a small dyno), pull out
    only the objects that mention the ticker and parse those."""
    out = []
    for m in _REC.finditer(text):
        blob = m.group(0)
        if f'"{ticker}"' not in blob:
            continue
        try:
            out.append(json.loads(blob))
        except Exception:
            continue
    return out


def _norm_congress(rec, chamber):
    who = (rec.get("representative") or rec.get("senator") or rec.get("member")
           or rec.get("name") or "").replace("Hon. ", "").strip()
    if (rec.get("ticker") or "").upper() != TICKER:
        return None
    typ = (rec.get("type") or "").lower()
    side = ("buy" if "purchase" in typ or typ.startswith("buy")
            else "sell" if "sale" in typ or typ.startswith("sell") else "other")
    return {
        "who": who or "Unknown",
        "role": chamber,
        "party": rec.get("party") or "",
        "state": rec.get("district") or rec.get("state") or "",
        "side": side,
        "typeRaw": rec.get("type") or "",
        "amount": rec.get("amount") or "",
        "txDate": _date(rec.get("transaction_date")),
        "filedDate": _date(rec.get("disclosure_date")),
        "url": rec.get("ptr_link") or "",
        "owner": rec.get("owner") or "",
        "kind": "congress",
    }


def _date(s):
    if not s:
        return ""
    s = str(s).strip()
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, f).strftime("%Y-%m-%d")
        except Exception:
            pass
    return s


async def _one_congress(client, chamber, urls):
    for u in urls:
        r = await _get(client, u, timeout=45)
        if not r:
            continue
        try:
            recs = _scan_records(r.text)
        except Exception:
            continue
        out = [x for x in (_norm_congress(rec, chamber) for rec in recs) if x]
        if out:
            return out
    return []


async def fetch_congress(client, limit=40):
    res = await asyncio.gather(*[_one_congress(client, c, u) for c, u in CONGRESS_SOURCES],
                               return_exceptions=True)
    rows = []
    for r in res:
        if isinstance(r, list):
            rows += r
    rows.sort(key=lambda x: (x.get("txDate") or "", x.get("filedDate") or ""), reverse=True)
    return rows[:limit]


# ------------------------------------------------------------------ insiders

def _t(el, *names):
    """Depth-first text lookup — Form 4 wraps most fields in <value>."""
    if el is None:
        return ""
    for n in names:
        for node in el.iter():
            if node.tag.split("}")[-1] == n:
                v = None
                for ch in node.iter():
                    if ch.tag.split("}")[-1] == "value" and (ch.text or "").strip():
                        v = ch.text.strip(); break
                return v if v is not None else (node.text or "").strip()
    return ""


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("$", ""))
    except Exception:
        return None


def _parse_form4(xml_text, url, filed):
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    owner, title = "", ""
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag == "reportingOwner":
            owner = _t(node, "rptOwnerName") or owner
            rel = None
            for ch in node.iter():
                if ch.tag.split("}")[-1] == "reportingOwnerRelationship":
                    rel = ch; break
            if rel is not None:
                bits = []
                if _t(rel, "officerTitle"):
                    bits.append(_t(rel, "officerTitle"))
                elif (_t(rel, "isDirector") or "") in ("1", "true"):
                    bits.append("Director")
                if (_t(rel, "isTenPercentOwner") or "") in ("1", "true"):
                    bits.append("10% owner")
                title = ", ".join(bits)
            break
    rows = []
    for node in root.iter():
        if node.tag.split("}")[-1] != "nonDerivativeTransaction":
            continue
        code = _t(node, "transactionCode")
        shares = _num(_t(node, "transactionShares"))
        px = _num(_t(node, "transactionPricePerShare"))
        ad = _t(node, "transactionAcquiredDisposedCode")
        d = _t(node, "transactionDate")
        side = "buy" if ad == "A" else "sell" if ad == "D" else "other"
        if code in ("P",):
            side = "buy"
        elif code in ("S",):
            side = "sell"
        rows.append({
            "who": (owner or "").title(), "role": title or "Insider",
            "side": side, "code": code, "codeLabel": CODE_LABEL.get(code, code or ""),
            "shares": shares, "price": px,
            "value": round(shares * px) if (shares and px) else None,
            "txDate": _date(d), "filedDate": _date(filed), "url": url,
            "kind": "insider",
        })
    return rows


async def _form4_rows(client, acc_nodash, filed):
    """Locate and parse the ownership XML inside one Form 4 accession folder."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc_nodash}"
    idx = await _get(client, f"{base}/index.json", timeout=15)
    if not idx:
        return []
    try:
        items = idx.json()["directory"]["item"]
    except Exception:
        return []
    name = None
    for it in items:
        n = it.get("name", "")
        if n.lower().endswith(".xml") and "index" not in n.lower():
            name = n
            break
    if not name:
        return []
    doc = await _get(client, f"{base}/{name}", timeout=15)
    if not doc:
        return []
    human = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc_nodash}/{name}"
    return _parse_form4(doc.text, human, filed)


async def fetch_insiders(client, limit=25, max_filings=12):
    r = await _get(client, f"https://data.sec.gov/submissions/CIK{CIK}.json", timeout=25)
    if not r:
        return []
    try:
        rec = r.json()["filings"]["recent"]
    except Exception:
        return []
    forms = rec.get("form", [])
    accs = rec.get("accessionNumber", [])
    dates = rec.get("filingDate", [])
    picks = [(accs[i].replace("-", ""), dates[i])
             for i in range(min(len(forms), len(accs), len(dates)))
             if forms[i] in ("4", "4/A")][:max_filings]
    res = await asyncio.gather(*[_form4_rows(client, a, d) for a, d in picks],
                               return_exceptions=True)
    rows = []
    for x in res:
        if isinstance(x, list):
            rows += x
    rows.sort(key=lambda x: (x.get("txDate") or "", x.get("filedDate") or ""), reverse=True)
    return rows[:limit]


# ------------------------------------------------------------------ combined

def _summarise(congress, insiders):
    def tally(rows):
        b = sum(1 for r in rows if r.get("side") == "buy")
        s = sum(1 for r in rows if r.get("side") == "sell")
        return {"buys": b, "sells": s, "net": b - s}
    ins_open = [r for r in insiders if r.get("code") in ("P", "S")]
    val_b = sum(r["value"] or 0 for r in ins_open if r["side"] == "buy")
    val_s = sum(r["value"] or 0 for r in ins_open if r["side"] == "sell")
    return {
        "congress": tally(congress),
        "insiders": tally(ins_open),
        "insiderBuyUsd": val_b,
        "insiderSellUsd": val_s,
        "people": len({r["who"] for r in congress}),
        "note": ("Congress files within 45 days of a trade, so those dates lag. "
                 "Form 4 insider filings land within 2 business days."),
    }


async def fetch_all(client):
    c, i = await asyncio.gather(fetch_congress(client), fetch_insiders(client),
                               return_exceptions=True)
    c = c if isinstance(c, list) else []
    i = i if isinstance(i, list) else []
    return {
        "ok": bool(c or i),
        "congress": c,
        "insiders": i,
        "summary": _summarise(c, i),
        "asOf": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
