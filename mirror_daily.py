#!/usr/bin/env python3
"""
mirror_daily.py: save a copy of every slow-changing source The Bitcoin
Halvening reads for its Dollar, Market and World pages, byte for byte, so the
app can read one static file per source instead of asking FRED, the Treasury,
the IMF, the World Bank, CoinGecko and Blockchain.com from every phone.

Each file is the source's own response, unchanged, so the app parses it with
the same code it uses for the live source. A source that cannot be read today
keeps yesterday's file (nothing is deleted) and is listed in manifest.json
under "failed" with the reason.

Output: mirror/<source>/<name>, plus mirror/manifest.json with generated_at.
"""

import datetime as dt
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "mirror")

# An honest, named agent. FRED and the IMF refuse a browser string coming from
# a data center; they answer a plain named client.
UA = {
    "User-Agent": "TheBitcoinHalvening-mirror/1.0 (+https://github.com/TheBitcoinHalvingApp/bitcoin-etf-holdings)",
    "Accept": "*/*",
}

# FRED series the app reads (DollarDataManager and WorldDataManager), from the
# earliest date any card asks for. The app trims to the window it needs.
FRED_SINCE = "2016-01-01"
FRED_IDS = [
    "M2SL", "CMDEBT", "HHMSDODNS", "TOTALSL", "REVOLSL", "GDP", "A191RL1Q225SBEA",
    "GFDEGDQ188S", "CPIAUCSL", "DGS10", "DGS30", "DGS2", "DFF", "A091RC1Q027SBEA",
    "MORTGAGE30US", "DCOILWTICO", "DCOILBRENTEU", "GASREGW", "GASDESW", "DTWEXBGS",
    "WALCL", "SP500", "NASDAQCOM", "POPTHM", "DEXUSEU", "DEXJPUS", "DEXCHUS",
    "DEXUSUK", "DEXCAUS", "DEXMXUS", "DJIA",
    # World page
    "ECBASSETSW", "JPNASSETS", "ECBDFR", "IRSTCI01JPM156N", "IRSTCI01GBM156N",
]

IMF_INDICATORS = ["GGXWDG_NGDP", "NGDPD", "PCPIPCH"]


def month_key(offset_months: int) -> str:
    today = dt.date.today().replace(day=1)
    y, m = today.year, today.month + offset_months
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return "%04d%02d" % (y, m)


def sources():
    """(relative path, url, validator) for everything to mirror."""
    items = []
    for sid in FRED_IDS:
        items.append((
            "fred/%s.csv" % sid,
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=%s" % (sid, FRED_SINCE),
            lambda b: b.startswith(b"observation_date") and b.count(b"\n") > 5,
        ))
    for key in (month_key(0), month_key(-1)):
        items.append((
            "treasury/yield_%s.xml" % key,
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=" + key,
            lambda b: b"<feed" in b[:2000],
        ))
    items.append((
        "treasury/mts_table_1.json",
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/mts/mts_table_1?sort=-record_date&page[size]=60&fields=record_date,classification_desc,record_type_cd,src_line_nbr,current_month_gross_rcpt_amt,current_month_gross_outly_amt",
        lambda b: b'"data"' in b[:200],
    ))
    gecko = [
        ("global.json", "https://api.coingecko.com/api/v3/global", b'"market_cap_percentage"'),  # PIPE-282 (API-9): the app reads this copy when its live /global call fails
        ("stablecoins.json", "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=stablecoins&per_page=100&page=1", b'"market_cap"'),
        ("treasury.json", "https://api.coingecko.com/api/v3/companies/public_treasury/bitcoin", b'"total_holdings"'),
        ("governments.json", "https://api.coingecko.com/api/v3/governments/public_treasury/bitcoin", b'"total_holdings"'),
        ("strategy_chart.json", "https://api.coingecko.com/api/v3/public_treasury/strategy/bitcoin/holding_chart?days=365", b'"holdings"'),
    ]
    for name, url, needle in gecko:
        items.append(("gecko/" + name, url, (lambda n: (lambda b: n in b))(needle)))
    for ind in IMF_INDICATORS:
        items.append((
            "imf/%s.json" % ind,
            "https://www.imf.org/external/datamapper/api/v1/" + ind,
            (lambda n: (lambda b: b'"values"' in b[:200] and n.encode() in b[:400]))(ind),
        ))
    items.append((
        "worldbank/SP.POP.TOTL.json",
        "https://api.worldbank.org/v2/country/WLD/indicator/SP.POP.TOTL?format=json&mrv=4",
        lambda b: b'"value"' in b,
    ))
    items.append((
        "blockchain/market-price-4y.json",
        "https://api.blockchain.info/charts/market-price?timespan=4years&format=json",
        lambda b: b'"values"' in b[:300],
    ))
    return items


def fetch(url: str, tries: int = 3) -> bytes:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            if r.status_code == 200 and r.content:
                return r.content
            last = "HTTP %d" % r.status_code
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
        if i + 1 < tries:  # PIPE-282 (GH-34): no wait after the last try
            time.sleep(3 + 3 * i)
    raise RuntimeError(last or "no response")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    ok, failed = [], {}
    gecko_pause = False
    for rel, url, valid in sources():
        path = os.path.join(OUT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if rel.startswith("gecko/"):
            if gecko_pause:
                time.sleep(3)  # the free tier answers 429 to bursts
            gecko_pause = True
        try:
            body = fetch(url)
            if not valid(body):
                raise RuntimeError("unexpected content: " + body[:80].decode("utf-8", "replace").replace("\n", " "))
            with open(path, "wb") as f:
                f.write(body)
            ok.append(rel)
            print("ok      %-34s %7d bytes" % (rel, len(body)))
        except Exception as e:  # noqa: BLE001
            failed[rel] = str(e)
            kept = "kept yesterday's file" if os.path.exists(path) else "no file yet"
            print("FAILED  %-34s %s (%s)" % (rel, e, kept))
    manifest = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": len(ok),
        "failed": failed,
    }
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print("wrote manifest.json: ok=%d failed=%d" % (len(ok), len(failed)))
    # A run with nothing readable at all is a real failure; a few misses are not.
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
