#!/usr/bin/env python3
"""
etf_holdings.py - fetch the number of bitcoin held by each US spot Bitcoin ETF
from each issuer's OWN public page, file or API (no third-party trackers),
and write etf.json (plus etf_previous.json for day-over-day changes).

Dependencies: requests, beautifulsoup4 (pip install --break-system-packages
requests beautifulsoup4). Playwright + Chromium are loaded lazily and only
for issuers whose sites refuse plain HTTP clients (Invesco, Fidelity, and the
bot-walled Grayscale / WisdomTree attempts).
"""
import datetime as dt
import glob
import csv
import json
import os
import re
import shutil
import sys
import time
import traceback

try:
    import requests
except ImportError:  # pragma: no cover
    os.system(f"{sys.executable} -m pip install --break-system-packages -q requests beautifulsoup4")
    import requests
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    os.system(f"{sys.executable} -m pip install --break-system-packages -q beautifulsoup4")
    from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "etf.json")
PREV_JSON = os.path.join(HERE, "etf_previous.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
TIMEOUT = 30
PAUSE_BETWEEN_ISSUERS = 1.5

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def num(s):
    """'799,878.62850' -> 799878.6285"""
    return float(str(s).replace(",", "").replace("$", "").strip())


def iso_date(s, fmts=("%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%b-%d-%Y",
                      "%B %d, %Y", "%m/%d/%y")):
    s = (s or "").strip()
    for f in fmts:
        try:
            return dt.datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def get(url, **kw):
    kw.setdefault("timeout", TIMEOUT)
    r = SESSION.get(url, **kw)
    r.raise_for_status()
    return r


# --- lazy Playwright ---------------------------------------------------------
_PW = {"pw": None, "browser": None}


def _browser():
    """Launch Chromium once and reuse it for every issuer that needs a browser."""
    if _PW["browser"] is None:
        from playwright.sync_api import sync_playwright
        _PW["pw"] = sync_playwright().start()
        exe = None
        cands = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
        if cands:
            exe = cands[-1]
        # --disable-http2: Fidelity's edge answers GitHub's runners with
        # ERR_HTTP2_PROTOCOL_ERROR; over HTTP/1.1 the same page loads.
        kw = {"headless": True, "args": ["--disable-blink-features=AutomationControlled", "--disable-http2"]}
        if exe:
            kw["executable_path"] = exe
        _PW["browser"] = _PW["pw"].chromium.launch(**kw)
    return _PW["browser"]


def browser_page(url, wait_text=None, capture=None, wait_until="domcontentloaded",
                 settle=3, max_wait=45, ua=UA):
    """
    Load `url` in a fresh browser context. Returns (body_text, captured) where
    captured is a list of {url, body} for XHR/fetch responses whose URL contains
    `capture` (a substring). Waits until `wait_text` appears in the page text.
    """
    b = _browser()
    ctx_kw = {"locale": "en-US", "viewport": {"width": 1366, "height": 900}}
    if ua:  # some sites (Invesco/Akamai) reject a UA that disagrees with client hints
        ctx_kw["user_agent"] = ua
    ctx = b.new_context(**ctx_kw)
    page = ctx.new_page()
    captured = []

    def on_resp(r):
        try:
            if capture and capture in r.url:
                captured.append({"url": r.url, "status": r.status, "body": r.text()})
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        try:
            page.goto(url, wait_until=wait_until, timeout=TIMEOUT * 1000)
        except Exception as e:  # networkidle timeouts are common; keep going
            if "Timeout" not in str(e):
                raise
        deadline = time.time() + max_wait
        text = ""
        while time.time() < deadline:
            time.sleep(settle)
            try:
                text = page.inner_text("body")
            except Exception:
                text = ""
            if not wait_text or wait_text in text:
                break
        return text, captured
    finally:
        ctx.close()


def close_browser():
    try:
        if _PW["browser"]:
            _PW["browser"].close()
        if _PW["pw"]:
            _PW["pw"].stop()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# one function per fund: returns (btc_held, as_of, source_url, method)
# ----------------------------------------------------------------------------
def fetch_ibit():
    """BlackRock: official holdings CSV linked from the product page."""
    url = "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf/latest-holdings.csv"
    r = get(url, headers={"Accept": "text/csv,*/*"})
    txt = r.text
    m = re.search(r'Fund Holdings as of,"([^"]+)"', txt)
    as_of = iso_date(m.group(1)) if m else ""
    # row: "BTC","BITCOIN",...,"Quantity",...
    for line in txt.splitlines():
        if line.startswith('"BTC"'):
            cells = [c.strip('"') for c in line.split('","')]
            # header: Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,...
            qty = num(cells[7])
            return qty, as_of, url, "requests: iShares latest-holdings.csv, BTC row, Quantity column"
    raise RuntimeError("BTC row not found in iShares holdings CSV")


def fetch_fbtc():
    """Fidelity: quote dashboard shows 'Total bitcoin in fund' (needs a browser)."""
    url = "https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=FBTC"
    text, _ = browser_page(url, wait_text="Total bitcoin in fund", max_wait=75)
    m = re.search(r"Total bitcoin in fund\s*\n\s*As of\s+([A-Za-z]{3}-\d{2}-\d{4})\s*\n\s*([\d,]+\.?\d*)", text)
    if not m:
        # Say what Fidelity served instead, so the log explains a refusal.
        head = re.sub(r"\s+", " ", text)[:300]
        raise RuntimeError(f"'Total bitcoin in fund' not found on Fidelity dashboard; page began: {head!r}")
    return num(m.group(2)), iso_date(m.group(1)), url, \
        "playwright: Fidelity quote dashboard, 'Total bitcoin in fund' field"


def _grayscale(ticker):
    url = f"https://etfs.grayscale.com/{ticker.lower()}"
    # 1) plain request (Grayscale answers 429 + Vercel Security Checkpoint to non-browsers)
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        status, html = r.status_code, r.text
    except Exception as e:
        status, html = f"error {type(e).__name__}", ""
    if status == 200 and "Security Checkpoint" not in html:
        text = BeautifulSoup(html, "html.parser").get_text("\n")
    else:
        # 2) real browser
        text, _ = browser_page(url, wait_text="Bitcoin per Share", max_wait=40)
        if "Security Checkpoint" in text or "Failed to verify your browser" in text:
            raise RuntimeError(f"Grayscale blocked both requests (HTTP {status}) "
                               f"and headless Chromium with a Vercel Security Checkpoint")
    # Look for 'Bitcoin in Trust' / 'Total Bitcoin' style figure
    m = (re.search(r"(?:Bitcoin in Trust|Total Bitcoin(?: in Trust)?|Bitcoin Holdings)\s*\n?\s*([\d,]+\.?\d*)", text, re.I)
         or re.search(r"([\d,]{5,}\.?\d*)\s*BTC", text))
    if not m:
        raise RuntimeError("could not find bitcoin-in-trust figure on Grayscale page")
    d = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    return num(m.group(1)), iso_date(d.group(1)) if d else "", url, "grayscale product page"


def fetch_gbtc():
    return _grayscale("GBTC")


def fetch_btc_mini():
    return _grayscale("BTC")


def fetch_arkb():
    url = "https://assets.ark-funds.com/fund-documents/funds-etf-csv/ARK_21SHARES_BITCOIN_ETF_ARKB_HOLDINGS.csv"
    r = get(url, headers={"Accept": "text/csv,*/*"})
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(r.text)))
    for row in rows:
        if (row.get("ticker") or "").strip().upper() == "BTC":
            return num(row["shares"]), iso_date(row.get("date", "")), url, \
                "requests: ARK daily holdings CSV, ticker BTC, 'shares' column"
    raise RuntimeError("BTC row not found in ARKB CSV")


