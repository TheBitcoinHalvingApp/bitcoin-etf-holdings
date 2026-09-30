# Bitcoin ETF holdings

How many bitcoin each US spot bitcoin ETF holds, read every morning from the
issuers' own published holdings files and pages. No third-party trackers.

- `etf.json`: today's figures. Apps read this file at its raw URL.
- `history.csv`: one line a day, from the first morning the job runs.
- `history_quarterly.csv`: the exact bitcoin held by every fund at each quarter
  end since 2024, from the trusts' own 10-Q and 10-K filings on SEC EDGAR
  (`backfill_edgar.py`). Refreshed on the 1st and 16th of each month so a new
  filing is picked up within two weeks.
- `etf_holdings.py`: the daily script. `.github/workflows/etf.yml` runs both.

Covered: all eleven US spot funds. Eight are read straight from the issuer
(IBIT, FBTC, ARKB, BITB, HODL, BRRR, EZBC, BTCO). Grayscale GBTC and BTC and
WisdomTree BTCW are tried the same way; when their bot checks refuse the runner,
the script estimates them from the trusts' own SEC filings (bitcoin per share
from the latest 10-Q, less the daily fee since, times shares outstanding) and
marks them `"estimated": true`. The anchors for those estimates come from
EDGAR and from `history_quarterly.csv`, so a new filing updates them on its own;
nothing to type. Hashdex DEFI was liquidated in August 2026.

If a fund cannot be read on a given morning, its last good figure is carried
forward with its own date and marked `"stale": true`.

## Setup, once

1. Create a new public repository and upload everything in this folder,
   including the hidden `.github` folder.
2. Settings > Actions > General > Workflow permissions: choose
   "Read and write permissions", Save. (The job commits the file; it needs this.)
3. Actions tab > "Bitcoin ETF holdings" > Run workflow, once, to check it works.
4. The raw file is then at
   `https://raw.githubusercontent.com/<account>/<repo>/main/etf.json`
   Put that address in the app.

After that it runs itself at 6:20 am Pacific. If an issuer changes its page the
job fails and GitHub emails the account; the app keeps showing the last good
figures and their date.
