# Fundamentals

> **Stub.** #108 fills in this page: what the statement tables hold and how to read them point in
> time, the as-first-reported convention and what it does to growth factors, and the deferred
> research mode. For now, the design is in
> [plans/fundamentals-sf1.md](plans/fundamentals-sf1.md), the rules in
> [ADR 0018](adr/0018-bitemporal-fundamentals.md) and
> [ADR 0019](adr/0019-derived-metrics-not-vendor-metrics.md), the columns in the
> [data dictionary](data_dictionary.md#fundamentals-migration-0035-adr-0018), and the loader in
> [ingestion.md](ingestion.md#statements-from-sf1-90).

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

- **Growth compares versions in force at one date.** History before the backfill reads as first
  reported, so growth there is first reported against first reported. After a restatement is
  loaded, the restated periods compare with each other. Where a company reclassified a line, the
  two years are on different bases, and the growth figure inherits that.
- **Classification is today's.** `sector_name`, `industry_name` and `exchange_code` are the
  security master's current values, not as they were at the date.
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
| `test/fafnir/test_fundamental_panel.py` | integration | Migration 0037 (#100): every factor's arithmetic, the denominator rule, currency mismatch, the extra lag, survivorship, the two-year horizon, the look-ahead invariant on a refreshed panel, and the split-era trap through the loader. The panel's TTM meets `fn_fundamentals_ttm` at every month-end of a history with a restatement, a late filing and a missing quarter, and one date or sixty give the same rows |

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