def fetch_bitb():
    url = "https://bitbetf.com/"
    r = get(url)
    text = BeautifulSoup(r.text, "html.parser").get_text("\n")
    # Fund Holdings table: "Bitcoin in Trust" 38,305.94, preceded by "Data as of 09/24/2026"
    m = re.search(r"Fund Holdings\s*\n\s*Data as of\s*\n?\s*(\d{2}/\d{2}/\d{4})[\s\S]{0,400}?Bitcoin in Trust\s*\n\s*([\d,]+\.?\d*)", text)
    if not m:
        raise RuntimeError("Fund Holdings / Bitcoin in Trust not found on bitbetf.com")
    btc, as_of = num(m.group(2)), iso_date(m.group(1))
    por = re.search(r"Trust Net Assets\s*\n\s*([\d,]+)\s*BTC", text)
    method = "requests: bitbetf.com 'Fund Holdings' table, 'Bitcoin in Trust'"
    if por:
        method += f" (Proof of Reserves 'Trust Net Assets' shows {por.group(1)} BTC)"
    return btc, as_of, url, method


def fetch_hodl():
    """VanEck: the holdings block on the page is filled by this JSON endpoint."""
    url = ("https://www.vaneck.com/Main/HoldingsBlock/GetContent/?blockid=348327&pageid=243755"
           "&ticker=HODL&reactlang=en&reactctr=us&epieditmode=false&latest=false&contextmode=Default")
    r = get(url, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                          "Referer": "https://www.vaneck.com/us/en/investments/bitcoin-etf-hodl/holdings/"})
    d = r.json()["data"]
    for h in d.get("Holdings", []):
        if (h.get("HoldingName") or "").lower() == "bitcoin":
            return num(h["Shares"]), iso_date(d.get("AsOfDate", "")), url, \
                "requests: VanEck HoldingsBlock JSON (feeds the HODL holdings page), Bitcoin row 'Shares'"
    raise RuntimeError("Bitcoin row not found in VanEck holdings JSON")


def fetch_brrr():
    """CoinShares: the product page's holdings widget is fed by www-api.coinshares.com."""
    url = ("https://www-api.coinshares.com/api/v2/Widgets?ApiKey=094DA478-140C-4E3E-B394-7A19BBE8326B"
           "&names=VALKYRIE_HOLDINGS_BRRR")
    r = get(url, headers={"Accept": "application/json", "Origin": "https://coinshares.com",
                          "Referer": "https://coinshares.com/us/etf/brrr/"})
    for widget in r.json():
        for sec in widget.get("sections", []):
            meta = {m["key"]: m["value"] for m in sec.get("meta", [])}
            if (meta.get("securityname") or "").upper() == "BITCOIN":
                return num(meta["shares"]), iso_date(meta.get("date", "")), url, \
                    "requests: CoinShares widgets API VALKYRIE_HOLDINGS_BRRR (feeds coinshares.com/us/etf/brrr), BITCOIN 'shares'"
    raise RuntimeError("BITCOIN row not found in CoinShares widget JSON")


