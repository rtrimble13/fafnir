# Fundamentals

A researcher's guide to the statements fafnir holds and the factors it derives from them:
what is there, how to read it as it was known on a date, and the rules that keep a backtest
free of look-ahead. The rules themselves are [ADR 0018](adr/0018-bitemporal-fundamentals.md)
(the statements in time) and [ADR 0019](adr/0019-derived-metrics-not-vendor-metrics.md) (the
derived metrics). The columns are in the
[data dictionary](data_dictionary.md#fundamentals-migration-0035-adr-0018), the loader in
[ingestion.md](ingestion.md#statements-from-sf1-90), and the design in
[plans/fundamentals-sf1.md](plans/fundamentals-sf1.md).

From the command line, `duk fs` prints a statement as known on a date and `duk fa` a
security's factor row ([duk.md](duk.md#fs-and-fa--statements-and-factors-point-in-time)).
An agent reads the same rows through `statements_as_of` and `factor_row` (#107).

## What the warehouse holds

**The statements** are Sharadar SF1's, in three tables: `core.income_statement`,
`core.balance_sheet` and `core.cash_flow_statement`. Readers reach them through mart.
- **Which rows.** The first-reported rows (SF1's ARQ, ARY) and the restated ones (MRQ, MRY), for
  every security linked to Sharadar, dead issuers included. SF1's trailing rows (ART, MRT) are
  not read: the mart computes trailing twelve months from quarters.
- **Which items.** 58 statement items (income 19, balance sheet 26, cash flow 13), typed and
  named in `ref.statement_line_item`. SF1's other columns (its vendor metrics such as P/E and
  EV, per-share values and `*usd` conversions) are kept in `extra` and never read by mart.
  Every ratio is derived from the statements and the warehouse's own prices (ADR 0019).
- **Absent and zero.** SF1 writes 0 for an item a filing does not carry and NULL for one that
  does not apply, such as a bank's current assets. The warehouse keeps both as given, so a
  NULL is never a zero, and a ratio's denominator rule handles the zeros.
- **The primary share class only.** SF1 files an issuer's statements under one class (GOOGL,
  not GOOG; BRK.B, not BRK.A). A secondary class holds none. `duk` and the MCP tools answer it
  with the primary class through the shared CIK, and say so.
- **Currency.** Amounts are in the reporting currency (`reported_currency`). A ratio of a
  statement figure to a price in another currency is NULL (`currency_mismatch`).
- **Share terms.** A share count is stored in the share terms of the day its version became
  known. A later split therefore changes nothing stored, and the panel puts every count in its
  own date's terms with the warehouse's splits (ADR 0019 §3).

**Versions, on two timelines** (ADR 0018). A row describes a period (`fiscal_date`), is one
version of it (`version_no`), and holds over a knowledge interval, `known_from` to `known_to`,
half-open. How `known_from` was set is `knowledge_source`:

| `knowledge_source` | The version is | Known from |
|---|---|---|
| `filed` | the first report of the period (version 1) | its SEC filing date |
| `observed` | a restatement: a restated row that differed from the version in force | the day fafnir first loaded it |
| `estimated` | a period SF1 has no first-reported row for (a late filing, a coverage edge) | the issuer's next filing after the period |

A vendor correction is on the other timeline. When SF1 fixes its record of a filing, the old
row is superseded (`superseded_at`) and the corrected one takes its version and interval. The
filing did not change, so what was known when did not either.

### Coverage

- **The early 1990s to today in `core`.** SF1 reaches back to 1991 in the probe's sample,
  and every year loads: the first panel years need the years before them for trailing twelve
  months and growth.
- **1998 onward in the panel.** `mart.fundamental_panel` and every cross-section the mart
  presents as complete start on 1998-01-01. That is the survivorship floor
  ([ADR 0016](adr/0016-sharadar-identity-and-survivorship-floor.md)): from it, the warehouse
  holds the issuers that died as well as those that lived. Before it, a cross-section is the
  survivors only, and a factor's history there would be survivorship-biased.
- **By decade.** The production counts are recorded on #92 after the backfill, from section 3
  of `scripts/fundamentals_report.sql`. A mart reader can count the securities and periods
  for itself:

```sql
-- Securities and annual periods per decade, as known today, by how each became known.
SELECT (extract(year FROM fiscal_date)::int / 10) * 10    AS decade,
       count(DISTINCT security_id)                        AS securities,
       count(*)                                           AS periods,
       count(*) FILTER (WHERE knowledge_source = 'filed')     AS filed,
       count(*) FILTER (WHERE knowledge_source = 'observed')  AS observed,
       count(*) FILTER (WHERE knowledge_source = 'estimated') AS estimated
  FROM mart.v_income_statement_versions
 WHERE period_type = 'annual' AND superseded_at IS NULL
   AND known_from <= current_date AND (known_to IS NULL OR current_date < known_to)
 GROUP BY 1 ORDER BY 1;
```

## Reading point in time

One rule answers "what was known on date D": a version is **in force** at D when it is
current on the load timeline and D falls in its knowledge interval:

```sql
superseded_at IS NULL AND known_from <= D AND (known_to IS NULL OR D < known_to)
```

Every point-in-time read in the mart applies it. Pick the read for the question:

| Question | Read |
|---|---|
| Each security's latest period, as known on D | `mart.fn_statements_as_of(D)`: one row per security and period type, the three statements joined on `fiscal_date` |
| One security's history, as known on D | the versions views (`mart.v_income_statement_versions`, `…_balance_sheet_…`, `…_cash_flow_statement_…`) with the rule above; `duk fs --as-of D` |
| Trailing twelve months, as known on D | `mart.fn_fundamentals_ttm(D)`: four contiguous quarters, or NULL, never a partial sum |
| The factors, as known on D | `mart.fundamental_panel` at a month-end; `mart.fn_fundamental_snapshot(D)` at any date |
| The warehouse as it stood when a past run read it | `mart.fn_statements_as_of(D, load_as_of)`: rows loaded by then, a later correction ignored |

```sql
-- What the market could know about one company on 2015-06-30: each fiscal year at its
-- version in force that day (a later restatement does not show).
SELECT fiscal_date, version_no, known_from, knowledge_source, revenue, net_income
  FROM mart.v_income_statement_versions v
  JOIN mart.v_symbol_lookup x ON x.security_id = v.security_id AND x.valid_to IS NULL
 WHERE x.symbol = 'AAPL' AND v.period_type = 'annual'
   AND v.superseded_at IS NULL AND v.known_from <= DATE '2015-06-30'
   AND (v.known_to IS NULL OR DATE '2015-06-30' < v.known_to)
 ORDER BY fiscal_date;

-- The whole cross-section's latest annual period on that day.
SELECT * FROM mart.fn_statements_as_of(DATE '2015-06-30') WHERE period_type = 'annual';
```

`mart.v_statements_current` and `mart.v_fundamentals_ttm` are the two functions at today: for
screens, never for a backtest, since they know every restatement since.

### The look-ahead rules

1. **Bound knowledge, not periods.** A filter on `fiscal_date <= D` lets in a period filed
   weeks after D, and restated values loaded years later. Always apply the in-force rule at D.
2. **Restatements are known from the day fafnir loaded them.** SF1 does not date a
   restatement: its restated row's `date` is the period end, before even the original filing,
   and `lastupdated` is a restamp. So fafnir dates one from the day it first saw it
   (`observed`). This is the **as-first-reported convention**: up to the backfill day
   (2026), every period reads as first reported. A restatement the backfill found is known
   from the backfill day, and every later one from the night it arrived.
3. **What that does to growth.** Growth factors compare versions in force at one date. Across
   history they compare first reported with first reported, which is what a reader at the time
   saw. After a restatement loads, the restated periods compare with each other. Where a company
   reclassified a line, the two years are on different bases, and the growth figure inherits
   that.
4. **`estimated` knowledge dates are inferred.** A period SF1 has no first report of is dated
   from the issuer's next filing. A strict backtest can exclude them:
   `knowledge_source <> 'estimated'` (it is in every panel row).
5. **A reporting lag is one argument.** A filing date is when the market *could* know, not
   when a data feed carried it. `mart.fn_fundamental_panel(dates, extra_lag_days)` reads the
   statements as known `extra_lag_days` before each date, against the date's own prices.
6. **Prices and share counts at the date.** The panel's market cap is the raw close at the
   date times a share count put in that date's terms. A count in today's terms against a 1999
   price would put Apple's market cap 112 times too high.
7. **Keep the dead in the universe.** The panel holds every security with statements that
   traded in the week to each date, including those that later died. A filter on today's `is_actively_trading`
   or `delisted_date IS NULL` throws them out and biases every return upward.
8. **Join statements on `fiscal_date`,** never on knowledge dates: the three statements of
   one period can become known on different days.
9. **Classification is today's.** `sector_name` and `industry_name` are the current
   classification, not as it was at the date: the feeding vendor's
   (`core.v_security_classification`, #109), Sharadar's for a security Sharadar feeds.

**Deferred: an estimated restatement timeline.** One research mode would date each restated
value from the filing of the same period a year later, giving growth rates a consistent basis.
It risks a look-ahead SF1 cannot test, because SF1 keeps only the latest restated value. The
stored filing dates are enough to compute it later, so it needs no schema now
([plan](plans/fundamentals-sf1.md), *Deferred, not built*).

## The factor panel (#100)

`mart.fundamental_panel` holds, at the last trading day of each month from 1998, one row per
security that traded that week: its factors from the statements known then. The columns are in
the [data dictionary](data_dictionary.md#the-factor-panel-materialized-view-and-functions); the
design is the plan's [§5.3 and §5.8](plans/fundamentals-sf1.md#58-as-built-the-factor-panel-100).
For another set of dates, or a conservative reporting lag, call the function the panel is built
from:

```sql
-- Month-ends of 2020, statements as known 60 days before each.
SELECT * FROM mart.fn_fundamental_panel(
    ARRAY(SELECT DISTINCT as_of_date FROM mart.fundamental_panel
           WHERE as_of_date BETWEEN '2020-01-01' AND '2020-12-31'), 60);
-- One date.
SELECT * FROM mart.fn_fundamental_snapshot('2020-06-15');
```

### By sector, and now

- **`mart.v_industry_fundamentals`** summarizes the panel per month-end, by sector and by
  sector and industry: how many securities, and for sixteen of the factors their count and
  quartiles. A group of fewer than three securities has no row, and a factor with fewer than
  three values has no quartiles, so a median of two companies is never read as a sector norm.
  The [data dictionary](data_dictionary.md#martv_industry_fundamentals--the-panel-by-sector-and-industry-view)
  lists the factors.
- **`mart.security_fundamentals_latest`** is each security's row as of the last refresh, with
  its symbol and name: for screens. `mart.security_latest`, which duk's screener reads, is
  unchanged.

### What a row reads

- **The basis.** The security's latest period known at the date: the trailing twelve months of
  four contiguous quarters, or its fiscal year where that is newer. The TTM wins a tie. Flows
  (revenue, earnings, cash flows) are the basis's; stocks (assets, debt, equity, shares) are at
  its period end. A basis that ended more than two years before gives no row.
- **Year-ago and three-years-ago.** The same basis labelled four and twelve quarters back, as
  known at the same date. An *average* balance is the mean of the basis's and the year-ago's,
  and NULL without the year-ago.
- **Price.** The raw close on the date, or the last one in the week before it.
- **Share terms.** Share counts, EPS and dividends per share are put in the date's terms with
  the warehouse's own splits, the ones its raw prices follow (ADR 0019). Market cap is the raw
  close times that count. A 1999 count in today's terms, against 1999's raw price, would put
  Apple's market cap and P/E 112 times too high.
- **Signs are SF1's.** Capital expenditure, dividends paid and buybacks are negative cash flows,
  so the formulas below negate them.

### The rules every ratio follows

- **Value factors are yields,** fundamental over price, so a loss is a negative number on the
  same scale rather than a meaningless negative multiple.
- **The denominator rule** (`mart.fn_ratio`, `mart.fn_growth`): a NULL, zero or negative
  denominator gives NULL. A numerator keeps its sign.
- **Nothing is winsorized, clipped or filled.** Outliers are a research decision, made on the
  raw panel.
- **Currency mismatch.** Where the statements are not in the trading currency, every ratio of a
  statement figure to the price is NULL, as are enterprise value and the Altman Z.
- **Enterprise value** is market cap plus total debt less cash. It leaves out minority interest
  and preferred stock: SF1 carries no balance for either.

### Definitions

`mcap` is market cap, `EV` enterprise value, `avg` the mean of the basis's and the year-ago's
balance, `p4` the year-ago period and `p12` the period three years back.

| Column | Definition | After |
|---|---|---|
| `earnings_yield` | net income to common / mcap | Basu (1977) |
| `book_to_price` | parent equity / mcap; NULL when equity ≤ 0 | Fama & French (1992) |
| `sales_to_price` | revenue / mcap | Lakonishok, Shleifer & Vishny (1994) |
| `cash_flow_to_price` | operating cash flow / mcap | Lakonishok, Shleifer & Vishny (1994) |
| `fcf_yield` | (operating cash flow + capex) / mcap | |
| `ev_to_ebitda` | EV / (EBIT + depreciation and amortization) | Loughran & Wellman (2011) |
| `ev_to_sales`, `ev_to_ebit`, `ev_to_fcf` | EV / revenue, EBIT, free cash flow | |
| `roe` | net income to common / avg parent equity | Fama & French (2015) |
| `roa` | net income / avg total assets | |
| `roic` | (EBIT − income tax) / (total debt + parent equity − cash) | |
| `gross_profitability` | gross profit / total assets | Novy-Marx (2013) |
| `gross_margin`, `operating_margin`, `net_margin` | gross profit, operating income, net income / revenue | |
| `asset_turnover` | revenue / avg total assets | |
| `accruals` | (net income − operating cash flow) / avg total assets | Sloan (1996); Hribar & Collins (2002) |
| `net_operating_assets` | (total debt + parent equity − cash) / `p4` total assets | Hirshleifer, Hou, Teoh & Zhang (2004) |
| `debt_to_equity` | total debt / parent equity | |
| `net_debt_to_ebitda` | (total debt − cash) / EBITDA | |
| `interest_coverage` | EBIT / interest expense | |
| `current_ratio`, `quick_ratio` | current assets, and current assets − inventory, / current liabilities | |
| `altman_z` | 1.2 WC/TA + 1.4 RE/TA + 3.3 EBIT/TA + 0.6 mcap/TL + 1.0 revenue/TA | Altman (1968) |
| `piotroski_f` | nine signals, below | Piotroski (2000) |
| `revenue_growth_yoy`, `net_income_growth_yoy` | basis / `p4` − 1 | |
| `eps_growth_yoy` | EPS / `p4` EPS − 1, both in the date's share terms | |
| `revenue_cagr_3y` | (revenue / `p12` revenue)^(1/3) − 1 | Lakonishok, Shleifer & Vishny (1994) |
| `asset_growth_1y` | total assets / `p4` total assets − 1 | Cooper, Gulen & Schill (2008) |
| `capex_to_sales` | −capex / revenue | Titman, Wei & Xie (2004) |
| `net_share_issuance_1y` | ln(shares / `p4` shares), both in the date's terms | Pontiff & Woodgate (2008); Daniel & Titman (2006) |
| `dividend_yield_ttm` | dividends per share with an ex-date in the last 365 days, as declared (ADR 0001) and in the date's terms, / raw close | |
| `buyback_yield_ttm` | −net equity issuance / mcap | Boudoukh, Michaely, Richardson & Roberts (2007) |
| `shareholder_yield` | −(dividends paid + net equity issuance) / mcap | Boudoukh, Michaely, Richardson & Roberts (2007) |

**The F-score's nine signals,** one point each, against the year-ago period: net income above
zero; operating cash flow above zero; ROA (net income / total assets) up; operating cash flow
above net income; long-term debt / total assets down; current ratio up; shares not up (in the
same terms); gross margin up; asset turnover up. NULL unless both years have positive total
assets, current liabilities and revenue.

### What it does not settle

- **Growth and classification** are as the look-ahead rules above say: growth compares versions
  in force at one date (rule 3), and `sector_name`, `industry_name` and `exchange_code` are
  today's (rule 9).
- **Staleness beyond two years** is the only filter. A stricter one is a research choice:
  `fiscal_date` and `days_since_filing` are in every row.

### References

- Altman, E. (1968). Financial ratios, discriminant analysis and the prediction of corporate
  bankruptcy. *Journal of Finance* 23(4).
- Basu, S. (1977). Investment performance of common stocks in relation to their price-earnings
  ratios. *Journal of Finance* 32(3).
- Boudoukh, J., Michaely, R., Richardson, M. and Roberts, M. (2007). On the importance of
  measuring payout yield. *Journal of Finance* 62(2).
- Cooper, M., Gulen, H. and Schill, M. (2008). Asset growth and the cross-section of stock
  returns. *Journal of Finance* 63(4).
- Daniel, K. and Titman, S. (2006). Market reactions to tangible and intangible information.
  *Journal of Finance* 61(4).
- Fama, E. and French, K. (1992). The cross-section of expected stock returns. *Journal of
  Finance* 47(2).
- Fama, E. and French, K. (2015). A five-factor asset pricing model. *Journal of Financial
  Economics* 116(1).
- Hirshleifer, D., Hou, K., Teoh, S. H. and Zhang, Y. (2004). Do investors overvalue firms with
  bloated balance sheets? *Journal of Accounting and Economics* 38.
- Hribar, P. and Collins, D. (2002). Errors in estimating accruals: implications for empirical
  research. *Journal of Accounting Research* 40(1).
- Lakonishok, J., Shleifer, A. and Vishny, R. (1994). Contrarian investment, extrapolation, and
  risk. *Journal of Finance* 49(5).
- Loughran, T. and Wellman, J. (2011). New evidence on the relation between the enterprise
  multiple and average stock returns. *Journal of Financial and Quantitative Analysis* 46(6).
- Novy-Marx, R. (2013). The other side of value: the gross profitability premium. *Journal of
  Financial Economics* 108(1).
- Piotroski, J. (2000). Value investing: the use of historical financial statement information
  to separate winners from losers. *Journal of Accounting Research* 38.
- Pontiff, J. and Woodgate, A. (2008). Share issuance and cross-sectional returns. *Journal of
  Finance* 63(2).
- Sloan, R. (1996). Do stock prices fully reflect information in accruals and cash flows about
  future earnings? *The Accounting Review* 71(3).
- Titman, S., Wei, K. C. J. and Xie, F. (2004). Capital investments and stock returns. *Journal
  of Financial and Quantitative Analysis* 39(4).

## Worked example: a value decile

A cross-section formed from the panel, sorted into earnings-yield deciles, with each
decile's next twelve months. The dead issuers stay in: the panel already holds every
security with statements that traded that week, and nothing here filters on today's status.

<!-- worked-example: value-decile (test/fafnir/test_fundamentals_guide.py runs this block) -->
```sql
-- Earnings-yield deciles at the panel's month-end on or before 2010-06-30, every input as
-- known then, and each decile's return over the next twelve months.
WITH formed AS (
    SELECT p.security_id, p.as_of_date, p.earnings_yield,
           ntile(10) OVER (ORDER BY p.earnings_yield) AS decile
      FROM mart.fundamental_panel p
     WHERE p.as_of_date = (SELECT max(as_of_date) FROM mart.fundamental_panel
                            WHERE as_of_date <= DATE '2010-06-30')
       AND p.earnings_yield IS NOT NULL
),
held AS (
    SELECT f.decile, f.as_of_date, f.earnings_yield,
           s.close AS start_close, e.close AS end_close, e.trade_date AS end_date
      FROM formed f
      CROSS JOIN LATERAL (
            SELECT a.close FROM mart.v_daily_price_adjusted a
             WHERE a.security_id = f.security_id AND a.trade_date <= f.as_of_date
             ORDER BY a.trade_date DESC LIMIT 1) s
      CROSS JOIN LATERAL (
            SELECT a.close, a.trade_date FROM mart.v_daily_price_adjusted a
             WHERE a.security_id = f.security_id
               AND a.trade_date <= f.as_of_date + 365
             ORDER BY a.trade_date DESC LIMIT 1) e
)
SELECT decile,
       count(*)                                                 AS securities,
       count(*) FILTER (WHERE end_date < as_of_date + 358)      AS stopped_trading,
       round(min(earnings_yield)::numeric, 4)                   AS ey_from,
       round(max(earnings_yield)::numeric, 4)                   AS ey_to,
       round(avg(end_close / start_close - 1), 4)               AS mean_return_12m
  FROM held
 GROUP BY decile
 ORDER BY decile;
```

- **The month-end.** The panel's last date on or before the one given. Any other date is
  `mart.fn_fundamental_snapshot(D)` in place of the panel, and a reporting lag is its second
  argument.
- **The universe.** Every security in the panel with a non-NULL earnings yield: a loss is a
  negative yield, in decile 1, and a NULL (no basis, a currency mismatch) is out.
- **The return** is the split- and dividend-adjusted close twelve months on (or the last one
  before) over the close at formation: a total return.
- **`stopped_trading`** counts the securities whose last close in the year is more than a week
  before its end: the ones that died, or stopped trading for good. Their return runs to their
  last close. A delisting return (a cash-out at a price no venue printed) is not in the
  warehouse, so a decile heavy with dead issuers is measured to their last trade.
- **What it shows.** Each decile's size, its yield range, how many in it died within the year,
  and its mean return. A test runs this exact block on a synthetic universe. The production
  output is posted on #108 once the backfill (#92) has loaded the statements.

Drop the dead ones, with `AND sp.delisted_date IS NULL` against `mart.v_security_profile`, and
each decile loses its failures: the survivorship bias the panel exists to avoid.

## How it is tested

Every fixture is synthetic: no Sharadar row is committed. The golden values are built from the
companies' own 10-K filings, which are public record, and every restated value in them is
invented.

| File | Kind | What it proves |
|---|---|---|
| `test/fafnir/test_fundamentals_map.py` | unit | The statement map types SF1's statement indicators and nothing else: no per-share value, conversion or vendor metric. Classes, signs and SQL types are as decided |
| `test/fafnir/test_fundamentals_schema.py` | integration | Migration 0035. The constraints make a look-ahead and an undated restatement unstorable. One version is in force per period and one row is current per version. The taxonomy equals the map, the grants hold, and the upgrade leaves existing rows byte-identical |
| `test/fafnir/test_fundamentals_loader.py` | unit | The loader's logic: a row's map, fiscal labels (September and June years), every `known_from` rule, share-term recovery with exact split ratios, the hash at 1,000-share precision, and routing (a changed first-reported row is a correction, a changed restated row a restatement). Also that a split after load and a re-run write nothing |
| `test/fafnir/test_fundamentals_ingest.py` | integration | `fafnir ingest fundamentals` against the ledger. A backfill on both timelines, then a re-run that writes nothing. A nightly restatement, correction and split. Quarantine, unlinked keys, merges and the command |
| `test/fafnir/test_fundamentals_integration.py` | integration | End to end (#91): the golden values, read point in time. NULL is kept apart from 0. Loading twice is identical, and one moved value restates. The upgrade and the first load leave every existing table byte-identical |
| `test/fafnir/test_fundamentals_pit.py` | integration | Migration 0036 (#95): the versions views, `fn_statements_as_of` on both axes, and `fn_fundamentals_ttm`'s contiguity, NULLs and share terms |
| `test/fafnir/test_fundamental_panel.py` | integration | Migration 0037 (#100): every factor's arithmetic, the denominator rule, currency mismatch, the extra lag, survivorship, the two-year horizon, the look-ahead invariant on a refreshed panel, and the split-era trap through the loader. The panel's TTM meets `fn_fundamentals_ttm` at every month-end of a history with a restatement, a late filing and a missing quarter, and one date or sixty give the same rows. Migration 0038 (#101): the aggregates' quartiles over non-NULLs and both suppression rules, and the latest snapshot as today's, refreshed after the panel |
| `test/duk/test_db_fundamentals.py`, `test/duk/test_cli_fundamentals.py` | integration, unit | `duk fs` and `duk fa` (#107): a statement as known on a date (a later filing or restatement unseen, a corrected row never shown), the secondary share class through the CIK, the factor row at a month-end and latest, and the commands' db-only contract |
| `test/fafnir_mcp/test_fundamentals_tools.py` | unit, integration | `statements_as_of` and `factor_row` return duk's rows, validate before connecting, and name a secondary class |
| `test/fafnir/test_fundamentals_guide.py` | unit, integration | This page's worked example, read from the page and run on a synthetic universe: every decile's size, yield range, dead issuers and mean return as computed by hand |

**Golden values** (`test_fundamentals_integration.py`):
- **AAPL FY2020:** revenue 274,515,000,000, known from the 10-K of 2020-10-30 and not the day
  before.
- **AAPL FY2015:** weighted shares 5,753,421,000 as filed. They are recovered from SF1's
  post-2020 terms through the 4-for-1 of 2020-08-31.
- **MSFT FY2015:** two versions. As filed (93,580,000,000) until the load day, and a restated
  version from it.

**Standing invariants**, asserted after every load in `test_fundamentals_ingest.py`:
- nothing is known before its period ended (`known_from >= fiscal_date`);
- every period's current versions chain, each `known_to` equal to the next `known_from`, with
  only the last one open.

The panel's look-ahead invariant (#100) and the AAPL split-era P/E band (ADR 0019) join them with
the mart, in `test_fundamental_panel.py`.
