# Bitcoin ETF holdings
<!-- PIPE-282 (GH-18): brought up to date with the 6 pm pull, flows.json, the NYSE calendar and the force input -->

How many bitcoin each US spot bitcoin ETF holds, read each weekday at 6 pm New
York time from the issuers' own holdings files, pages, APIs and workbooks. No
third-party trackers.

- `etf.json`: the latest read. `holdings_date` is the trading day the figures
  reflect (the NYSE trading day before the read) and `read_date` the New York
  date of the read. Each fund carries `as_of` (the issuer's own date, in its own
  convention), `"estimated": true` when its figure is worked out from SEC
  filings and the exchange's share count, and `"stale": true` when its last
  good figure is carried forward (with `error` when the read failed). Apps read
  this file at its raw URL.
- `flows.json`: `days`, each trading day's change in bitcoin held over the funds
  read fresh on both days (the last 60), and `periods`: years since 2024, this
  year's quarters and months, and the last 7 and 30 days once the record is that
  long. Each period says how many funds it covers (`funds`).
- `history.csv`: one row per NYSE trading day, written only by a read between 5
  and 8 pm New York time. `flags` marks funds carried forward (`=s`) or
  estimated (`=e`); day-based figures leave those out. GBTC and BTC are
  rewritten on every run from Grayscale's own workbooks.
- `history_quarterly.csv`: the exact bitcoin held by every fund at each quarter
  end since 2024, from the trusts' own 10-Q and 10-K filings on SEC EDGAR
  (`backfill_edgar.py`). Refreshed on the 1st and 16th of each month so a new
  filing is picked up within two weeks. A quarter end counts as exact only once
  every fund has filed for it; until then the daily reads fill in.
- `etf_previous.json`, `etf_baseline.json`: working files of the daily script.
- ARKB split its shares about 3 for 1 between 2024 and 2025, so its `shares_outstanding` in
  `history_quarterly.csv` mixes pre- and post-split counts; the bitcoin column is unaffected.
  Year-end rows carry the filing that last restated them (sometimes a later 10-Q).
  <!-- TIDY-285 (GH-31) -->
- `etf_holdings.py`: the daily script. `.github/workflows/etf.yml` runs the
  three scripts.
- `mirror/`: a daily copy of every slow-changing source the app's Dollar,
  Market and World pages read (FRED, the Treasury, the IMF, the World Bank,
  CoinGecko, Blockchain.com), each file the source's own response unchanged,
  written by `mirror_daily.py`. `mirror/manifest.json` carries the time of the
  last run; the app reads the copies when that is under three days old and the
  live sources otherwise. A source that fails on a given day keeps its previous
  file and is listed under `failed`.

Read from the issuer each day: IBIT (iShares holdings file), FBTC (Fidelity's
research API), ARKB (ARK's file), BITB (bitbetf.com), HODL (VanEck), BRRR
(CoinShares), EZBC (Franklin Templeton), BTCO (Invesco, in headless Chromium),
GBTC and BTC (Grayscale's daily performance workbooks: shares outstanding times
bitcoin per share; when Grayscale has not posted a day by 6 pm, the day before is
carried and the next run fills it in). Estimated each day: WisdomTree BTCW and
Morgan Stanley MSBT, whose sites refuse automated readers: bitcoin per share from
the latest 10-Q (`history_quarterly.csv`), less the fee since, times the shares
outstanding from Nasdaq (market cap divided by the price it used). Fidelity
falls back to the same estimate when its own figures cannot be read. Hashdex
DEFI was liquidated in August 2026.

Any other fund that cannot be read keeps its last good figure, with its own date,
marked `"stale": true`.

## Schedule

`.github/workflows/etf.yml` pulls the ETFs at 6:04 pm New York time on weekdays:
GitHub's schedules are in UTC, so there are two (22:04 and 23:04 UTC) and a gate
job lets through the one that is 6 pm in New York that day. Nothing is written on
NYSE holidays. The mirror runs at 13:20 and 01:30 UTC, the SEC refresh on the 1st
and 16th.

A manual run (Actions tab > "Bitcoin ETF holdings" > Run workflow) outside 5 to 8
pm New York time, or on a holiday, reads everything and writes nothing. Tick
"force" only for a run whose figures you know are right.

## Setup, once

1. Create a new public repository and upload everything in this folder,
   including the hidden `.github` folder.
2. Settings > Actions > General > Workflow permissions: choose
   "Read and write permissions", Save. (The job commits the file; it needs this.)
3. Actions tab > "Bitcoin ETF holdings" > Run workflow, once, to check it works.
4. The raw file is then at
   `https://raw.githubusercontent.com/<account>/<repo>/main/etf.json`
   Put that address in the app.
5. Settings > Secrets and variables > Actions > New repository secret: name
   `SEC_USER_AGENT`, value a name and an email address you read (the SEC asks
   automated readers for a contact). Without it the scripts send a placeholder
   address, which the SEC may refuse.

After that it runs itself. A fund that cannot be read does not fail the run: it
keeps its last good figure, shown dimmed in the app with its date, and the run
log says why. A failed SEC refresh or mirror pass fails the run (GitHub emails
the account) after everything else has been committed.
