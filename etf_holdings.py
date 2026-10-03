#!/usr/bin/env python3
"""
etf_holdings.py - fetch the number of bitcoin held by each US spot Bitcoin ETF
from each issuer's OWN public page, file or API (no third-party trackers),
and write etf.json, history.csv (one row per NYSE trading day, from the 6 pm
New York read) and flows.json; etf_previous.json is the last file written.

Dependencies: requests, beautifulsoup4 and openpyxl (Grayscale's workbooks).
Playwright + Chromium are loaded lazily and only for issuers whose sites
refuse plain HTTP clients (Invesco always; Fidelity's dashboard and Morgan
Stanley's page as fallbacks).
"""
# PIPE-282 (GH-29): docstring brought up to date (6 pm read, history.csv, flows.json, openpyxl)
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
    r = get(url + "?ts=" + str(int(time.time())), headers={"Accept": "text/csv,*/*"})  # PIPE-282 (GH-34): never a CDN copy cached before the post
    txt = r.text
    m = re.search(r'Fund Holdings as of,"([^"]+)"', txt)
    as_of = iso_date(m.group(1)) if m else ""
    # PIPE-282 (GH-15): the Quantity column is found by its header name, so an added
    # column can never publish a dollar figure as bitcoin
    col = None
    for line in txt.splitlines():
        if line.startswith("Ticker,"):  # PIPE-282 (GH-15): Ticker,Name,Sector,...,Quantity,...
            head = [c.strip().strip('"') for c in line.split(",")]  # PIPE-282 (GH-15)
            col = head.index("Quantity") if "Quantity" in head else None  # PIPE-282 (GH-15)
        if line.startswith('"BTC"'):
            if col is None:  # PIPE-282 (GH-15)
                raise RuntimeError("iShares holdings CSV has no Quantity column header")  # PIPE-282 (GH-15)
            cells = [c.strip('"') for c in line.split('","')]
            qty = num(cells[col])  # PIPE-282 (GH-15)
            return qty, as_of, url, "requests: iShares latest-holdings.csv, BTC row, Quantity column"
    raise RuntimeError("BTC row not found in iShares holdings CSV")


def fetch_fbtc_api():
    """Fidelity's own research API, the one its quote dashboard calls: a token
    from /api/tokens, then POST /api/quote {"symbol": "FBTC"}. The answer's
    cryptoDetails.totalUnitPerCoin is 'Total bitcoin in fund' with its as-of
    date. Plain requests, no browser (verified 2026-10-01: 183,166.9836 BTC as
    of 09/30/2026, the same figure the page shows)."""
    page = "https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=FBTC"
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US"})
    s.get(page, timeout=TIMEOUT)
    tokens = s.get("https://digital.fidelity.com/prgw/digital/research/api/tokens", timeout=TIMEOUT,
                   headers={"Accept": "application/json", "Referer": page}).json()
    tok = None
    stack = [tokens]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if "csrf" in k.lower() and isinstance(v, str):
                    tok = v
                stack.append(v)
    if not tok:
        raise RuntimeError("Fidelity token endpoint gave no csrf token")
    q = s.post("https://digital.fidelity.com/prgw/digital/research/api/quote", json={"symbol": "FBTC"}, timeout=TIMEOUT,
               headers={"Content-Type": "application/json", "Accept": "application/json",
                        "Origin": "https://digital.fidelity.com", "Referer": page, "X-CSRF-TOKEN": tok})
    q.raise_for_status()
    m = re.search(r'"cryptoDetails":\{"totalUnitPerCoin":([\d.]+).*?"asOfDate":"(\d{2}/\d{2}/\d{4})"', q.text)
    if not m:
        raise RuntimeError("Fidelity quote API answered without cryptoDetails")
    return float(m.group(1)), iso_date(m.group(2)), page, "requests: Fidelity research API (feeds the FBTC quote dashboard), cryptoDetails 'totalUnitPerCoin'"


def fetch_fbtc():
    """Fidelity: its research API first; the quote dashboard in a browser second."""
    try:
        return fetch_fbtc_api()
    except Exception as e:
        print(f"   Fidelity API failed ({type(e).__name__}: {e}); trying the dashboard", file=sys.stderr)
    url = "https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=FBTC"
    text, _ = browser_page(url, wait_text="Total bitcoin in fund", max_wait=75)
    m = re.search(r"Total bitcoin in fund\s*\n\s*As of\s+([A-Za-z]{3}-\d{2}-\d{4})\s*\n\s*([\d,]+\.?\d*)", text)
    if not m:
        # Say what Fidelity served instead, so the log explains a refusal.
        head = re.sub(r"\s+", " ", text)[:300]
        raise RuntimeError(f"'Total bitcoin in fund' not found on Fidelity dashboard; page began: {head!r}")
    return num(m.group(2)), iso_date(m.group(1)), url, \
        "playwright: Fidelity quote dashboard, 'Total bitcoin in fund' field"