def fetch_ezbc():
    """Franklin Templeton: GraphQL 'Holdings' call the product page makes."""
    url = "https://www.franklintempleton.com/api/pds/price-and-performance?op=Holdings"
    page = ("https://www.franklintempleton.com/investments/options/exchange-traded-funds/products/"
            "39639/SINGLCLASS/franklin-bitcoin-etf/EZBC")
    q = {"query": "query Holdings($productId: String!, $countryCode: String!, $languageCode: String!) {"
                  " Portfolio(fundid: $productId, countrycode: $countryCode, languagecode: $languageCode) {"
                  " fundname portfolio { dailyholdings { asofdatestd secname cusipnbr quantityshrpar mktvalue pctofnetassets } } } }",
         "variables": {"countryCode": "US", "languageCode": "en_US", "productId": "39639"},
         "operationName": "Holdings"}
    r = SESSION.post(url, json=q, timeout=TIMEOUT,
                     headers={"Accept": "application/json, text/plain, */*", "Referer": page})
    r.raise_for_status()
    for h in r.json()["data"]["Portfolio"]["portfolio"]["dailyholdings"]:
        if (h.get("secname") or "").upper() == "BITCOIN":
            return num(h["quantityshrpar"]), iso_date(h.get("asofdatestd", "")), url, \
                "requests: Franklin GraphQL Holdings (feeds EZBC product page), BITCOIN 'quantityshrpar'"
    raise RuntimeError("BITCOIN row not found in Franklin holdings")


def fetch_btco():
    """Invesco: Akamai returns 406 to non-browser clients; load the holdings page in
    Chromium and read the dng-api fundDetails JSON ('units') that fills 'Total units of crypto'."""
    url = "https://www.invesco.com/us/financial-products/etfs/holdings?audienceType=Investor&ticker=BTCO"
    text, caps = browser_page(url, wait_text="Total units of crypto",
                              capture="variationType=fundDetails", ua=None)
    for c in caps:
        try:
            d = json.loads(c["body"])
        except Exception:
            continue
        if d.get("units"):
            return float(d["units"]), iso_date(d.get("shareclassTotalNetAssetsEffectiveDate")
                                                or d.get("effectiveBusinessDate", "")), url, \
                "playwright: Invesco holdings page; dng-api fundDetails JSON 'units' (= 'Total units of crypto')"
    m = re.search(r"Total units of crypto\s*\n\s*([\d,]+\.?\d*)", text)
    if m:
        d = re.search(r"Market value \(as of (\d{2}/\d{2}/\d{4})\)", text)
        return num(m.group(1)), iso_date(d.group(1)) if d else "", url, \
            "playwright: Invesco holdings page text, 'Total units of crypto'"
    raise RuntimeError("'Total units of crypto' not found on Invesco page")


def fetch_btcw():
    url = "https://www.wisdomtree.com/investments/etfs/crypto/btcw"
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        status, html = r.status_code, r.text
    except Exception as e:
        status, html = f"error {type(e).__name__}", ""
    if status == 200 and "Sorry, you have been blocked" not in html:
        text = BeautifulSoup(html, "html.parser").get_text("\n")
    else:
        text, _ = browser_page(url, wait_text="Bitcoin", max_wait=45)
        if "security verification" in text.lower() or "you have been blocked" in text.lower():
            raise RuntimeError(f"WisdomTree blocked both requests (HTTP {status}) and headless "
                               f"Chromium with a Cloudflare managed challenge")
    m = (re.search(r"(?:Bitcoin(?: Held)?(?: in Trust)?|BTC(?: Held)?)\s*\n?\s*([\d,]{4,}\.?\d*)", text)
         or re.search(r"([\d,]{4,}\.?\d*)\s*BTC", text))
    if not m:
        raise RuntimeError("could not find bitcoin holdings figure on WisdomTree page")
    d = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    return num(m.group(1)), iso_date(d.group(1)) if d else "", url, "wisdomtree product page"


# --- SEC-anchored fallback for the three walled issuers ------------------------
# Grayscale and WisdomTree challenge automated readers from some addresses. When
# the direct read fails, the holdings are ESTIMATED from public filings, and the
# JSON says so ("estimated": true, with the basis):
#   bitcoin per share moves only by the daily sponsor fee between filings, so
#   BPS(today) = BPS(quarter end) x (1 - fee)^(days/365), exactly (checked against
#   two quarters of Grayscale filings: matches to five decimals);
#   holdings(today) = shares outstanding(today) x BPS(today).
# Shares outstanding: WisdomTree from Nasdaq's market cap / last price (matched
# the issuer's own figure to the share on 2026-09-28); Grayscale from the latest
# cover-page count in the trust's own 10-Q/10-K on EDGAR (quarterly, so the
# estimate drifts between filings; GBTC has been shedding 5 to 10% a quarter).
SEC_UA = "Bitcoin Halvening ETF bot admin@example.com"  # the SEC asks for a name and contact in the UA

def sec_facts(cik):
    r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                     headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["facts"]

def _latest(facts, ns, key):
    units = facts[ns][key]["units"]
    rows = units[list(units)[0]]
    best = max(rows, key=lambda r: r["end"])
    return float(best["val"]), best["end"]

def nasdaq_shares(ticker):
    """Shares outstanding implied by Nasdaq's market cap and last sale price."""
    h = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US"}
    base = f"https://api.nasdaq.com/api/quote/{ticker}/"
    summ = requests.get(base + "summary?assetclass=etf", headers=h, timeout=TIMEOUT).json()["data"]["summaryData"]
    info = requests.get(base + "info?assetclass=etf", headers=h, timeout=TIMEOUT).json()["data"]["primaryData"]
    cap = num(summ["MarketCap"]["value"])
    px = num(info["lastSalePrice"].replace("$", ""))
    if not cap or not px:
        raise RuntimeError(f"Nasdaq gave no market cap or price for {ticker}")
    return round(cap / px), info.get("lastTradeTimestamp", "")

def _decayed_bps(bps_anchor, anchor_date, fee_per_year):
    days = (dt.date.today() - dt.date.fromisoformat(anchor_date)).days
    return bps_anchor * (1.0 - fee_per_year) ** (days / 365.0)

