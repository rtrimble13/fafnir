# ADR 0014: A venue transfer is not a delisting

- Status: Accepted
- Date: 2026-10-12
- Implemented by: `src/fafnir/ingest/delisted.py` (`load_delisted`,
  `is_recent_delisting`, `bars_after`, `flag_still_trading`),
  `src/fafnir/db/repository.py` (`nightly_universe`, `upsert_security`,
  `record_venue_override`, `reverse_delisting`, `condition_status`),
  `src/fafnir/ingest/security_master.py` (`TXSE`, `flag_inactive_but_trading`),
  `src/fafnir/ingest/venue_transfer.py`, migration 0027,
  `fafnir security venue-transfer`, `fafnir security delist`
- Related: [ADR 0005](0005-automatic-universe-maintenance.md) (the screener defines
  the universe), [ADR 0012](0012-warrants-rights-units-out-of-scope.md) (the
  identity key)

## Context

On 2026-10-05 six NYSE securities moved their primary listing to the **Texas Stock
Exchange (TXSE)** and kept their tickers: Energy Transfer (ET), Sunoco (SUN),
SunocoCorp (SUNC), USA Compression (USAC), Dillard's (DDS) and Dillard's Capital
Trust I (DDT). The warehouse recorded each as delisted and stopped loading their
prices. Texas Capital (TCBI) moved on 2026-10-08, Origin Bancorp (OBK) moves on
2026-10-13, and more issuers are expected to follow.

Two independent paths freeze a security that has moved:

1. **The delisting sweep stamps it.** FMP's delisted feed reported each move off
   the NYSE as a delisting. The sweep stamped all six on 2026-10-07, closed their
   tickers, and `mark_delisted` is one-way. Its only guard looks for 30 days of
   stored bars after the date, which can never arrive once the price step stops
   loading the security.
2. **The universe load deactivates it.** FMP's screener and profile call a moved
   security `isActivelyTrading: false`. The nightly `ingest securities` copies that
   onto the row, and the nightly price step loaded only `is_actively_trading` rows.
   This freezes a security even if the sweep never fires. TCBI was exposed to it
   from the night of 2026-10-12.

**The vendor's profile is not evidence; its price feed is.** On 2026-10-12 FMP's
profile for ET said `exchange "NYSE"` and `isActivelyTrading false`, while quoting
a price of 20.72 and that day's volume of 7,204,304. FMP had no venue code for TXSE
at all. Its price feed, though, served ET's bars after the move: a close of 20.62
on 5,454,900 shares on 2026-10-08.

**What separates a transfer from a real delisting** in that feed:

- **Not a bar on the delisting date.** FMP's `delistedDate` is often a security's
  last trading day. AMZE, GBTG, BTAI, GWH, AETH and ZSC, all genuine delistings in
  2026-09, have a bar on the date.
- **Bars after the date are rare.** In the warehouse only BURU (an earlier wrong
  delisting) and CSAN had them.
