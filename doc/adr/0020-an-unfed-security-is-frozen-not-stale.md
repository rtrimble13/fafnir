# ADR 0020: A security nothing feeds is frozen, not stale

- Status: Accepted
- Date: 2026-11-10
- Implemented by: migration 0045 (`core.security.feed_frozen_on`,
  `feed_frozen_note`; `mart.v_security_profile`), `src/fafnir/db/repository.py`
  (`unfed_securities`, `freeze_feed`, `thaw_feed`, `frozen_securities`),
  `fafnir security freeze | thaw`, `scripts/daily_update.sh` (the Sharadar night),
  `src/fafnir/dq/checks.py` (`check_freshness`), `src/fafnir/dq/recheck.py`
  (the `stale` negation), `src/fafnir/dq/compare.py` (the `inactive` state),
  `src/fafnir/dq/dorq.py` (`drop_not_trading`), `src/fafnir/ingest/vendor_feed.py`
- Related: [ADR 0014](0014-venue-transfer-is-not-a-delisting.md) (a security that
  trades is not delisted), [ADR 0015](0015-multi-vendor-provenance-and-precedence.md)
  (one feeding vendor per security; `fmp_fed`), the FMP tier decision (#128)

## Context

On 2026-11-01 the FMP subscription was reduced (#128), and on 2026-11-05 the
operator chose to keep FMP as a backup feed only. Between 2026-11-08 and 2026-11-10
`[general] fmp_secondary` was set false, and the Sharadar night stopped running
FMP's loaders. What FMP alone fed (`repo.fmp_fed`) took its last bar on
2026-11-06: 3,937 open-end mutual funds, which Sharadar does not carry at all
(its fund table has one `MF` row, delisted in 2011), and 7 stocks with no Sharadar
link.

Those securities still trade. A fund strikes a NAV every session; the 7 stocks
print on their venues. The warehouse has simply stopped receiving them.

Nothing in the schema could say that:

- **Delisting them would be false.** `delisted_date` is a market fact, and the
  delisting sweep is built to refuse a record the price feed contradicts (ADR 0014).
  It would also close their ticker periods, and drop them from the screener's
  default view, where a fund that still exists belongs.
- **Turning `is_actively_trading` off would be false too**, for the same reason,
  and the nightly security-master load would copy the vendor's `true` back.
- **Leaving them as they are made them stale.** `check_freshness` measures a feed
  that should reach an actively trading security and does not. With no marker it
  would have written one `stale` flag per fund on the third session after the last
  bar: about 3,937 flags on the 2026-11-11 night, a 37% jump in the queue, every
  one of them a restatement of one decision nobody could act on. dorq's stale-feed
  check (DQ304) would have reported the same, and `dq compare` would have counted
  both as recall.
- **Accepting the flags afterwards** would have held (a `stale` flag is keyed on
  `last_date`, which does not move), but at the cost of 3,937 accepts recording a
  fact about the feed as if it were a fact about each security, and with the
  screener still reading them as live names priced on 2026-11-06.

## Decision

1. **The marker.** `core.security.feed_frozen_on DATE` is the last session the
   warehouse holds a bar for when its feed stopped: the day its history ends.
   `feed_frozen_note TEXT` says why, and is set with it, never alone (a CHECK). The
   security's other columns are untouched: it stays actively trading, listed, with
   an open ticker period, and the screener lists it with the marker beside it
   (`feedFrozenOn`); `mart.v_security_profile` carries both columns.

2. **Who stamps it.** `fafnir security freeze --unfed` marks every security
   `fmp_fed` and in the nightly universe that is not marked yet, with the note
   `fmp_secondary=false: no vendor feeds this security`. It refuses while
   `fmp_secondary` is true, or in FMP mode: those securities are fed. The Sharadar
   night runs it on the `fmp_secondary = false` branch, every night, so a security
   that becomes unfed later (a link rejected, a Sharadar feed moved back) is marked
   the night it does; it stamps nothing twice.

3. **What clears it.** A fed security is not frozen. `vendor feed` to another
   vendor and a feeding link (`insert_vendor_link(is_feed=True)`, which `vendor link
   accept` and the dead-issuer mint use) clear the marker on the security they feed.
   `fafnir security thaw --unfed` clears every marker the rule stamped, and the
   night runs it on the `fmp_secondary = true` branch before FMP's loaders, so
   turning FMP back on is one config change. `security thaw <id>` clears one.

4. **What reads it.** The freshness check skips a frozen security
   (`WHERE s.is_actively_trading AND s.feed_frozen_on IS NULL`). Its negation in
   `dq recheck --check stale` closes a `stale` flag whose security is frozen, so the
   recheck, not a resolve, is how the flags written before the marker go. `dq
   compare` judges a `stale` flag on a frozen security as `inactive`, not holding,
   and dorq's stale-feed drop (`drop_not_trading`) leaves out DQ304 on frozen
   securities as it does on inactive ones, so the shadow and the SQL checks keep the
   same rule. Nothing else reads it: a frozen security's bars are judged for bad
   prints, splits and gaps as before, within the history it holds.

5. **Coverage is honest.** `mart.v_security_price_coverage` ends at the last bar, as
   it always did; `feed_frozen_on` says that end is the feed's, not the market's.
   A reader of a frozen fund's series sees it stop on 2026-11-06 and can read why.

## Consequences

- The 3,937 funds and 7 stocks are marked on the first night after this is
  deployed with `fmp_secondary = false`, and the `stale` flags they would have
  raised are never written. Any written between the flag's flip and the deploy
  close on the next `dq recheck --check stale`.
- A frozen fund that FMP later serves again (the tier restored, `fmp_secondary`
  back to true) thaws on the next night and loads from its watermark; the gap in
  its history between the freeze and the thaw is a real feed gap, and the gap
  check reports it as such.
- `fmp_fed` is unchanged: a frozen security is still FMP's to feed if FMP's loaders
  are ever run for it, and still refused to every other vendor's per-symbol pull.
- A new `stale` flag on a security with the marker is a bug: the check and the
  marker disagree, and the test that pins the predicate into the check's SQL is
  the first place to look.