def estimate_grayscale(ticker, cik, fee):
    f = sec_facts(cik)
    btc, q_end = _latest(f, "us-gaap", "InvestmentOwnedBalanceContracts")
    sh_q, q_end2 = _latest(f, "us-gaap", "SharesOutstanding")
    if q_end2 != q_end:
        raise RuntimeError("filing dates disagree")
    bps = _decayed_bps(btc / sh_q, q_end, fee)
    shares, cover_date = _latest(f, "dei", "EntityCommonStockSharesOutstanding")
    est = shares * bps
    method = (f"ESTIMATE from SEC filings: {btc:,.2f} BTC / {sh_q:,.0f} shares at {q_end} "
              f"(bitcoin per share, less the {fee*100:.2f}% fee accrued since) x {shares:,.0f} shares "
              f"outstanding on the {cover_date} 10-Q cover; Grayscale's site blocks automated readers")
    return est, cover_date, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", method

# WisdomTree's XBRL carries no bitcoin count; the 10-Q's schedule of investments
# does. Re-anchor these two numbers each quarter from the newest 10-Q or 10-K.
BTCW_ANCHOR = {"btc": 2310.0, "shares": 2_185_000, "date": "2026-06-30", "fee": 0.0025}
QUARTERLY_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_quarterly.csv")

def btcw_anchor():
    """The newest BTCW quarter end in history_quarterly.csv (written by backfill_edgar.py
    from the trust's own 10-Q/10-K), falling back to the typed constant."""
    a = dict(BTCW_ANCHOR)
    try:
        with open(QUARTERLY_CSV) as fh:
            rows = [r for r in csv.DictReader(fh) if r["ticker"] == "BTCW" and r.get("btc") and r.get("shares_outstanding")]
        if rows:
            r = max(rows, key=lambda r: r["date"])
            if r["date"] >= a["date"]:
                a.update({"btc": float(r["btc"]), "shares": int(float(r["shares_outstanding"])), "date": r["date"]})
    except Exception as e:
        print(f"   history_quarterly.csv not used for BTCW anchor: {e}", file=sys.stderr)
    return a

def estimate_btcw():
    a = btcw_anchor()
    bps = _decayed_bps(a["btc"] / a["shares"], a["date"], a["fee"])
    shares, stamp = nasdaq_shares("BTCW")
    est = shares * bps
    method = (f"ESTIMATE: {a['btc']:,.0f} BTC / {a['shares']:,} shares at {a['date']} (10-Q schedule of investments; "
              f"bitcoin per share less the {a['fee']*100:.2f}% fee since) x {shares:,} shares outstanding "
              f"(Nasdaq market cap / last price, {stamp}); WisdomTree's site blocks automated readers")
    return est, dt.date.today().isoformat(), "https://api.nasdaq.com/api/quote/BTCW/summary?assetclass=etf", method

# --- Morgan Stanley MSBT (launched 2026-04-08) --------------------------------
# Morgan Stanley's site answers data-center addresses with an Akamai "Access
# Denied" page, so the fund is anchored to its own 10-Q schedule of investments
# (history_quarterly.csv, written by backfill_edgar.py) and shares outstanding
# from Nasdaq, the same way as BTCW. The direct read is tried first in case the
# wall ever comes down.
MSBT_ANCHOR = {"btc": 5059.30771216, "shares": 17_650_000, "date": "2026-06-30", "fee": 0.0014}

def quarter_anchor(ticker, default):
    """The newest quarter end for `ticker` in history_quarterly.csv, else the typed constant."""
    a = dict(default)
    try:
        with open(QUARTERLY_CSV) as fh:
            rows = [r for r in csv.DictReader(fh) if r["ticker"] == ticker and r.get("btc") and r.get("shares_outstanding")]
        if rows:
            r = max(rows, key=lambda r: r["date"])
            if r["date"] >= a["date"]:
                a.update({"btc": float(r["btc"]), "shares": int(float(r["shares_outstanding"])), "date": r["date"]})
    except Exception as e:
        print(f"   history_quarterly.csv not used for {ticker} anchor: {e}", file=sys.stderr)
    return a

def fetch_msbt():
    url = "https://www.morganstanley.com/im/en-us/individual-investor/products/etfs/digital-assets/morgan-stanley-bitcoin-trust.html"
    text, _ = browser_page(url, wait_text="Bitcoin", max_wait=45)
    if "Access Denied" in text or "don't have permission" in text:
        raise RuntimeError("Morgan Stanley's site refused the request (Akamai Access Denied)")
    m = (re.search(r"(?:Total )?Bitcoin(?: Held)?(?: in Trust)?\s*\n?\s*([\d,]{4,}\.?\d*)", text)
         or re.search(r"([\d,]{4,}\.?\d*)\s*BTC", text))
    if not m:
        raise RuntimeError("could not find a bitcoin holdings figure on Morgan Stanley's page")
    d = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    return num(m.group(1)), iso_date(d.group(1)) if d else "", url, "morganstanley.com product page"

def estimate_msbt():
    a = quarter_anchor("MSBT", MSBT_ANCHOR)
    bps = _decayed_bps(a["btc"] / a["shares"], a["date"], a["fee"])
    shares, stamp = nasdaq_shares("MSBT")
    est = shares * bps
    method = (f"ESTIMATE: {a['btc']:,.2f} BTC / {a['shares']:,} shares at {a['date']} (10-Q schedule of investments; "
              f"bitcoin per share less the {a['fee']*100:.2f}% fee since) x {shares:,} shares outstanding "
              f"(Nasdaq market cap / last price, {stamp}); Morgan Stanley's site blocks automated readers")
    return est, dt.date.today().isoformat(), "https://api.nasdaq.com/api/quote/MSBT/summary?assetclass=etf", method

# --- Fidelity FBTC ------------------------------------------------------------
# Fidelity's quote dashboard renders an empty page for GitHub's runners (it
# reads fine from other addresses). Same anchor method as BTCW and MSBT, checked
# against a live read on 2026-10-01: estimate 182,308 vs 183,167 read (0.5% low,
# Nasdaq's rounded market cap).
FBTC_ANCHOR = {"btc": 174383.0, "shares": 200_328_476, "date": "2026-06-30", "fee": 0.0025}

