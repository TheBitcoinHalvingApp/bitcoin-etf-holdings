#!/usr/bin/env python3
"""
backfill_edgar.py

Quarter-end (and fiscal-year-end) bitcoin held by each US spot bitcoin ETF
since 2024-01-01, taken only from the trusts' own SEC filings via the keyless
EDGAR APIs. No third-party trackers.

Sources, in order of preference for each fund and period end:
  1. XBRL "company facts" API (data.sec.gov/api/xbrl/companyfacts). Only some
     trusts tag the bitcoin count (Grayscale GBTC and BTC, ARK 21Shares, and
     Fidelity's 10-K year ends).
  2. The primary 10-Q / 10-K document itself: the Schedule of Investments
     table is parsed for the bitcoin quantity (and the fair value when XBRL
     does not carry it). Every fund's documents are parsed; where an XBRL
     value also exists the two are compared.

Output: history_quarterly.csv next to this script, columns
  date, ticker, btc, shares_outstanding, fair_value_usd, form, accession, method

Usage: python3 backfill_edgar.py [--since 2024-01-01] [--cache DIR]
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(HERE, "history_quarterly.csv")

UA = {
    "User-Agent": os.environ.get("SEC_USER_AGENT") or "Bitcoin Halvening ETF bot admin@example.com",  # PIPE-282 (GH-14): set the SEC_USER_AGENT repository secret to a name and a real contact
    "Accept-Encoding": "gzip, deflate",
}
PAUSE = 0.3  # seconds between requests; SEC asks for under 10/s, we stay well under 5/s

FUNDS = [
    # ticker, name, known CIK (None = look up in company_tickers.json)
    ("IBIT", "iShares Bitcoin Trust ETF", None),
    ("FBTC", "Fidelity Wise Origin Bitcoin Fund", None),
    ("GBTC", "Grayscale Bitcoin Trust ETF", 1588489),
    ("BTC", "Grayscale Bitcoin Mini Trust ETF", 2015034),
    ("ARKB", "ARK 21Shares Bitcoin ETF", None),
    ("BITB", "Bitwise Bitcoin ETF", None),
    ("HODL", "VanEck Bitcoin ETF", None),
    ("BRRR", "CoinShares Bitcoin ETF", None),  # formerly Valkyrie Bitcoin Fund
    ("EZBC", "Franklin Bitcoin ETF", None),  # registrant: Franklin Templeton Digital Holdings Trust
    ("BTCO", "Invesco Galaxy Bitcoin ETF", None),
    ("BTCW", "WisdomTree Bitcoin Fund", 1850391),
    ("MSBT", "Morgan Stanley Bitcoin Trust", 2103612),  # launched 2026-04-08
]

FORMS = ("10-Q", "10-K", "10-Q/A", "10-K/A")

# XBRL tags that may hold a bitcoin count, with units that mean "bitcoin".
BTC_COUNT_TAGS = ["InvestmentOwnedBalanceContracts", "InvestmentOwnedBalanceShares"]
BTC_COUNT_UNITS = {"number", "bitcoin", "btc", "pure", "shares"}
FAIR_VALUE_TAGS = [
    "InvestmentOwnedAtFairValue",
    "CryptoAssetFairValue",
    "InvestmentsFairValueDisclosure",
    "InvestmentInPhysicalCommoditiesFairValueDisclosure",
]
SHARES_TAGS = [
    "SharesOutstanding",
    "CommonStockSharesOutstanding",
    "TemporaryEquitySharesOutstanding",
    "CommonStockOtherSharesOutstanding",
    "CommonStockSharesIssued",
    "TemporaryEquitySharesIssued",
    "SharesIssued",
]
NAV_TAGS = ["NetAssetValuePerShare"]

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
DATE_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})\b")

# Quantity of bitcoin as it appears in a Schedule of Investments line, e.g.
#   "Bitcoin 734,261 $ 61,003,144,500 $ 43,395,920,710"          (IBIT, BTCO)
#   "Bitcoin (a) 2,310 $ 136,506,043"                             (BTCW)
#   "Investment in bitcoin^ 36,207.6919 $ 1,891,192 $ 2,125,990"  (BITB)
#   "Investment in bitcoin 5,864 $ 442,207,125 $ 342,431,656"     (BRRR, EZBC, HODL 2024)
#   "Global Bitcoin 144,704.48094252 $ 7,587,410 $ 10,215,701"    (FBTC)
#   "Bitcoin 16,241.00 $ 1,291,666,946 $ 958,827,545"             (HODL)
QTY_RE = re.compile(
    r"(?:Investment in bitcoin|Global Bitcoin|Bitcoin)"
    r"(?:\s*\([a-z]\))?\s*\^?\s*,?\s*(?:at Fair Value\s*)?"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*\$\s*\(?\s*(\d{1,3}(?:,\d{3})*)"
    r"(?:\s*\)?\s*\$\s*(\d{1,3}(?:,\d{3})*))?",
    re.I,
)
SOI_RE = re.compile(r"Schedules?\s+of\s+Investments?", re.I)


class Edgar:
    def __init__(self, cache_dir):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.cache = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._last = 0.0

    def get(self, url, binary=False):
        key = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[1])
        path = os.path.join(self.cache, key)
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        else:
            wait = PAUSE - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            for attempt in range(4):
                r = self.s.get(url, timeout=90)
                self._last = time.time()
                if r.status_code == 200:
                    break
                if r.status_code in (403, 429, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
            r.raise_for_status()
            data = r.content
            with open(path, "wb") as f:
                f.write(data)
        return data if binary else data.decode("utf-8", "replace")

    def json(self, url):
        return json.loads(self.get(url))


def find_ciks(ed, funds):
    """Resolve CIKs from the SEC ticker files; fall back to full-text search."""
    tickers = {}
    for url in ("https://www.sec.gov/files/company_tickers.json",
                "https://www.sec.gov/files/company_tickers_exchange.json"):
        try:
            d = ed.json(url)
        except Exception as e:  # noqa
            print(f"  warn: {url}: {e}", file=sys.stderr)
            continue
        if isinstance(d, dict) and "data" in d:  # company_tickers_exchange layout
            fields = d["fields"]
            ci, ti, ni = fields.index("cik"), fields.index("ticker"), fields.index("name")
            for row in d["data"]:
                tickers.setdefault(str(row[ti]).upper(), (int(row[ci]), row[ni]))
        else:
            for row in d.values():
                tickers.setdefault(str(row["ticker"]).upper(), (int(row["cik_str"]), row["title"]))
    out = {}
    for ticker, name, cik in funds:
        if cik is None and ticker in tickers:
            cik, title = tickers[ticker]
        elif cik is None:
            q = requests.utils.quote(f'"{name}"')
            d = ed.json(f"https://efts.sec.gov/LATEST/search-index?q={q}&forms=10-Q")
            hits = d.get("hits", {}).get("hits", [])
            if not hits:
                raise SystemExit(f"could not find CIK for {ticker} ({name})")
            cik = int(hits[0]["_source"]["ciks"][0])
            title = hits[0]["_source"]["display_names"][0]
        else:
            title = tickers.get(ticker, (cik, name))[1]
        out[ticker] = (cik, title)
    return out


def is_quarter_end(d):
    return (d.month, d.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}


def latest_by_end(entries, since):
    """entries: list of XBRL fact dicts. Keep one per end date, latest filed wins."""
    best = {}
    for e in entries:
        if "end" not in e or e.get("start"):  # instants only
            continue
        end = date.fromisoformat(e["end"])
        if end < since or not is_quarter_end(end):
            continue
        if e.get("form") not in FORMS:
            continue
        cur = best.get(end)
        if cur is None or (e["filed"], e["accn"]) > (cur["filed"], cur["accn"]):
            best[end] = e
    return best


def xbrl_facts(ed, cik):
    d = ed.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    return d.get("facts", {})


def pick_xbrl(facts, tags, units_ok, since, all_filings=False):
    """Return {end_date: (value, tag, unit, form, accn, filed)}, latest filing wins.

    With all_filings=True return {end_date: {accn: value}} instead (every filing's
    value for that period end), used to pair facts from the same filing.
    """
    found = {}
    for ns, group in facts.items():
        for tag in tags:
            if tag not in group:
                continue
            for unit, entries in group[tag]["units"].items():
                if units_ok is not None and unit.lower() not in units_ok:
                    continue
                if all_filings:
                    for e in entries:
                        if e.get("start") or "end" not in e or e.get("form") not in FORMS:
                            continue
                        end = date.fromisoformat(e["end"])
                        if end >= since and is_quarter_end(end):
                            found.setdefault(end, {}).setdefault(e["accn"], e["val"])
                    continue
                for end, e in latest_by_end(entries, since).items():
                    cur = found.get(end)
                    if cur is None or (e["filed"], e["accn"]) > (cur[5], cur[4]):
                        found[end] = (e["val"], f"{ns}:{tag}[{unit}]", unit, e["form"], e["accn"], e["filed"])
    return found


def scan_custom_tags(facts):
    """Any tag outside us-gaap/dei, or any tag name hinting at bitcoin quantities."""
    hits = []
    for ns, group in facts.items():
        for tag, v in group.items():
            if ns not in ("us-gaap", "dei") or re.search(r"bitcoin|digital|quantity", tag, re.I):
                hits.append(f"{ns}:{tag}{list(v['units'])}")
    return hits


def filings(ed, cik):
    d = ed.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    rows = []

    def add(block):
        for i in range(len(block["form"])):
            if block["form"][i] in FORMS:
                rows.append({
                    "form": block["form"][i],
                    "accn": block["accessionNumber"][i],
                    "filed": block["filingDate"][i],
                    "report": block["reportDate"][i],
                    "doc": block["primaryDocument"][i],
                })
    add(d["filings"]["recent"])
    for extra in d["filings"].get("files", []):
        add(ed.json("https://data.sec.gov/submissions/" + extra["name"]))
    return sorted(rows, key=lambda r: (r["filed"], r["accn"]))


def doc_text(ed, cik, accn, doc):
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{doc}"
    html = ed.get(url, binary=True)
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text(" ")
    text = text.replace("\xa0", " ").replace("\u200b", "")  # MSBT pads its tables with zero-width spaces
    return re.sub(r"\s+", " ", text), url


def parse_schedules(text):
    """Find bitcoin quantities in Schedule of Investments tables.

    Returns {date: {"btc": float, "fair_value": int|None, "snippet": str}}.
    Each schedule header is followed by one or more "Month D, YYYY" sub-headers
    (current period and comparative); the quantity line follows each date.
    """
    out = {}
    for m in SOI_RE.finditer(text):
        window = text[m.start(): m.start() + 2500]
        # "Amounts in 000's" / "in thousands" scale the dollar columns, never the quantity
        scale = 1000 if re.search(r"in (?:000|thousands)", window[:600], re.I) else 1
        dates = list(DATE_RE.finditer(window))
        for i, dm in enumerate(dates):
            seg_end = dates[i + 1].start() if i + 1 < len(dates) else len(window)
            seg = window[dm.end(): seg_end]
            q = QTY_RE.search(seg)
            if not q:
                continue
            d = date(int(dm.group(3)), MONTHS[dm.group(1)], int(dm.group(2)))
            btc = float(q.group(1).replace(",", ""))
            if btc <= 0:
                continue
            nums = [q.group(2), q.group(3)]
            nums = [int(n.replace(",", "")) * scale for n in nums if n]
            fv = nums[-1] if nums else None  # cost then fair value; lone number is the value
            if d not in out:
                out[d] = {"btc": btc, "fair_value": fv, "snippet": seg[:160]}
    return out


def parse_shares(text):
    """Shares outstanding for the current period, from the statement of assets and liabilities.

    Prefer the "Shares outstanding N ... Net asset value per share" line of the
    balance sheet (the first number is the current period), else the first
    match after the statement heading.
    """
    pat = re.compile(r"Shares (?:issued and )?outstanding[^0-9$]{0,80}?(\d{1,3}(?:,\d{3})+)", re.I)
    hits = list(pat.finditer(text))
    for m in hits:
        if re.search(r"net asset value per share", text[m.end(): m.end() + 200], re.I):
            return int(m.group(1).replace(",", ""))
    h = re.search(r"Statements? of Assets and Liabilities", text, re.I)
    start = h.start() if h else 0
    for m in hits:
        if m.start() >= start:
            return int(m.group(1).replace(",", ""))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--cache", default=os.path.join("/tmp", "edgar_cache"))
    ap.add_argument("--out", default=OUT_CSV)
    args = ap.parse_args()
    since = date.fromisoformat(args.since)
    ed = Edgar(args.cache)

    print("Resolving CIKs ...")
    ciks = find_ciks(ed, FUNDS)
    for t, (cik, title) in ciks.items():
        print(f"  {t:5s} CIK {cik:>10d}  {title}")

    rows = []
    notes = []
    methods_used = {}
    for ticker, name, _ in FUNDS:
        cik, title = ciks[ticker]
        print(f"\n== {ticker} ({title}, CIK {cik})")
        facts = xbrl_facts(ed, cik)
        custom = scan_custom_tags(facts)
        if custom:
            print(f"   non-standard / bitcoin-ish tags: {custom}")

        x_btc = pick_xbrl(facts, BTC_COUNT_TAGS, BTC_COUNT_UNITS, since)
        x_fv = pick_xbrl(facts, FAIR_VALUE_TAGS, {"usd"}, since)
        x_sh = pick_xbrl(facts, SHARES_TAGS, {"shares"}, since)
        x_nav = pick_xbrl(facts, NAV_TAGS, None, since)
        # For the NAV x shares check, pair NAV and shares from the same filing
        # (share splits, e.g. ARKB's 3-for-1 in 2025, restate NAV in later filings).
        nav_all = pick_xbrl(facts, NAV_TAGS, None, since, all_filings=True)
        sh_all = pick_xbrl(facts, SHARES_TAGS, {"shares"}, since, all_filings=True)
        print(f"   XBRL bitcoin-count periods: {len(x_btc)}  "
              f"tags: {sorted({v[1] for v in x_btc.values()})}")

        # Parse every 10-Q / 10-K document (latest filing wins per period end).
        doc_btc, doc_sh = {}, {}
        for f in filings(ed, cik):
            if f["report"] and date.fromisoformat(f["report"]) < since:
                continue
            try:
                text, url = doc_text(ed, cik, f["accn"], f["doc"])
            except Exception as e:  # noqa
                notes.append(f"{ticker}: could not fetch {f['accn']} {f['doc']}: {e}")
                continue
            sched = parse_schedules(text)
            if not sched:
                notes.append(f"{ticker}: no schedule of investments parsed in {f['form']} {f['accn']} ({f['doc']})")
            for d, v in sched.items():
                if d < since or not is_quarter_end(d):
                    continue
                v = dict(v, form=f["form"], accn=f["accn"], filed=f["filed"], url=url)
                doc_btc[d] = v  # filings sorted by date, so later filings overwrite
            if f["report"]:
                rd = date.fromisoformat(f["report"])
                sh = parse_shares(text)
                if sh and (rd not in doc_sh or f["filed"] >= doc_sh[rd][1]):
                    doc_sh[rd] = (sh, f["filed"])
        print(f"   document-parsed periods: {len(doc_btc)}")

        periods = sorted(set(x_btc) | set(doc_btc))
        for d in periods:
            xv = x_btc.get(d)
            dv = doc_btc.get(d)
            if xv and dv:
                xval, dval = float(xv[0]), dv["btc"]
                agree = abs(xval - dval) <= max(1.0, 0.0005 * xval)
                # Fidelity tags a rounded integer in the 10-K but prints more decimals in the schedule.
                if agree and len(str(dval).split(".")[-1]) > len(str(xval).split(".")[-1]) and dval != int(dval):
                    btc, form, accn = dval, dv["form"], dv["accn"]
                    method = f"doc:schedule-of-investments (xbrl {xv[1]} agrees, rounded)"
                else:
                    btc, form, accn = xval, xv[3], xv[4]
                    method = f"xbrl:{xv[1]}" + ("; doc agrees" if agree else f"; DOC DISAGREES ({dval})")
                if not agree:
                    notes.append(f"{ticker} {d}: XBRL {xval} vs document {dval}")
            elif xv:
                btc, form, accn, method = float(xv[0]), xv[3], xv[4], f"xbrl:{xv[1]}"
            else:
                btc, form, accn, method = dv["btc"], dv["form"], dv["accn"], "doc:schedule-of-investments"
            fv = x_fv[d][0] if d in x_fv else (dv["fair_value"] if dv else None)
            fv_src = "xbrl" if d in x_fv else ("doc" if dv and dv["fair_value"] else "")
            if d in x_sh:
                sh = x_sh[d][0]
            elif d in doc_sh:
                sh = doc_sh[d][0]
                method += "; shares:doc"
            else:
                sh = None
            nav_pair = None
            for accn_ in sorted(set(nav_all.get(d, {})) & set(sh_all.get(d, {})), reverse=True):
                nav_pair = (nav_all[d][accn_], sh_all[d][accn_])
                break
            rows.append({
                "date": d.isoformat(), "ticker": ticker, "btc": btc,
                "shares_outstanding": sh, "fair_value_usd": fv, "fv_src": fv_src,
                "nav": x_nav[d][0] if d in x_nav else None, "nav_pair": nav_pair,
                "form": form, "accession": accn, "method": method,
            })
            methods_used.setdefault(ticker, set()).add(method.split(";")[0])

    # Sanity checks: implied price per fund vs the cross-fund median at that date,
    # and NAV x shares vs fair value where both are tagged.
    by_date = {}
    for r in rows:
        if r["fair_value_usd"] and r["btc"]:
            by_date.setdefault(r["date"], []).append(r["fair_value_usd"] / r["btc"])
    flags = []
    for r in rows:
        r["flag"] = ""
        if not r["fair_value_usd"]:
            r["flag"] = "no fair value to check"
            continue
        implied = r["fair_value_usd"] / r["btc"]
        med = statistics.median(by_date[r["date"]])
        dev = implied / med - 1
        if abs(dev) > 0.05:
            r["flag"] = f"implied price ${implied:,.0f} is {dev:+.1%} vs cross-fund median ${med:,.0f}"
        if r["nav_pair"]:
            nav, sh = r["nav_pair"]
            nav_dev = nav * sh / r["fair_value_usd"] - 1
            if abs(nav_dev) > 0.05:
                r["flag"] += f"; NAV x shares off fair value by {nav_dev:+.1%}"
        if r["flag"]:
            flags.append(f"{r['ticker']} {r['date']}: {r['flag']}")

    rows.sort(key=lambda r: (r["date"], r["ticker"]))
    out_rows = [[r["date"], r["ticker"], f"{r['btc']:.8f}".rstrip("0").rstrip("."),
                 r["shares_outstanding"] or "", r["fair_value_usd"] or "",
                 r["form"], r["accession"], r["method"]] for r in rows]  # PIPE-282 (GH-17)
    # PIPE-282 (GH-17): a quarter row the previous file had and this run did not
    # produce (a filing document that failed to download) is kept as it was, so
    # one network error never removes a filing from the record.
    have = {(x[0], x[1]) for x in out_rows}
    if os.path.exists(args.out):
        with open(args.out, newline="") as f:
            for old in csv.reader(f):
                if len(old) == 8 and old[0] != "date" and (old[0], old[1]) not in have:
                    out_rows.append(old)
                    notes.append(f"{old[1]} {old[0]}: not produced by this run; the previous row is kept")
    out_rows.sort(key=lambda x: (x[0], x[1]))
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "btc", "shares_outstanding", "fair_value_usd", "form", "accession", "method"])
        w.writerows(out_rows)  # PIPE-282 (GH-17)
    print(f"\nWrote {len(out_rows)} rows to {args.out}")  # PIPE-282 (GH-17)

    print("\nPer-fund summary")
    print(f"{'fund':5s} {'periods':>7s}  {'first':10s} {'btc':>16s}  {'last':10s} {'btc':>16s}  methods")
    for ticker, _, _ in FUNDS:
        fr = [r for r in rows if r["ticker"] == ticker]
        if not fr:
            print(f"{ticker:5s} {0:7d}  (nothing found)")
            continue
        a, b = fr[0], fr[-1]
        print(f"{ticker:5s} {len(fr):7d}  {a['date']} {a['btc']:16,.4f}  {b['date']} {b['btc']:16,.4f}  "
              f"{'; '.join(sorted(methods_used[ticker]))}")

    # Gaps: quarter ends between a fund's first and last period with no row.
    all_q = sorted({date.fromisoformat(r["date"]) for r in rows})
    for ticker, _, _ in FUNDS:
        have = {date.fromisoformat(r["date"]) for r in rows if r["ticker"] == ticker}
        if not have:
            continue
        missing = [q for q in all_q if min(have) <= q <= max(have) and q not in have]
        if missing:
            notes.append(f"{ticker}: missing quarter ends {[m.isoformat() for m in missing]}")

    print("\nSanity flags:" if flags else "\nSanity flags: none")
    for x in flags:
        print("  " + x)
    print("\nNotes / doubts:" if notes else "\nNotes / doubts: none")
    for x in notes:
        print("  " + x)


if __name__ == "__main__":
    main()