- **Volume does not separate the cases.** ET traded 7.2M shares on its first day
  after the move against a 7.6M median, but USAC traded 18% of its median. GRP-UN,
  which left the NYSE for OTCQX on 2025-12-31 (a real delisting from this
  warehouse's scope), traded 25% of its median, on 13 bars in about 15 sessions.

So bars after the date mean "do not stamp automatically; ask".

## Decision

1. **The sweep asks the price feed (prevention).** Before stamping, in order:
   - **A flag already settles it.** A `security_delisting_contradicted` flag whose
     `record_key` is this feed record (`{"symbol", "delisted_date"}`) declines it
     without asking. An accepted flag means an operator recorded the move
     (`known_transfer`). An open one means an operator has yet to decide
     (`pending_decision`); this holds past the window below, so an undecided record
     is never stamped by default.
   - **Recent records only.** For a record within 60 days of today, with the
     security's last stored bar within 10 sessions of the date, the sweep fetches
     the price feed from about 10 sessions before the date to today: one request,
     with the price step's client.
     - If the vendor has **any bar strictly after the date**, the record is declined
       (`still_trading`) and flagged: `security_delisting_contradicted`, warn, with
       the bars after and the volumes either side in `detail`.
     - If the request fails, it is declined for the night (`fetch_failed`). The feed
       keeps the record for weeks.
     - Otherwise it is marked as before.

   **Cost.** The nightly sweep has marked 1 to 22 securities a night, so it asks
   about that many at most. A `--full` sweep (4,041 candidates in 2026-09) asks only
   about the recent tail.

   **Records.** The run's `params` record `declined` per reason and
   `prices_requested`. `rows_quarantined`, which marks the run `partial` for
   `scripts/monitor.sh`, counts every decline that wants a look. It leaves out
   `known_transfer`, which repeats nightly for as long as the feed keeps the old
   record and needs nothing.

2. **The price universe keeps a recently trading security.** Every nightly universe
   (`ingest prices`, and the corporate-action universes) is "listed, and either
   actively trading or with a bar in the last 30 days" (`nightly_universe`).
   - On 2026-10-12 that added 9 securities to the 16,117 polled. The other 1,734
     inactive, listed rows had no bar in 30 days.
   - A real wind-down stops being polled once FMP has served no bar for 30 days.
   - When the universe load turns a row inactive while its last bar is within 5
     sessions, it raises an info `security_inactive_but_trading` flag: the early
     warning of a move, before the delisted feed reports it.

3. **Two commands record the operator's decision (repair).** Each is a repair an
   agent may run after showing the dry run and getting an explicit yes, like
   `security merge`. Neither deletes anything, and each can be undone with the
   other.
   - **`security venue-transfer <id> --venue TXSE --date <first session>`**
     records a move, whether or not the sweep has stamped it.
     - It reverses a stamped delisting, re-opening the ticker period the delisting
       closed rather than inserting one: the ticker never changed hands.
     - It sets the venue under decision 4, and marks the security active.
     - It accepts the contradiction flag for each feed record the move settles.
       Those are the stamped delisting, and one dated `--date`, which is how FMP
       dated ET's record. The sweep then declines them unasked.
     - It refuses a ticker another listed security holds, and one the vendor has no
       bar for after `--date`.
     - The next nightly `ingest prices` resumes from the watermark, so the bars
       since the move load before `dq run` and raise no `gap` flags.
   - **`security delist <id> --date <date>`** confirms a delisting the sweep
     declined, such as a move to OTC. It stamps the security as the sweep would
     and resolves its flags.

4. **TXSE is fafnir's own venue code, held against the nightly load.**
   - **The code.** `TXSE` normalizes from the aliases FMP may adopt and is a
     listed US venue.
   - **Kept off the screener.** It is not in `SCREENER_EXCHANGES` until FMP's
     screener accepts a code for it; the TODO there says how to check. The sweep
     still reads feed rows naming it.
   - **The override.** The upsert would otherwise write FMP's `NYSE` back the next
     night. So `venue-transfer` stores the venue in `core.security.venue_override`
     (migration 0027), with the venue it left in `venue_override_left`. While it is
     set, the nightly upsert keeps the venue, and keeps the row's activity: FMP
     calling a moved security inactive on the venue it left is the same stale
     view. Once FMP names any other venue, it has caught up, and the override
     clears itself.
   - **Calendar.** TXSE keeps the US equity holiday calendar. The DQ checks judge
     every security against the run's `--exchange` calendar (NASDAQ in the
     nightly), so TXSE needs no calendar of its own.

**Naming.** TXSE is Texas. **TSX** is FMP's code for the *Toronto* Stock Exchange,
and a screener query for `TSX` would pull in Toronto's listings, so `TSX` is used
nowhere for Texas.

## Why an operator in the loop

The bars cannot tell a move between listed venues from a move to OTC, and nothing
else FMP serves can either. GRP-UN's OTC bars look like USAC's TXSE bars.

The sweep therefore leaves the security trading and asks. Wrongly keeping a
security active is the cheaper error: it stays visible as an open flag and a
`partial` run, and the security keeps loading. Wrongly stamping one is the silent
error this ADR exists to stop.

The cost is one decision per move (`venue-transfer` or `delist`), taken from a dry
run that shows the vendor's bars. A move announced in advance can be recorded as
soon as the vendor serves a bar after the move date. Until then the sweep and the
price universe keep the security loading on their own.

## Consequences

- **The six need a `venue-transfer` each**, with `--date 2026-10-05`, after
  `fafnir db migrate` applies 0027. TCBI (`--date 2026-10-08`) and OBK
  (`--date 2026-10-13`) are recorded the same way, whether or not they have been
  stamped or turned inactive by then. Then `db refresh-marts`.
- **A feed record dated otherwise than the move** raises one flag the night the
  sweep sees it. The record is settled by running `venue-transfer` again, or by
  `dq accept`.
- **Funds.** A fund recorded under MUTF is not on a listed venue. The sweep never
  reads a security's recorded venue: funds stay out of it because the feed's own
  rows for them carry a venue the sweep does not read. The comment in
  `ingest/tracked.py`, and migration 0019's, said the recorded venue was what
  protected them. The code comment is corrected. Migration 0019 is left as written,
  because applied migrations are checksummed.
- **Out of scope.**
  - Classifying TXSE in sector or market-structure reporting; venue is a security
    attribute only.
  - Anything before the move dates; the NYSE and NASDAQ histories are intact.
  - Dual listings. FMP's feeds give one bar per ticker, so a dual-listed company
    keeps one row.