def estimate_fbtc():
    a = quarter_anchor("FBTC", FBTC_ANCHOR)
    bps = _decayed_bps(a["btc"] / a["shares"], a["date"], a["fee"])
    shares, stamp = nasdaq_shares("FBTC")
    est = shares * bps
    method = (f"ESTIMATE: {a['btc']:,.0f} BTC / {a['shares']:,} shares at {a['date']} (10-Q schedule of investments; "
              f"bitcoin per share less the {a['fee']*100:.2f}% fee since) x {shares:,} shares outstanding "
              f"(Nasdaq market cap / last price, {stamp}); Fidelity's dashboard served an empty page")
    return est, dt.date.today().isoformat(), "https://api.nasdaq.com/api/quote/FBTC/summary?assetclass=etf", method

def with_fallback(direct, fallback):
    """Try the issuer's page; if it is walled, estimate from filings and flag it."""
    def run():
        try:
            return direct()
        except Exception as e:
            print(f"   direct read failed ({type(e).__name__}: {e}); estimating from filings", file=sys.stderr)
            btc, as_of, src, method = fallback()
            return btc, as_of, src, method, True
    return run


def fetch_defi():
    """Hashdex DEFI (optional). Its own page says the fund was liquidated."""
    url = "https://hashdex-etfs.com/defi"
    text, _ = browser_page(url, wait_text="Holdings", max_wait=30)
    if "has been liquidated" in text or "Closure of Hashdex Bitcoin ETF" in text:
        d = re.search(r"Holdings\s*\n\s*As of ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
        return 0.0, iso_date(d.group(1)) if d else "", url, \
            "playwright: hashdex-etfs.com states the fund has been liquidated and delisted; holdings recorded as 0"
    m = re.search(r"BTC\s*\n\s*BITCOIN\s*\n\s*([\d,]+\.?\d*)", text)
    if not m:
        raise RuntimeError("BTC row not found on Hashdex page")
    d = re.search(r"Holdings\s*\n\s*As of ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    return num(m.group(1)), iso_date(d.group(1)) if d else "", url, "playwright: hashdex-etfs.com holdings table"


# All twelve US spot bitcoin ETFs. Eight read the issuer's own file or page every
# morning. Grayscale GBTC and BTC and WisdomTree BTCW try the issuer's page first
# (some addresses pass their bot checks, some do not) and fall back to an estimate
# anchored to the trusts' own SEC filings, flagged "estimated". Hashdex DEFI was
# liquidated in August 2026.
FUNDS = [
    ("IBIT", "BlackRock iShares",     "iShares Bitcoin Trust ETF",            fetch_ibit),
    ("FBTC", "Fidelity",              "Fidelity Wise Origin Bitcoin Fund",    with_fallback(fetch_fbtc, estimate_fbtc)),
    ("ARKB", "ARK 21Shares",          "ARK 21Shares Bitcoin ETF",             fetch_arkb),
    ("BITB", "Bitwise",               "Bitwise Bitcoin ETF",                  fetch_bitb),
    ("HODL", "VanEck",                "VanEck Bitcoin ETF",                   fetch_hodl),
    ("BRRR", "CoinShares Valkyrie",   "CoinShares Bitcoin ETF",               fetch_brrr),
    ("EZBC", "Franklin Templeton",    "Franklin Bitcoin ETF",                 fetch_ezbc),
    ("BTCO", "Invesco Galaxy",        "Invesco Galaxy Bitcoin ETF",           fetch_btco),
    ("GBTC", "Grayscale",             "Grayscale Bitcoin Trust ETF",          with_fallback(fetch_gbtc, lambda: estimate_grayscale("GBTC", 1588489, 0.015))),
    ("BTC",  "Grayscale",             "Grayscale Bitcoin Mini Trust ETF",     with_fallback(fetch_btc_mini, lambda: estimate_grayscale("BTC", 2015034, 0.0015))),
    ("BTCW", "WisdomTree",            "WisdomTree Bitcoin Fund",              with_fallback(fetch_btcw, estimate_btcw)),
    ("MSBT", "Morgan Stanley",        "Morgan Stanley Bitcoin Trust ETP",      with_fallback(fetch_msbt, estimate_msbt)),  # launched 2026-04-08
]
NOT_COVERED = "Hashdex DEFI (liquidated August 2026)"
HISTORY_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.csv")


# HIST: the start of the ETF era. Grayscale's trust held this much bitcoin on
# 2023-12-31, eleven days before it and the nine new funds began trading as
# ETFs (GBTC 10-K for 2023, filed 2024-02-23, XBRL InvestmentOwnedBalanceContracts).
# Every other fund held nothing. So 2024's net is the 2024-12-31 total less this.
GBTC_2023_12_31 = 619525.92917
QUARTERLY_CSV = os.path.join(HERE, "history_quarterly.csv")


def eastern_date(generated_at):
    """The US Eastern calendar date of a run (the issuers work on New York
    time; a run at 8 pm Eastern is still that day's read, not tomorrow's)."""
    from zoneinfo import ZoneInfo
    t = dt.datetime.strptime(generated_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return t.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def run_trading_day(generated_at):
    """The trading day a run's figures reflect: the previous business day of
    the run's US Eastern date. Checked on 2026-10-01: at 6 pm Eastern every
    issuer's file still showed the previous close (iShares' as-of date said so;
    Bitwise's and ARK's figures matched Farside's previous-day line). Bitwise
    posts the new day between 6 and 9 pm and iShares the next afternoon, so
    the one daily pull is scheduled at 6 pm Eastern (22:00 UTC) and nothing
    later, which keeps every fund on the same day."""
    return trading_day(eastern_date(generated_at))


def file_trading_day(file):
    """The trading day an earlier etf.json reflects (older files lack the field)."""
    if file.get("holdings_date"):
        return file["holdings_date"]
    return run_trading_day(file.get("generated_at", "2026-01-01T12:00:00Z"))


def trading_day(run_date):
    """The trading day a read reflects: the issuers post the prior close, so a
    weekday run is the day before and a weekend or Monday run is the Friday.
    Labelling flows this way lines them up with the trading-day tables everyone
    else publishes (a Tuesday-morning read is Monday's flow). `run_date` is the
    US Eastern date (see eastern_date)."""
    d = dt.date.fromisoformat(run_date)
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def flow_periods(today_funds, today, today_stale=None):
    """Net bitcoin added by year, by quarter and by month (this year) and over
    the last 7 and 30 days. Period ends come from the trusts' SEC filings at
    quarter ends (history_quarterly.csv), from the daily reads otherwise, and
    from today's file for the period that holds today. Every net is summed
    fund by fund over the funds present at BOTH ends, so a fund launching
    (MSBT, April 2026) or closing never shows up as a flow. Missing data means
    a period is left out, never guessed."""
    exact = {}
    if os.path.exists(QUARTERLY_CSV):
        with open(QUARTERLY_CSV) as f:
            for r in csv.DictReader(f):
                try:
                    exact.setdefault(r["date"], {})[r["ticker"]] = float(r["btc"])
                except (KeyError, ValueError):
                    pass
    daily = {}
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV) as f:
            for r in csv.DictReader(f):
                row = {}
                stale = {x.split("=")[0] for x in (r.get("flags") or "").split()}  # =s carried forward, =e estimated
                for t, _, _, _ in FUNDS:
                    v = r.get(t)
                    if v and t not in stale:  # only funds read fresh take part in day-based math
                        try:
                            row[t] = float(v)
                        except ValueError:
                            pass
                if row and r.get("date"):
                    daily[r["date"]] = row  # keyed by trading day
    daily[today] = {t: v for t, v in today_funds.items() if t not in (today_stale or {})}
    year = int(today[:4])

    def at(end):
        """(per-fund holdings, how) at a date: exact filing, else the last daily
        read within three days before it, else None."""
        if end == "2023-12-31":
            return {"GBTC": GBTC_2023_12_31}, "exact"
        if end in exact:
            return exact[end], "exact"
        d = dt.date.fromisoformat(end)
        for back in range(0, 4):
            k = (d - dt.timedelta(days=back)).isoformat()
            if k in daily:
                return daily[k], "daily"
        return None, ""

    def net_between(a, b):
        common = [t for t in b if t in a]
        return sum(b[t] - a[t] for t in common), len(common)

    def period(label, start_end, end, group):
        start, s_how = at(start_end)
        if start is None:
            return None
        if end > today:  # a period whose last day is this trading day is complete, not "so far"
            finish, f_how = dict(today_funds), "so far"
        else:
            finish, f_how = at(end)
        if finish is None:
            return None
        # 2024: every fund other than GBTC began at zero on launch
        if start_end == "2023-12-31":
            start = {t: start.get(t, 0.0) for t in finish}
        net, n = net_between(start, finish)
        if n == 0:
            return None
        status = "so far" if f_how == "so far" else ("exact" if s_how == "exact" and f_how == "exact" else "daily")
        return {"group": group, "label": label, "net_btc": round(net, 2),
                "start_btc": round(sum(start[t] for t in finish if t in start), 2),
                "end_btc": round(sum(finish[t] for t in finish if t in start), 2),
                "funds": n, "status": status, "through": today if status == "so far" else end}

    out = []
    for y in range(2024, year + 1):
        p = period(str(y), f"{y - 1}-12-31", f"{y}-12-31", "year")
        if p:
            out.append(p)
    q_ends = [f"{year}-03-31", f"{year}-06-30", f"{year}-09-30", f"{year}-12-31"]
    q_starts = [f"{year - 1}-12-31"] + q_ends[:3]
    for i in range(4):
        if q_starts[i] >= today:
            break
        p = period(f"Q{i + 1} {year}", q_starts[i], q_ends[i], "quarter")
        if p:
            out.append(p)
    names = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    for m in range(1, 13):
        first = dt.date(year, m, 1)
        if first.isoformat() > today:
            break
        last = (dt.date(year + 1, 1, 1) if m == 12 else dt.date(year, m + 1, 1)) - dt.timedelta(days=1)
        prev_end = (first - dt.timedelta(days=1)).isoformat()
        p = period(f"{names[m - 1]} {year}", prev_end, last.isoformat(), "month")
        if p:
            out.append(p)
    # Rolling windows from the daily reads. Until the history is that long, the
    # window starts at the first morning on record and the entry says so.
    earlier = sorted(k for k in daily if k < today)
    def window(label, start_key, status):
        net, n = net_between(daily[start_key], today_funds)
        if n == 0:
            return None
        return {"group": "window", "label": label, "net_btc": round(net, 2),
                "start_btc": round(sum(daily[start_key][t] for t in today_funds if t in daily[start_key]), 2),
                "end_btc": round(sum(today_funds[t] for t in today_funds if t in daily[start_key]), 2),
                "funds": n, "status": status, "through": today, "since": start_key}
    if earlier:
        shown = 0
        for label, days in (("Last 7 days", 7), ("Last 30 days", 30)):
            target = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
            at_or_before = [k for k in earlier if k <= target]
            if not at_or_before:
                continue  # the record is not that long yet; no row rather than a misleading one
            w = window(label, at_or_before[-1], "daily")
            if w:
                out.append(w)
                shown += 1
        # Fewer than seven days on record: no window row at all (owner's rule:
        # never show a period the data cannot fill).
    return out


def main():
    previous = None
    if os.path.exists(PREV_JSON):
        try:
            with open(PREV_JSON) as f:
                previous = json.load(f)
        except Exception:
            previous = None
    # SAMEDAY: a second run on the same day (a manual run, a re-run) must still
    # compare against the last different day, not against this morning's file,
    # or the change column would show a few hours of drift as a day's flow.
    run_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    this_td = run_trading_day(run_at)  # the close this run's figures reflect
    baseline_path = os.path.join(HERE, "etf_baseline.json")
    newest = previous  # the last file written, whatever day: the carry-forward source
    if previous and file_trading_day(previous) == this_td:
        # Same trading day as the last file (an evening read followed by the
        # morning read, or a manual re-run): compare against the day before.
        try:
            with open(baseline_path) as f:
                previous = json.load(f)
            if file_trading_day(previous) == this_td:
                previous = None
        except Exception:
            previous = None
        if previous is None:
            try:  # history.csv is keyed by trading day; the last row before this one stands in
                with open(HISTORY_CSV) as f:
                    rows = [r for r in csv.DictReader(f) if r.get("date", "") < this_td]
                if rows:
                    last = rows[-1]
                    stale_t = {x.split("=")[0] for x in (last.get("flags") or "").split() if x.endswith("=s")}
                    est_t = {x.split("=")[0] for x in (last.get("flags") or "").split() if x.endswith("=e")}
                    previous = {"generated_at": last["date"] + "T12:00:00Z", "holdings_date": last["date"],
                                "funds": [{"ticker": t, "btc": float(last[t]), "stale": t in stale_t, "estimated": t in est_t}
                                          for t, _, _, _ in FUNDS if last.get(t)]}
            except Exception as e:
                print(f"history.csv not used as baseline: {e}", file=sys.stderr)
    prev_by_ticker = {}
    prev_kind = {}  # "read" / "estimate" / "stale" on the baseline day
    prev_rec = {}
    if previous:
        for fnd in previous.get("funds", []):
            if isinstance(fnd.get("btc"), (int, float)):
                prev_by_ticker[fnd["ticker"]] = fnd["btc"]
                prev_kind[fnd["ticker"]] = "stale" if fnd.get("stale") else ("estimate" if fnd.get("estimated") else "read")
    if newest:
        for fnd in newest.get("funds", []):
            if isinstance(fnd.get("btc"), (int, float)) and fnd.get("btc", 0) > 0:
                prev_rec[fnd["ticker"]] = fnd
    # A fund missing from the newest file (it failed and nothing carried) still
    # has its last good morning in history.csv; carry from there, dated that day.
    try:
        if os.path.exists(HISTORY_CSV):
            with open(HISTORY_CSV) as f:
                hist = list(csv.DictReader(f))
            for ticker, _, _, _ in FUNDS:
                if ticker in prev_rec:
                    continue
                for row in reversed(hist):
                    v = row.get(ticker, "")
                    if v and float(v) > 0:
                        prev_rec[ticker] = {"btc": float(v), "as_of": row["date"], "source": "history.csv",
                                            "method": "last good morning read, from history.csv"}
                        break
    except Exception as e:
        print(f"history.csv not used for carry-forward: {e}", file=sys.stderr)

    results = []
    for i, (ticker, issuer, name, fn) in enumerate(FUNDS):
        if i:
            time.sleep(PAUSE_BETWEEN_ISSUERS)
        rec = {"ticker": ticker, "issuer": issuer, "name": name}
        try:
            # RETRY: one second try after a short pause; this morning's FBTC miss
            # was a transient network error that passed on the next attempt.
            try:
                got = fn()
            except Exception as first:
                print(f"{ticker:5s} first try failed ({type(first).__name__}); retrying in 20 s", file=sys.stderr)
                time.sleep(20)
                got = fn()
            btc, as_of, source, method = got[0], got[1], got[2], got[3]
            estimated = len(got) > 4 and bool(got[4])
            rec.update({"btc": round(float(btc), 8), "as_of": as_of, "source": source, "method": method})
            if estimated:
                rec["estimated"] = True
            print(f"{ticker:5s} {btc:>16,.4f} BTC  as of {as_of or '?':10s}  {method}", file=sys.stderr)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"{ticker:5s} FAILED: {err}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr) if os.environ.get("ETF_DEBUG") else None
            if ticker in prev_rec:
                # One flaky morning must not drop a fund from the total: carry the
                # last good figure forward, with its own date, and say so.
                old = prev_rec[ticker]
                rec.update({"btc": old["btc"], "as_of": old.get("as_of", ""), "source": old.get("source", ""),
                            "method": old.get("method", ""), "stale": True, "error": err})
                print(f"{ticker:5s} carried forward {old['btc']:,.4f} BTC as of {old.get('as_of','?')}", file=sys.stderr)
            else:
                rec.update({"btc": None, "as_of": "", "source": "", "method": "", "error": err})
        # A day's change is only meaningful between two figures of the same kind:
        # a fresh read against a fresh read, or an estimate against an estimate.
        # Estimate-against-carried-forward is the difference between two methods,
        # not a flow, so it is left blank.
        # (change_btc is filled in below from history.csv, the one record keyed by trading day)
        results.append(rec)

    close_browser()

    ok = [r for r in results if r.get("btc") is not None]
    out = {
        "generated_at": run_at,  # set before the reads so the trading day and the stamp agree
        "funds": results,
        "total_btc": round(sum(r["btc"] for r in ok), 8),
        "ok": len(ok),
        "failed": len(results) - len(ok),
    }
    out["stale"] = sum(1 for r in ok if r.get("stale"))
    out["estimated"] = sum(1 for r in ok if r.get("estimated"))
    out["not_covered"] = NOT_COVERED
    out["universe"] = len(FUNDS)  # how many US spot funds exist; the app's "N of M"
    out["holdings_date"] = this_td  # the close these figures reflect
    out["read_date"] = eastern_date(out["generated_at"])  # the US Eastern date of the read
    if previous:
        out["previous_generated_at"] = previous.get("generated_at", "")

    if previous is not None and file_trading_day(previous) != this_td:
        with open(baseline_path, "w") as f:
            json.dump(previous, f, indent=2)
    # One line a day of history, for charts later: date, total, then each fund
    # in FUNDS order. SAMEDAY: a second run on the same day replaces that day's line.
    try:
        header = "date,total_btc," + ",".join(t for t, _, _, _ in FUNDS) + ",flags"
        lines = []
        if os.path.exists(HISTORY_CSV):
            with open(HISTORY_CSV) as h:
                lines = [ln.rstrip("\n") for ln in h if ln.strip()]
        if not lines or not lines[0].startswith("date,"):
            lines = [header] + lines
        if lines[0] != header:
            # A fund was added (MSBT): re-key every old row onto the new header.
            old_cols = lines[0].split(",")
            new_cols = header.split(",")
            # rows written by a newer script under the old header carry the new
            # funds as extra cells, in FUNDS order
            keyed = old_cols + [c for c in new_cols if c not in old_cols]
            fixed = [header]
            for ln in lines[1:]:
                cells = ln.split(",")
                m = dict(zip(keyed, cells))
                fixed.append(",".join(m.get(c, "") for c in new_cols))
            lines = fixed
        day = this_td  # rows are keyed by the trading day they reflect; a later read of the same day replaces the row
        lines = [ln for ln in lines if not ln.startswith(day + ",")]
        seen = {}
        for ln in lines[1:]:  # one line per date; the later one wins
            seen[ln.split(",", 1)[0]] = ln
        lines = [lines[0]] + [seen[k] for k in sorted(seen)]
        by = {r["ticker"]: r for r in results}
        cells = [day, f"{out['total_btc']:.4f}"]
        for t, _, _, _ in FUNDS:
            v = by.get(t, {}).get("btc")
            cells.append("" if v is None else f"{v:.4f}")
        # flags: which funds were carried forward (s) or estimated (e) that morning,
        # so the period math can leave a stale figure out of a daily comparison
        flags = " ".join(t + ("=s" if by.get(t, {}).get("stale") else "=e") for t, _, _, _ in FUNDS
                         if by.get(t, {}).get("stale") or by.get(t, {}).get("estimated"))
        cells.append(flags)
        lines.append(",".join(cells))
        with open(HISTORY_CSV, "w") as h:
            h.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"history.csv not written: {e}", file=sys.stderr)
    # FLOWS: one entry a day of net buying or selling across the funds that were
    # read fresh that morning (not estimated, not carried forward), for the app's
    # "last days" rows. Kept for the last 60 days; today's entry replaces itself.
    # The daily flows are rebuilt from history.csv every run (keyed by trading
    # day, with flags), so they heal themselves whenever a row is replaced by a
    # fuller read of the same day. Only funds read fresh at BOTH ends count:
    # an estimate moves with Nasdaq's rounded market cap, not with buying.
    flows = []
    try:
        rows = []
        with open(HISTORY_CSV) as f:
            for r in csv.DictReader(f):
                if r.get("date"):
                    rows.append(r)
        rows.sort(key=lambda r: r["date"])

        def fresh_values(r):
            bad = {x.split("=")[0] for x in (r.get("flags") or "").split()}  # =s or =e
            vals = {}
            for t, _, _, _ in FUNDS:
                v = r.get(t)
                if v and t not in bad:
                    try:
                        vals[t] = float(v)
                    except ValueError:
                        pass
            return vals

        for i in range(1, len(rows)):
            a, b = fresh_values(rows[i - 1]), fresh_values(rows[i])
            changes = {t: round(b[t] - a[t], 4) for t in b if t in a}
            if not changes:
                continue
            net = sum(changes.values())
            big = max(changes, key=lambda t: abs(changes[t]))
            flows.append({
                "date": rows[i]["date"],
                "net_btc": round(net, 4),
                "counted": len(changes),
                "universe": len(FUNDS),
                "up": sum(1 for v in changes.values() if v > 0),
                "down": sum(1 for v in changes.values() if v < 0),
                "biggest_ticker": big,
                "biggest_change": changes[big],
                "funds": changes,
            })
        flows = flows[-60:]
        # today's per-fund change in etf.json, from the same two rows
        if flows and flows[-1]["date"] == this_td:
            for rec in results:
                if rec["ticker"] in flows[-1]["funds"]:
                    rec["change_btc"] = flows[-1]["funds"][rec["ticker"]]
    except Exception as e:
        print(f"daily flows not built: {e}", file=sys.stderr)

    with open(OUT_JSON, "w") as f:
        json.dump(out, f, indent=2)
    shutil.copyfile(OUT_JSON, PREV_JSON)
    try:
        flows_path = os.path.join(HERE, "flows.json")
        periods = flow_periods({r["ticker"]: r["btc"] for r in results if isinstance(r.get("btc"), (int, float)) and r["btc"] > 0},
                               this_td,
                               {r["ticker"]: r for r in results if r.get("stale") or r.get("estimated")})
        with open(flows_path, "w") as f:
            json.dump({"days": flows, "periods": periods}, f, indent=2)
    except Exception as e:
        print(f"flows.json not written: {e}", file=sys.stderr)
    print(f"wrote {OUT_JSON}: ok={out['ok']} failed={out['failed']} total_btc={out['total_btc']:,.2f}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