# GRAYSCALE-262: Grayscale's own performance workbooks, the files its product
# pages download, on a public Amazon S3 bucket that has no bot wall. Sheet
# "Daily Performance" has one row per business day (Date, Shares Outstanding,
# NAV, AUM) back to launch; sheet "Holdings" has the current bitcoin per share.
# A row dated D carries NAV for D but shares settled through the day before, so
# the shares after trading day T's creations and redemptions sit on the next
# row (checked 2026-10-01: every share change times NAV matched Farside's dollar
# flow for the previous trading day, e.g. GBTC row 2026-09-16 -750,000 shares =
# -$44.1M = Farside Sep 15). Holdings for T = shares(next row) x bitcoin per share.
GRAYSCALE_XLSX = {
    "GBTC": "https://reporting-prod-20231113144948145500000003.s3.us-east-1.amazonaws.com/product-performance/672e88c7-dac6-4fcd-9069-18eef01a2c73.xlsx",
    "BTC":  "https://reporting-prod-20231113144948145500000003.s3.amazonaws.com/product-performance/9ba286d6-3067-4153-b430-81d9d7a25696.xlsx",
}


def grayscale_rows(ticker):
    """[(date, shares)] oldest first, and (bps, bps_date), from Grayscale's workbook."""
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        os.system(f"{sys.executable} -m pip install --break-system-packages -q openpyxl")
        import openpyxl
    import io
    r = requests.get(GRAYSCALE_XLSX[ticker], headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    daily = list(wb["Daily Performance"].iter_rows(values_only=True))
    head = [str(c or "").strip() for c in daily[0]]
    i_date, i_sh = head.index("Date"), head.index("Shares Outstanding")
    rows = sorted({(str(x[i_date])[:10], float(x[i_sh])) for x in daily[1:] if x and x[i_date] and x[i_sh]})
    hold = list(wb["Holdings"].iter_rows(values_only=True))
    hh = [str(c or "").strip() for c in hold[0]]
    bps_row = next(x for x in hold[1:] if x and str(x[hh.index("Name")]).strip().upper() == "BTC")
    bps = float(bps_row[hh.index("Asset/Share")])
    if not rows or not 0 < bps < 1:
        raise RuntimeError("Grayscale workbook had no daily rows or no bitcoin per share")
    return rows, (bps, str(bps_row[hh.index("Date")])[:10])


def grayscale_settled(ticker):
    """{trading day: bitcoin held after that day's creations and redemptions}
    for every day in the workbook, at the current bitcoin per share. Holding
    bitcoin per share fixed makes a day's change exactly the shares created or
    redeemed, which is what a flow is (the fee moves it about 0.004% a day)."""
    rows, (bps, bps_date) = grayscale_rows(ticker)
    return {d0: sh1 * bps for (d0, _), (d1, sh1) in zip(rows, rows[1:])}, rows, bps, bps_date


def _grayscale(ticker):
    rows, (bps, bps_date) = grayscale_rows(ticker)
    target = run_trading_day(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    after = [r for r in rows if r[0] > target]
    if after:
        shares, row_date = after[0][1], after[0][0]
        return shares * bps, target, GRAYSCALE_XLSX[ticker], \
            (f"requests: Grayscale performance workbook, {shares:,.0f} shares (row {row_date}, settles {target}) "
             f"x {bps:.8f} bitcoin per share (Holdings sheet, {bps_date})")
    # PIPE-282 (GH-29): Grayscale posts the row that settles a trading day either by
    # the 6 pm read of the next business day (the 2026-10-02 row was up at 6:07 pm)
    # or only overnight after it (the 2026-10-01 row landed at 1:53 am Eastern on
    # Oct 2). When it is not up yet, the newest settled day is the one before.
    # Report it, dated as such, and flagged carried (6th element): the next run
    # heals the day in history.csv from the workbook (heal_grayscale).
    before = [r for r in rows if r[0] <= target]
    shares, row_date = before[-1][1], before[-1][0]
    as_of = before[-2][0] if len(before) > 1 else trading_day(row_date)  # PIPE-282 (GH-3): the workbook's own previous row (its rows are NYSE trading days)
    return shares * bps, as_of, GRAYSCALE_XLSX[ticker], \
        (f"requests: Grayscale performance workbook, {shares:,.0f} shares (row {row_date}, settles {as_of}) "
         f"x {bps:.8f} bitcoin per share; Grayscale had not yet posted the shares that settle {target}"), False, True


def heal_grayscale(history_path):
    """Rewrite GBTC and BTC in every history.csv row from Grayscale's workbook,
    exact, and drop their carried or estimated flags for the days it settles.
    Runs at any hour: it changes only past days with Grayscale's own numbers."""
    with open(history_path) as f:
        rdr = csv.DictReader(f)
        cols = rdr.fieldnames
        rows = list(rdr)
    changed = 0
    for t in GRAYSCALE_XLSX:
        if t not in cols:
            continue
        try:
            settled = grayscale_settled(t)[0]
        except Exception as e:
            print(f"{t:5s} history not healed: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for r in rows:
            v = settled.get(r["date"])
            if v is None:
                continue
            flags = [x for x in (r.get("flags") or "").split() if x.split("=")[0] != t]
            old = r.get(t, "")
            r[t] = f"{v:.4f}"
            r["flags"] = " ".join(flags)
            if old != r[t]:
                changed += 1
    for r in rows:  # totals follow the funds
        r["total_btc"] = f"{sum(float(r[t]) for t, _, _, _ in FUNDS if r.get(t)):.4f}"
    with open(history_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"history.csv: {changed} Grayscale cells healed from the workbooks", file=sys.stderr)


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


# PIPE-282 (GH-2): fetch_btcw() is gone. WisdomTree answers GitHub's runners with a
# Cloudflare challenge, and on the days its page did load, the loose regex took the
# year 2025 for the holdings (stored as a fresh read on 2026-09-29 and 09-30). BTCW
# is the flagged estimate every day (see FUNDS).


# --- SEC-anchored estimates (PIPE-282, GH-29) -----------------------------------
# WisdomTree and Morgan Stanley refuse automated readers, and Fidelity's own
# sources can fail on a given day. Then the holdings are ESTIMATED from public
# filings, and the JSON says so ("estimated": true, with the basis):
#   bitcoin per share moves only by the daily sponsor fee between filings, so
#   BPS(today) = BPS(quarter end) x (1 - fee)^(days/365), exactly (checked against
#   two quarters of Grayscale filings: matches to five decimals);
#   holdings(today) = shares outstanding(today) x BPS(today).
# PIPE-282 (GH-5): shares outstanding come from Nasdaq's market cap, divided by
# the price Nasdaq used for it (see nasdaq_shares); dividing by the after-hours
# last price, as before, gave 36,884,882 and 1,958,994 instead of 36,710,000 and
# 1,945,000 on 2026-10-02.
# PIPE-282 (GH-16): Grayscale no longer falls back to an estimate. Its fallback took
# shares from the last 10-Q cover (172,260,100 on 2026-07-30 against 163,870,100 in
# Grayscale's own workbook on 2026-10-02, 4.8% high), and Nasdaq's GBTC count is no
# better (247,465,857 on 2026-10-02; BTC has no market cap there). An unreadable
# workbook now keeps the last workbook figure, carried with its date, and
# heal_grayscale() corrects the day on the next run. sec_facts(), _latest(),
# SEC_UA and estimate_grayscale() went with it (GH-28).


def nasdaq_shares(ticker):
    """Shares outstanding implied by Nasdaq's market cap.  PIPE-282 (GH-5): Nasdaq
    computes MarketCap with the previous close during the day and the evening
    session (2026-10-02 5:42 pm: MSBT 890,401,050 = 36,710,000 x $24.255) and with
    the day's close once the market is closed (9:39 pm: 887,280,700 = 36,710,000 x
    $24.17), so the price that gives a whole number of shares is the right one."""
    h = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US"}
    base = f"https://api.nasdaq.com/api/quote/{ticker}/"
    summ = requests.get(base + "summary?assetclass=etf", headers=h, timeout=TIMEOUT).json()["data"]["summaryData"]
    data = requests.get(base + "info?assetclass=etf", headers=h, timeout=TIMEOUT).json()["data"]  # PIPE-282 (GH-5)
    info = data["primaryData"]
    cap = num(summ["MarketCap"]["value"])
    cands = []  # PIPE-282 (GH-5): (whole-share test, distance to a round thousand), shares, price basis
    for basis, raw in (("previous close", (summ.get("PreviousClose") or {}).get("value")),
                       ("last price", info.get("lastSalePrice")),
                       ("close", (data.get("secondaryData") or {}).get("lastSalePrice"))):
        try:
            px = num(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            sh = cap / px
            cands.append(((abs(sh - round(sh)) > 0.05, abs(sh - round(sh / 1000.0) * 1000.0)), round(sh), basis))
    if not cap or not cands:
        raise RuntimeError(f"Nasdaq gave no market cap or price for {ticker}")
    _, shares, basis = min(cands)  # PIPE-282 (GH-5)
    return shares, f"{basis}, {info.get('lastTradeTimestamp', '')}"  # PIPE-282 (GH-5): the basis goes into the method text

def _decayed_bps(bps_anchor, anchor_date, fee_per_year):
    days = (dt.date.today() - dt.date.fromisoformat(anchor_date)).days
    return bps_anchor * (1.0 - fee_per_year) ** (days / 365.0)

# PIPE-282 (GH-16, GH-28): estimate_grayscale() removed (see the note above nasdaq_shares).

# WisdomTree's XBRL carries no bitcoin count; the 10-Q's schedule of investments
# does. quarter_anchor() takes the newest quarter end from history_quarterly.csv
# (written by backfill_edgar.py); the typed constant is only the fallback.  # PIPE-282 (GH-28)
BTCW_ANCHOR = {"btc": 2310.0, "shares": 2_185_000, "date": "2026-06-30", "fee": 0.0025}
QUARTERLY_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_quarterly.csv")


def estimate_btcw():
    a = quarter_anchor("BTCW", BTCW_ANCHOR)  # PIPE-282 (GH-28): btcw_anchor() was a copy of quarter_anchor()
    bps = _decayed_bps(a["btc"] / a["shares"], a["date"], a["fee"])
    shares, stamp = nasdaq_shares("BTCW")
    est = shares * bps
    method = (f"ESTIMATE: {a['btc']:,.0f} BTC / {a['shares']:,} shares at {a['date']} (10-Q schedule of investments; "
              f"bitcoin per share less the {a['fee']*100:.2f}% fee since) x {shares:,} shares outstanding "
              f"(Nasdaq market cap / {stamp}); WisdomTree's site blocks automated readers")  # PIPE-282 (GH-5): the stamp names the price basis
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

MSBT_JSON = "https://www.morganstanley.com/im/json/imwebdata/data/product/EF/100761/chart/etfTradeDateHoldingsCurrent.json"


def msbt_bitcoin_row(obj):
    """TIDY-285 (GH-33): the holdings row that is the trust's bitcoin: a dict with a "quantity" and a name, ticker
    or description naming bitcoin (BTC, XBT). None when there is no such row."""
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "quantity" in x:
                words = " ".join(str(v) for k, v in x.items() if k != "quantity" and isinstance(v, str)).lower()
                if "bitcoin" in words or any(w in ("btc", "xbt") for w in re.split(r"[^a-z]+", words)):
                    return x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(reversed(x))
    return None


def fetch_msbt_json():
    """MSBT-263: the holdings file Morgan Stanley's own fund page loads: the
    trust's bitcoin quantity with its effective date (2026-10-01 in a browser:
    10,519.09657402 BTC as of 10/01/2026). Akamai refuses it from data-center
    addresses, so the estimate below stays as the fallback."""
    r = requests.get(MSBT_JSON, timeout=TIMEOUT, headers={
        "User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.morganstanley.com/im/en-us/individual-investor/product-and-performance/etfs/morgan-stanley-bitcoin-trust.html"})
    if r.status_code != 200 or not r.text.lstrip().startswith(("{", "[")):
        raise RuntimeError(f"Morgan Stanley holdings JSON refused (HTTP {r.status_code})")
    text = r.text
    # TIDY-285 (GH-33): parse the file and take the bitcoin row's quantity (the first "quantity" anywhere could be a cash line)
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError("Morgan Stanley holdings JSON did not parse")
    row = msbt_bitcoin_row(data)
    if row is None:
        raise RuntimeError("Morgan Stanley holdings JSON had no bitcoin row")
    d = row.get("effectiveDate") or ""
    if not d:
        m = re.search(r'"effectiveDate"\s*:\s*"(\d{1,2}/\d{1,2}/\d{4})"', text)
        d = m.group(1) if m else ""
    return num(str(row["quantity"])), iso_date(d) if d else "", MSBT_JSON, \
        "requests: Morgan Stanley etfTradeDateHoldingsCurrent.json (feeds the MSBT holdings tab), 'quantity'"


def fetch_msbt():
    try:
        return fetch_msbt_json()
    except Exception as e:
        print(f"   Morgan Stanley JSON failed ({type(e).__name__}: {e}); trying the page", file=sys.stderr)
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
              f"(Nasdaq market cap / {stamp}); Morgan Stanley's site blocks automated readers")  # PIPE-282 (GH-5): the stamp names the price basis
    return est, dt.date.today().isoformat(), "https://api.nasdaq.com/api/quote/MSBT/summary?assetclass=etf", method

# --- Fidelity FBTC ------------------------------------------------------------
# PIPE-282 (GH-29): Fidelity's research API reads fine from GitHub's runners
# (fetch_fbtc_api); its quote dashboard renders an empty page there. If both fail,
# the same anchor method as BTCW and MSBT. (The 0.5% gap seen on 2026-10-01 came
# from dividing by the after-hours price; see nasdaq_shares.)
FBTC_ANCHOR = {"btc": 174383.0, "shares": 200_328_476, "date": "2026-06-30", "fee": 0.0025}

def estimate_fbtc():
    a = quarter_anchor("FBTC", FBTC_ANCHOR)
    bps = _decayed_bps(a["btc"] / a["shares"], a["date"], a["fee"])
    shares, stamp = nasdaq_shares("FBTC")
    est = shares * bps
    method = (f"ESTIMATE: {a['btc']:,.0f} BTC / {a['shares']:,} shares at {a['date']} (10-Q schedule of investments; "
              f"bitcoin per share less the {a['fee']*100:.2f}% fee since) x {shares:,} shares outstanding "
              f"(Nasdaq market cap / {stamp}); Fidelity's research API and dashboard could not be read")  # PIPE-282 (GH-5, GH-29)
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


# PIPE-282 (GH-28): fetch_defi() removed; Hashdex DEFI was liquidated in August 2026 and is not in FUNDS.


# PIPE-282 (GH-29): all twelve US spot bitcoin ETFs, read each weekday at 6 pm New
# York time from the issuers' own files, APIs and workbooks. FBTC falls back to an
# estimate anchored to the trust's own SEC filings, flagged "estimated", when
# Fidelity's API and dashboard both fail; BTCW and MSBT are estimated that way every
# day (their sites refuse automated readers; MSBT's own file is tried first). Any
# other fund that cannot be read keeps its last good figure, flagged "stale".
# Hashdex DEFI was liquidated in August 2026.
FUNDS = [
    ("IBIT", "BlackRock iShares",     "iShares Bitcoin Trust ETF",            fetch_ibit),
    ("FBTC", "Fidelity",              "Fidelity Wise Origin Bitcoin Fund",    with_fallback(fetch_fbtc, estimate_fbtc)),
    ("ARKB", "ARK 21Shares",          "ARK 21Shares Bitcoin ETF",             fetch_arkb),
    ("BITB", "Bitwise",               "Bitwise Bitcoin ETF",                  fetch_bitb),
    ("HODL", "VanEck",                "VanEck Bitcoin ETF",                   fetch_hodl),
    ("BRRR", "CoinShares Valkyrie",   "CoinShares Bitcoin ETF",               fetch_brrr),
    ("EZBC", "Franklin Templeton",    "Franklin Bitcoin ETF",                 fetch_ezbc),
    ("BTCO", "Invesco Galaxy",        "Invesco Galaxy Bitcoin ETF",           fetch_btco),
    ("GBTC", "Grayscale",             "Grayscale Bitcoin Trust ETF",          fetch_gbtc),  # PIPE-282 (GH-16): an unreadable workbook keeps the last workbook figure
    ("BTC",  "Grayscale",             "Grayscale Bitcoin Mini Trust ETF",     fetch_btc_mini),  # PIPE-282 (GH-16)
    ("BTCW", "WisdomTree",            "WisdomTree Bitcoin Fund",              lambda: (*estimate_btcw(), True)),  # PIPE-282 (GH-2): always the flagged estimate
    ("MSBT", "Morgan Stanley",        "Morgan Stanley Bitcoin Trust ETP",      with_fallback(fetch_msbt, estimate_msbt)),  # launched 2026-04-08
]
NOT_COVERED = "Hashdex DEFI (liquidated August 2026)"
# PIPE-282 (GH-10, log only): the issuers' own as-of dates at the 6 pm read, as seen
# on 2026-10-02: IBIT, FBTC, HODL, EZBC and BTCO name the trading day itself, Bitwise
# names the trading day before, ARKB and BRRR date their files with the next business
# day. A mismatch (a late or an early post) is printed in the run log, nothing more.
ASOF_IS_TRADING_DAY = {"IBIT", "FBTC", "HODL", "EZBC", "BTCO"}  # PIPE-282 (GH-10)
ASOF_ONE_DAY_EARLY = {"BITB"}  # PIPE-282 (GH-10)
HISTORY_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.csv")


# HIST: the start of the ETF era. Grayscale's trust held this much bitcoin on
# 2023-12-31, eleven days before it and the nine new funds began trading as
# ETFs (GBTC 10-K for 2023, filed 2024-02-23, XBRL InvestmentOwnedBalanceContracts).
# Every other fund held nothing. So 2024's net is the 2024-12-31 total less this.
GBTC_2023_12_31 = 619525.92917
# PIPE-282 (GH-28): QUARTERLY_CSV is defined once, next to the estimate anchors above


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
    the one daily pull is scheduled at 6 pm New York time (22:04 UTC in daylight
    time, 23:04 UTC in standard time; see etf.yml) and nothing later, which keeps
    every fund on the same day."""  # PIPE-282 (GH-4, GH-29)
    return trading_day(eastern_date(generated_at))


def file_trading_day(file):
    """The trading day an earlier etf.json reflects (older files lack the field)."""
    if file.get("holdings_date"):
        return file["holdings_date"]
    return run_trading_day(file.get("generated_at", "2026-01-01T12:00:00Z"))


# PIPE-282 (GH-3): the NYSE calendar, by NYSE's published rules (checked against
# NYSE's own 2025, 2026, 2027 and 2028 holiday lists: identical). Grayscale's
# workbooks have a row on exactly these trading days.
def _nth_weekday(y, m, weekday, n):
    """n-th (1-based) weekday (Monday=0) of month m; n=-1 for the last."""  # PIPE-282 (GH-3)
    if n > 0:
        d = dt.date(y, m, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(y):
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""  # PIPE-282 (GH-3)
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return dt.date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def _observed(d):
    """NYSE: a Saturday holiday closes the Friday before, a Sunday one the Monday after."""  # PIPE-282 (GH-3)
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


EXTRA_CLOSURES = {"2025-01-09"}  # PIPE-282 (GH-3): unscheduled closures (a national day of mourning); add by hand


def nyse_holidays(y):
    """NYSE full-day closures in year y."""  # PIPE-282 (GH-3)
    out = set()
    if dt.date(y, 1, 1).weekday() != 5:  # a Saturday New Year's Day is not observed on Friday Dec 31
        out.add(_observed(dt.date(y, 1, 1)))
    out.add(_nth_weekday(y, 1, 0, 3))            # Martin Luther King Jr. Day
    out.add(_nth_weekday(y, 2, 0, 3))            # Washington's Birthday
    out.add(_easter(y) - dt.timedelta(days=2))   # Good Friday
    out.add(_nth_weekday(y, 5, 0, -1))           # Memorial Day
    out.add(_observed(dt.date(y, 6, 19)))        # Juneteenth
    out.add(_observed(dt.date(y, 7, 4)))         # Independence Day
    out.add(_nth_weekday(y, 9, 0, 1))            # Labor Day
    out.add(_nth_weekday(y, 11, 3, 4))           # Thanksgiving
    out.add(_observed(dt.date(y, 12, 25)))       # Christmas
    return {d.isoformat() for d in out} | {x for x in EXTRA_CLOSURES if x.startswith(str(y))}


def is_trading_day(day):
    """True for an NYSE trading day (a date or a yyyy-mm-dd string)."""  # PIPE-282 (GH-3)
    d = dt.date.fromisoformat(day) if isinstance(day, str) else day
    return d.weekday() < 5 and d.isoformat() not in nyse_holidays(d.year)


def trading_day(run_date):
    """The trading day a read reflects: the last NYSE trading day before the
    run's US Eastern date (weekends and NYSE holidays skipped), because the
    issuers post the prior close. `run_date` is the US Eastern date (see
    eastern_date)."""  # PIPE-282 (GH-3)
    d = dt.date.fromisoformat(run_date) - dt.timedelta(days=1)
    while not is_trading_day(d):  # PIPE-282 (GH-3): was weekends only
        d -= dt.timedelta(days=1)
    return d.isoformat()


def flow_periods(today_funds, today, today_stale=None):
    """Net bitcoin added by year, by quarter and by month (this year) and over
    the last 7 and 30 days. Period ends come from the trusts' SEC filings at
    quarter ends (history_quarterly.csv), from the daily reads otherwise, and
    from today's file for the period that holds today. Every net is summed
    fund by fund over the funds present at BOTH ends; a fund that launched
    inside the period counts from zero (DECIDE-287, GH-23: MSBT, April 2026) and a
    closing fund never shows up as a flow. Missing data means a period is left
    out, never guessed."""
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
    # PIPE-282 (GH-6): the end of a running period and of the rolling windows is
    # each fund's newest FRESH figure (today's, else the newest of the three rows
    # before), so an estimate or a carried figure never stands in for a flow and a
    # day when Grayscale is late does not drop its funds from the year.
    fresh_end = {}
    for k in sorted((k for k in daily if k <= today), reverse=True)[:4]:
        for t, v in daily[k].items():
            fresh_end.setdefault(t, v)

    def at(end):
        """(per-fund holdings, how) at a date: exact filing, else the last daily
        read within three days before it, else None."""
        if end == "2023-12-31":
            return {"GBTC": GBTC_2023_12_31}, "exact"
        d = dt.date.fromisoformat(end)
        day = None  # PIPE-282 (GH-1)
        for back in range(0, 4):
            k = (d - dt.timedelta(days=back)).isoformat()
            if k in daily:
                day = daily[k]  # PIPE-282 (GH-1)
                break
        if end in exact:
            # PIPE-282 (GH-1): in filing season only some trusts have filed for this
            # quarter end; it is exact only when every fund on record at the quarter
            # before (and every fund read that day) has its filing. Until then the
            # filings that exist are merged with the daily read and called "daily".
            prev_q = max((k for k in exact if k < end), default=None)
            expected = (set(exact.get(prev_q, {})) | set(day or {})) & {t for t, _, _, _ in FUNDS}
            if set(exact[end]) >= expected:
                return exact[end], "exact"
            merged = dict(day or {})
            merged.update(exact[end])
            return merged, "daily"
        if day is not None:  # PIPE-282 (GH-1)
            return day, "daily"
        return None, ""

    # DECIDE-287 (GH-23): a fund that launched inside a period counts from zero, the rule 2024 already
    # used, so MSBT's first buying (April 2026) is part of 2026, Q2 2026 and April 2026.
    LAUNCHED = {"MSBT": "2026-04-08"}  # DECIDE-287 (GH-23)

    def net_between(a, b):
        common = [t for t in b if t in a]
        return sum(b[t] - a[t] for t in common), len(common)

    def period(label, start_end, end, group):
        start, s_how = at(start_end)
        if start is None:
            return None
        if end > today:  # a period whose last day is this trading day is complete, not "so far"
            finish, f_how = dict(fresh_end), "so far"  # PIPE-282 (GH-6): fresh figures only, as the day rows
        else:
            finish, f_how = at(end)
        if finish is None:
            return None
        # 2024: every fund other than GBTC began at zero on launch
        if start_end == "2023-12-31":
            start = {t: start.get(t, 0.0) for t in finish}
        for t, launched in LAUNCHED.items():  # DECIDE-287 (GH-23)
            if t in finish and t not in start and start_end < launched <= end:  # DECIDE-287 (GH-23)
                start = dict(start)  # DECIDE-287 (GH-23)
                start[t] = 0.0  # DECIDE-287 (GH-23)
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
    # Rolling windows from the daily reads, shown only once the record is that
    # long (owner's rule: never show a period the data cannot fill).  # PIPE-282 (GH-29)
    earlier = sorted(k for k in daily if k < today)
    def window(label, start_key, status):
        net, n = net_between(daily[start_key], fresh_end)  # PIPE-282 (GH-6)
        if n == 0:
            return None
        return {"group": "window", "label": label, "net_btc": round(net, 2),
                "start_btc": round(sum(daily[start_key][t] for t in fresh_end if t in daily[start_key]), 2),  # PIPE-282 (GH-6)
                "end_btc": round(sum(fresh_end[t] for t in fresh_end if t in daily[start_key]), 2),  # PIPE-282 (GH-6)
                "funds": n, "status": status, "through": today, "since": start_key}
    if earlier:
        for label, days in (("Last 7 days", 7), ("Last 30 days", 30)):  # PIPE-282 (GH-28): an unused counter removed
            target = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
            at_or_before = [k for k in earlier if k <= target]
            if not at_or_before:
                continue  # the record is not that long yet; no row rather than a misleading one
            w = window(label, at_or_before[-1], "daily")
            if w:
                out.append(w)
    return out


def main():
    previous = None
    if os.path.exists(PREV_JSON):
        try:
            with open(PREV_JSON) as f:
                previous = json.load(f)
        except Exception:
            previous = None
    # SAMEDAY: a second run for the same trading day (a forced re-run) keeps
    # etf_baseline.json and previous_generated_at on the last different day.  # PIPE-282 (GH-29)
    run_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    this_td = run_trading_day(run_at)  # the close this run's figures reflect
    # PIPE-282 (GH-3, GH-8): nothing is written on a New York weekend or NYSE
    # holiday (no issuer posts a new close), and a read outside 5 to 8 pm New York
    # time is a test read only (iShares may not have posted before, Bitwise is on
    # the next day after), unless ETF_FORCE=1 (the workflow's "force" input).
    from zoneinfo import ZoneInfo
    et_now = dt.datetime.strptime(run_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo("America/New_York"))
    forced = os.environ.get("ETF_FORCE") == "1"
    if not forced and not is_trading_day(et_now.date()):
        print(f"{et_now:%Y-%m-%d} is not an NYSE trading day in New York: no issuer posts a new close; nothing written", file=sys.stderr)
        return 0
    in_window = 17 <= et_now.hour <= 19  # PIPE-282 (GH-8): 5:00 to 7:59 pm New York time
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
    prev_rec = {}  # PIPE-282 (GH-28): prev_by_ticker and prev_kind were filled and never read
    if newest:
        for fnd in newest.get("funds", []):
            if isinstance(fnd.get("btc"), (int, float)) and fnd.get("btc", 0) > 0:
                prev_rec[fnd["ticker"]] = fnd
    # A fund missing from the newest file (it failed and nothing carried) still
    # has its last good read in history.csv; carry from there, dated that day.  # PIPE-282 (GH-29)
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
                                            "method": "last good read, from history.csv"}  # PIPE-282 (GH-29)
                        break
    except Exception as e:
        print(f"history.csv not used for carry-forward: {e}", file=sys.stderr)

    results = []
    for i, (ticker, issuer, name, fn) in enumerate(FUNDS):
        if i:
            time.sleep(PAUSE_BETWEEN_ISSUERS)
        rec = {"ticker": ticker, "issuer": issuer, "name": name}
        try:
            # RETRY: one second try after a short pause (an FBTC miss on 2026-09-30
            # was a transient network error that passed on the next attempt).  # PIPE-282 (GH-29)
            try:
                got = fn()
            except Exception as first:
                print(f"{ticker:5s} first try failed ({type(first).__name__}); retrying in 20 s", file=sys.stderr)
                time.sleep(20)
                got = fn()
            btc, as_of, source, method = got[0], got[1], got[2], got[3]
            estimated = len(got) > 4 and bool(got[4])
            # PIPE-282 (GH-15): a figure no fund could hold, or more than 50% away from the
            # fund's last good figure, is a failed read (the last good figure is kept)
            last_btc = (prev_rec.get(ticker) or {}).get("btc")
            if not 0 < float(btc) < 2_000_000 or (last_btc and not 0.5 < float(btc) / float(last_btc) < 1.5):
                raise RuntimeError(f"implausible read {float(btc):,.4f} BTC (last good {last_btc}); "
                                   f"the issuer's file may have changed format")  # PIPE-282 (GH-15)
            # PIPE-282 (GH-10, log only): an issuer as-of date that does not match the
            # trading day this read is filed under (a late or an early post)
            expect = trading_day(this_td) if ticker in ASOF_ONE_DAY_EARLY else this_td
            if (ticker in ASOF_IS_TRADING_DAY or ticker in ASOF_ONE_DAY_EARLY) and not estimated and as_of and as_of != expect:
                print(f"{ticker:5s} WARNING: the issuer's as-of date is {as_of}, expected {expect} for trading day {this_td}", file=sys.stderr)  # PIPE-282 (GH-10)
            if len(got) > 5 and got[5]:
                rec["stale"] = True  # GRAYSCALE-262: the issuer's newest settled day is the one before
            rec.update({"btc": round(float(btc), 8), "as_of": as_of, "source": source, "method": method})
            if estimated:
                rec["estimated"] = True
            print(f"{ticker:5s} {btc:>16,.4f} BTC  as of {as_of or '?':10s}  {method}", file=sys.stderr)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"{ticker:5s} FAILED: {err}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr) if os.environ.get("ETF_DEBUG") else None
            if ticker in prev_rec:
                # One failed read must not drop a fund from the total: carry the
                # last good figure forward, with its own date, and say so.  # PIPE-282 (GH-29)
                old = prev_rec[ticker]
                rec.update({"btc": old["btc"], "as_of": old.get("as_of", ""), "source": old.get("source", ""),
                            "method": old.get("method", ""), "stale": True, "error": err})
                print(f"{ticker:5s} carried forward {old['btc']:,.4f} BTC as of {old.get('as_of','?')}", file=sys.stderr)
            else:
                rec.update({"btc": None, "as_of": "", "source": "", "method": "", "error": err})
        # change_btc is filled in below from history.csv (funds read fresh on both days only).  # PIPE-282 (GH-29)
        results.append(rec)

    close_browser()
    if not (in_window or forced):  # PIPE-282 (GH-8): an off-window read mixes trading days; print it, write nothing
        print(f"run at {et_now:%H:%M} New York time is outside 5 to 8 pm: test read only, nothing written "
              f"(the workflow's force input, ETF_FORCE=1, writes anyway)", file=sys.stderr)  # PIPE-282 (GH-8)
        return 0

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
        # Only a read between 5 and 8 pm New York time goes into the daily record:
        # earlier, iShares may not have posted the previous close; later, Bitwise
        # has already posted the next one. Off-window runs returned above (test
        # read only) unless ETF_FORCE=1.  # PIPE-282 (GH-8)
        if not (in_window or forced):
            raise RuntimeError(f"run at {et_now:%H:%M} New York time is outside 5 to 8 pm; daily record left unchanged")  # PIPE-282 (GH-8)
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
        # flags: which funds were carried forward (s) or estimated (e) at that read,  # PIPE-282 (GH-29)
        # so the period math can leave a stale figure out of a daily comparison
        flags = " ".join(t + ("=s" if by.get(t, {}).get("stale") else "=e") for t, _, _, _ in FUNDS
                         if by.get(t, {}).get("stale") or by.get(t, {}).get("estimated"))
        cells.append(flags)
        lines.append(",".join(cells))
        with open(HISTORY_CSV, "w") as h:
            h.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"history.csv not written: {e}", file=sys.stderr)  # includes the deliberate off-hours skip
    try:  # GRAYSCALE-262: past days of GBTC and BTC, exact from Grayscale's workbooks
        if os.path.exists(HISTORY_CSV):
            heal_grayscale(HISTORY_CSV)
    except Exception as e:
        print(f"history.csv not healed: {e}", file=sys.stderr)
    # FLOWS: one entry a day of net buying or selling across the funds that were
    # read fresh at that day's 6 pm read (not estimated, not carried forward), for the app's  # PIPE-282 (GH-29)
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
            if trading_day(rows[i]["date"]) != rows[i - 1]["date"]:  # PIPE-282 (GH-7): a day is missing between the two rows
                continue  # the change spans two or more trading days; no single day can be shown
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
