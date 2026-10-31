# ADR 0016: Sharadar's permaticker is the identity authority; survivorship floor 1998

- Status: Accepted
- Date: 2026-10-20
- Implemented by: #55 (`core.security_vendor_xref`), #58 (the vendor master and
  events), #62 and #63 (`fafnir vendor link`, `show`, `accept`, `reject`), #71
  (`fafnir vendor mint-delisted`), #98 (`fafnir vendor feed`, the Sharadar-driven
  sweeps)
- Note (2026-10-30): §1's mutual funds keep FMP. Tiingo is not adopted
  ([ADR 0015](0015-multi-vendor-provenance-and-precedence.md)'s note), so a fund keeps
  its FMP identity and FMP's feed.
- Extends: [ADR 0002](0002-surrogate-security-id-and-bitemporal-readiness.md) (the
  surrogate `security_id`) and [ADR 0005](0005-automatic-universe-maintenance.md),
  whose "right long-term answer", a vendor-independent key, this adopts
- Related: [ADR 0012](0012-warrants-rights-units-out-of-scope.md) (instrument scope),
  [ADR 0015](0015-multi-vendor-provenance-and-precedence.md) (provenance and
  precedence), the [Sharadar adoption plan](../plans/sharadar-adoption.md)

## Context

fafnir's identity is a surrogate `security_id`, and its only link to the outside world
is the ticker in `core.symbol_xref` (ADR 0002). Tickers are reused and renamed, so
ADR 0005 built sweeps that apply renames and delistings to the security that already
holds the history, and named a vendor-independent key as the right long-term answer.
FMP's bulk screener never supplied one.

Sharadar does. Its TICKERS table gives every security it has ever priced a
**permaticker** that survives renames, exchange moves and CUSIP changes, and its
ACTIONS table records the listings, renames and delistings against it. It also
covers issuers that died before fafnir first ran, which FMP's screener cannot
return at all.

What the first two phases measured:

- **The vendor master** (#58, 2026-10-19): 61,146 securities (institutional
  investors left out), and 142,852 lifecycle events.
- **Linking** (#62–#64, 2026-10-20): of 12,888 active, non-fund FMP securities,
  12,265 (95.2%) are linked to a permaticker. The other 623 each carry a reason code:
  302 are second rows of an issuer already linked, the rest no or ambiguous price
  evidence.
- **Reliability:** where FMP's history and Sharadar's disagree on a linked security,
  FMP's is the one that is wrong: bars stored back-adjusted as raw, or another
  instrument's history under the ticker (the 2026-10-20 decision in the plan).
- **History floor:** SEP starts in December 1997, and Sharadar records no delisting
  before 1998. FMP has none either.

## Decision

### 1. The permaticker is the identity authority; `security_id` stays fafnir's key

- **Which key a security is** is recorded by an open link in
  `core.security_vendor_xref` (vendor `sharadar`, `vendor_key` the permaticker).
  Every table keeps keying on `security_id`; nothing outside the xref stores a
  permaticker.
- **From cutover (#98, #122)**, Sharadar's events drive the universe. New listings,
  renames and delistings come from TICKERS and ACTIONS through the xref, not from
  FMP's screener and sweeps.
- **The FMP sweeps stay in the code** (`ingest/security_master.py`,
  `ingest/delisted.py`, `ingest/symbol_changes.py`) for rollback, and for the
  securities Sharadar does not cover (below).
- **How a link is made** is #62's engine and #63's review. The evidence is ticker
  and span, then price, then identifier:
  - the engine links on agreeing prices (`price_history`), on the latest price
    when FMP's history is wrong (`latest_price`), or on a shared CIK or CUSIP
    (`identifier`);
  - a person decides what it cannot (`vendor link accept` / `reject`);
  - the engine never moves a link, manual or not.
- **A security with no link keeps its FMP identity.** That covers the 623 above,
  and every mutual fund: Sharadar prices none (4,588 `not_covered_asset_type`),
  and Tiingo replaces FMP for them (ADR 0015).

### 2. Survivorship floor: 1998-01-01

Cross-sectional research before 1998-01-01 is **survivor-only, under any vendor**:
a universe drawn from the warehouse for an earlier date holds only the issuers that
survived to be listed later. fafnir keeps the bars it has from before 1998 (FMP's
live names go back to 1990), but no backfill can add the issuers that died before
then, and no query should present a pre-1998 cross-section as complete.

### 3. Which dead issuers are minted

`fafnir vendor mint-delisted` (#71) mints a `core.security` row (`source =
'sharadar'`) for a Sharadar security when **all** of these hold:

| Rule | Why |
|---|---|
| It is in `stocks` or `funds` (SEP/SFP), so it has prices | A security with no bars adds nothing a researcher can use |
| `is_delisted`, and `last_price_date >= 1998-01-01` | Live securities come from the screener; before 1998 is below the floor |
| Its category maps to an asset type (`sharadar_map.map_category`): common and preferred stock (domestic, ADR, Canadian), ETF, ETN, ETD, ETMF, CEF | `UNIT` is out of scope (ADR 0012), and `IDX` is not a security |
| It is not a warrant, right or unit by its ticker and name (`instruments.out_of_scope_kind`, `[general] exclude_instruments`), read with and without the number Sharadar appends to a reused dead ticker (`HCMAU1`); a declared symbol is kept | ADR 0012. Sharadar files a SPAC's unit as a common stock's secondary class, so the category alone lets it through: until 2026-10-30 the mint did, and held back 1,149 dead units only because their overlap flags stayed open |
| Its last exchange maps to a screener venue: NYSE, NASDAQ, AMEX (Sharadar's NYSEMKT and NYSEARCA), BATS | The same venues `SCREENER_EXCHANGES` admits for live names; OTC-only lives are excluded |
| No open link holds its permaticker | It is already a fafnir security |
| Overlap detection (#68) does not flag it | It may be a delisted FMP security that did not link; minting it would fork one issuer into two rows |

How each kind is stored follows `sharadar_map` (#56):
- ETN, ETD and ETMF are `etf` with `is_etf`. FMP files them the same way.
- A CEF is `equity` with `is_fund`, as FMP stores closed-end funds.
- A preferred is `equity`.

A minted security is linked to its permaticker at once (`match_method = 'minted'`,
`is_feed`), so FMP never feeds it (ADR 0015 §2), and a later `vendor link` never
evaluates it.

## Consequences

- **Rename and delisting handling changes owner at cutover.** Until then the FMP
  sweeps run as today and Sharadar's events are only stored. A difference between
  the two during the overlap is what #122's dry-run criterion looks for:
  `fafnir ingest sharadar-universe --dry-run` (#93) reports it. It applies
  renames, delistings and new listings through the permaticker's link, and mints a
  live listing only when no fafnir security trades under its ticker (link before
  mint), the live counterpart of §3. A live listing may also be on TXSE, which
  Sharadar lists and FMP's screener cannot reach. It must also pass ADR 0012's
  instrument rule by ticker and name, as the FMP load applies it.
- **4.8% of active non-fund securities stay FMP-identified** after linking: the
  2026-10-20 decision accepts that rather than triage each by hand. Their flags
  (`vendor_xref_unmatched`, `vendor_xref_ambiguous`) say why, and the nightly
  `vendor link --new` links new listings as they appear.
- **Some links are weak by design.** A `latest_price` or `identifier` link may rest
  on one agreeing sample, or none, and its confidence says so. Its disagreeing
  history is the correction worklist (`vendor_history_disagrees`, #75). A wrong one
  is undone with `vendor link reject`.
- **Removing Sharadar** (its licence's deletion clause) means deleting the
  securities it minted (`source = 'sharadar'`) and its links, besides the rows
  ADR 0015 already covers. The FMP identities of linked securities are unaffected.
- **Before 1998 a cross-section is survivor-biased**, and the documentation says so
  where it describes survivorship ([architecture.md](../architecture.md#identity--survivorship)).
