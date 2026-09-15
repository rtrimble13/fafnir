# Classifying outlier flags at scale

The playbook in `dq-playbooks.md` says what an `outlier` flag *means*. This file is
the recipe for sorting a few hundred of them into causes — the queries, the tests
that separated one cause from the next, and the traps that made a plausible class
wrong. It was written from the 2026-09-15 session, which took 300 open outliers to
166: 134 closed or accepted, each batch by one cause, and the rest left open with a
named reason.

**Read the whole decision order before the first batch.** Most of the mistakes that
session nearly made came from stopping at the first test that seemed to explain a
flag.

---

## 1. One first-pass table, then partition

Pull every open flag with the features that decide its class, into a file (see
"Mechanics" in `SKILL.md` — this result is too large to read inline):

```sql
WITH f AS (
  SELECT q.dq_flag_id id, q.security_id sid, s.primary_symbol sym, s.exchange_code ex,
         (q.record_key->>'trade_date')::date d,
         (q.detail->>'close')::numeric c, (q.detail->>'prev_close')::numeric pc
    FROM ops.data_quality_flag q JOIN core.security s USING (security_id)
   WHERE q.check_name = 'outlier' AND q.resolved_at IS NULL AND q.accepted_at IS NULL)
SELECT f.id, f.sym, f.sid, f.d, round(f.c / nullif(f.pc, 0), 3) AS ratio,
  (SELECT is_open FROM ref.trading_calendar t
    WHERE t.exchange_code = f.ex AND t.trade_date = f.d)                      AS session,
  (SELECT max(p.trade_date) FROM core.daily_price p
    WHERE p.security_id = f.sid AND p.trade_date < f.d
      AND p.trade_date >= f.d - 4000)                                         AS prev_bar,
  (SELECT volume FROM core.daily_price p
    WHERE p.security_id = f.sid AND p.trade_date = f.d)                       AS vol,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY volume)::bigint
     FROM core.daily_price p
    WHERE p.security_id = f.sid AND p.trade_date BETWEEN f.d - 60 AND f.d - 1) AS med_vol,
  (SELECT round(p.close / nullif(f.c, 0), 3) FROM core.daily_price p
    WHERE p.security_id = f.sid AND p.trade_date > f.d AND p.trade_date <= f.d + 10
    ORDER BY p.trade_date LIMIT 1)                                            AS next_ratio,
  (SELECT string_agg(ca.corporate_action_id || '@' || ca.ex_date || ' '
                     || ca.split_numerator || ':' || ca.split_denominator, ' ')
     FROM core.corporate_action ca
    WHERE ca.security_id = f.sid AND ca.action_type = 'split'
      AND ca.ex_date BETWEEN f.d - 15 AND f.d + 15)                           AS splits_near
FROM f ORDER BY f.sym, f.d;
```

Add the adjusted move in a second query (`mart.v_daily_price_adjusted`, close on
`d` over the close on the previous bar) — joining it into the first one times out.
Then partition the ids into named groups in a file and **count the groups back to
the total** before proposing anything. A flag in two groups, or in none, is a
classification you have not finished.

## 2. The decision order

Take the tests in this order. Each one names what it rules *out*, because the
failure that session came closest to was accepting a class on its first matching
test.

### 2a. Already gone → `dq recheck --dry-run`

Always first. Bars deleted in an earlier session, and provisional vendor bars FMP
has since restated, show up here. The recheck closes a flag whose bar, or whose
previous bar, no longer exists — which is also how every delete repair below gets
closed.

### 2b. A split between the previous bar and this one

The check skips a split only when its ex-date **equals** the flagged bar's date
(`dq/checks.py`, `ca.ex_date = m.trade_date`). A split dated on a weekend, a
holiday, or a session with no stored bar sits *between* two bars and is flagged
every time, even though the adjusted series is right.

```sql
-- split ex_date in (prev_bar, d): the check cannot see it
SELECT ... WHERE ca.ex_date > prev_bar AND ca.ex_date < d
```

Qualifies for **accept** when the ratio matches the raw jump within ~35% **and** the
adjusted move is under 50% **and** the plausibility gate below passes. Prefer
accept over `actions redate` here: when the ex-date is a session with no bar,
re-dating to the next bar would record a date that is not the ex-date.

**The plausibility gate — do not skip it.** A vendor split row can be as wrong as
the prices it "explains". FMP has fabricated split rows that exactly match a history
it stored at the wrong scale, so the adjusted series looks smooth and both halves
are fiction. Before accepting, read `mart.v_security_price_coverage` (`min_close`,
`max_close`) and the adjusted closes around the date. That session pulled five of 28
from an accept batch this way:

- AKR: raw prev close 1,220,837, max close 149,613,176, for a REIT that traded ~$10–30.
- HUN: 283 → 27.66 on a "10:1 split" in 2014; Huntsman never split.
- JKL: 6,504 → 130 on a "50:1 split".

Those are an off-scale history, not a market fact. Leave them open.

### 2c. A split within ±5 sessions whose date is wrong → `actions redate`

Only when the raw series shows **one** jump, of the split's ratio, on a session other
than the ex-date, and the price holds afterwards. Read the bars either side of both
dates before proposing. Of 12 candidates, 5 were not misdates:

- **Two jumps of the split's size** a day or a few apart (BHAT, CATB, ELOX): the
  pre-split history is already mis-adjusted. Moving the date fixes neither jump.
- **The flagged move is a bad bar reverting** (BESS: a 0.0005 print between 0.07s).
- **Thin-trading noise** that bounces between two levels (NCRA 3.50 / 2.33): the
  price cannot tell you the date.

Zero-volume carry bars (the prior close repeated on 0 shares) on the vendor's
ex-date make the first *changed* price the best evidence of the date — use it, and
say in the note that the date rests on price, not trades.

### 2d. No split on file: unreported split, or not

A split the feed never reported qualifies for `actions add` only when **all** hold:

| Test | Threshold that worked |
|---|---|
| Clean ratio | within 3% of ×k or ÷k |
| Level holds | ≥90% of the next 40 sessions within 35% of the new level |
| No revert | no bar in the next 40 back within 25% of the old level |
| Volume shifts inversely | median volume ÷ (reverse split) or × (forward) by ≥2× |
| Real trading on the day | volume > 0 |
| No split nearby | none within ±120 days |

Same-day sibling funds moving by the same kind of ratio (iShares JKD/JKG/JKI on
2020-05-04; Invesco RGI/RTM on 2023-07-17) are strong corroboration.

Two things that pass the table and are still not splits:

- **Flat identical prices on heavy volume before the jump** (EQC 1997: 0.9475 three
  days running on 3.7–5.8M shares, then 18.95). That is the vendor storing older bars
  at the wrong scale. Adding a split would disguise it — leave open.
- **A merger exchange ratio** (Spark Networks Inc → Spark Networks SE ADS at 0.1).
  Recording it as a split keeps the adjusted series continuous; say in the note that
  it is an exchange, and tell the operator.

A jump that fails the table but moves with volume and keeps moving (JDST and NTG,
March 2020) is a **market fact**: accept, naming the neighbouring moves and any real
split on file nearby.

### 2e. The other half of an accepted pair

Bulk spike-and-revert acceptances pair flags by rule, and the rule misses edges —
the partner falls outside the window, or a third jump sits between. Those leftovers
look like fresh problems. Find the partner in **any state**:

```sql
-- the jump that reverses this one, within ~3 years, with nothing large in between
WITH f AS (
  SELECT q.dq_flag_id id, q.security_id sid, (q.record_key->>'trade_date')::date d,
         (q.detail->>'close')::numeric / nullif((q.detail->>'prev_close')::numeric, 0) r,
         CASE WHEN q.accepted_at IS NOT NULL THEN 'acc'
              WHEN q.resolved_at IS NOT NULL THEN 'res' ELSE 'OPEN' END st
    FROM ops.data_quality_flag q WHERE q.check_name = 'outlier')
SELECT a.id, a.d, round(a.r, 3), a.st, b.id, b.d, round(b.r, 3), b.st,
       round(a.r * b.r, 3) AS product
  FROM f a JOIN f b ON b.sid = a.sid AND b.d > a.d AND b.d <= a.d + 1200
   AND abs(a.r * b.r - 1) < 0.2 AND (a.r > 1.9 OR a.r < 0.53)
   AND NOT EXISTS (SELECT 1 FROM f c WHERE c.sid = a.sid AND c.d > a.d AND c.d < b.d
                     AND (c.r > 1.9 OR c.r < 0.53))
 WHERE a.st = 'OPEN' OR b.st = 'OPEN';
```

Then **look at the bars in the window before assuming a clean block**. Most were not
blocks at all but two or three price scales alternating day by day (EBIX 2003–05 at
×3/×9/×27; BVH at 0.75/18/90). No single delete or split repairs those. When the
partner was accepted under operator direction, accepting the other half is the same
decision — say so, and say the window's history is unreliable.

### 2f. A handful of bad bars inside good history → scan, then delete

When a window holds a few prints far from their neighbours, find them all before
deleting any:

```sql
-- every bar more than 2.5x off the median of its +/-10 sessions (one security)
WITH p AS (
  SELECT d.trade_date, d.close, d.volume,
         (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY x.close)
            FROM core.daily_price x
           WHERE x.security_id = <id> AND x.volume > 0
             AND x.trade_date BETWEEN d.trade_date - 10 AND d.trade_date + 10) AS nb_med
    FROM core.daily_price d
   WHERE d.security_id = <id> AND d.trade_date BETWEEN <from> AND <to>)
SELECT extract(year FROM trade_date)::int AS yr, count(*) AS bars,
       count(*) FILTER (WHERE close / nullif(nb_med, 0) NOT BETWEEN 0.4 AND 2.5) AS off,
       count(*) FILTER (WHERE close / nullif(nb_med, 0) NOT BETWEEN 0.4 AND 2.5
                          AND volume > 0) AS off_with_volume,
       string_agg(CASE WHEN close / nullif(nb_med, 0) NOT BETWEEN 0.4 AND 2.5
                       THEN to_char(trade_date, 'MM-DD') END, ',') AS dates
  FROM p GROUP BY 1 ORDER BY 1;
```

Run it over the security's **whole history** so the note can say there are no
others. RUSS: exactly 11 bars in 2011, all zero-volume at ~5×, none in 2012–2020.
IOR: exactly one, the day after a 1:4 split applied a second time.

### 2g. One-tick moves on sub-dime prices → accept

1/16 → 3/16 (ARWR 1995), 0.01 → 0.03 (PCYG 2003). The percentage is large because the
price is one or two ticks. A market fact.

### 2h. New listings carrying someone else's history

A fund or SPAC that starts trading on a ticker another instrument used gets the
whole ticker history from FMP. The flag sits on the new listing's first bar, after a
gap of months or years. Split the security's history into segments to see it:

```sql
WITH b AS (
  SELECT trade_date, close, volume,
         trade_date - lag(trade_date) OVER (ORDER BY trade_date) AS gap
    FROM core.daily_price WHERE security_id = <id>),
seg AS (
  SELECT *, sum(CASE WHEN gap IS NULL OR gap > 60 THEN 1 ELSE 0 END)
              OVER (ORDER BY trade_date) AS segno
    FROM b)
SELECT segno, min(trade_date), max(trade_date), count(*) AS bars,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY close)  AS med_close,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY volume) AS med_volume
  FROM seg GROUP BY segno ORDER BY segno;
```

An ETF's first real bar is almost always ~$25 (a SPAC's ~$10). Then decide per
earlier segment, **after the survivorship check**:

- **Junk** — sub-penny prices on zero volume every day, a stray bar years after
  delisting, a scale no instrument traded at (PBL 2013–15, FUSD, HBTC, EVA, INP,
  TOVX): `prices delete` the segment. Pre-first-bar deletes create no gap flags —
  the gap check only looks between a security's first and last bar — and they let
  `sparse_coverage` recheck closed.
- **A real earlier issuer** (Sotheby's under BID, Thoratec and Synthorx under THOR,
  Liberty Tax under TAX): **do not delete.** See below.

**The survivorship check.** The warehouse keeps delisted securities on purpose. An
earlier segment may be the *only* copy of a real company. Before deleting one,
identify the issuer and look for another row holding it — by name
(`core.security.company_name ~* ...`) and ticker history (`core.symbol_xref`), not by
matching closes across securities on a date (that times out). QSI held HighCape's
2020–21 history, so deleting it from CAPA lost nothing; the 2003–08 CAPA issuer
was held nowhere, and deleting it was a mistake that only the override record
recovers. When the earlier segment is the only copy, keep both histories and accept
the boundary flag with a note that says so, or move the history to its own security
if a command for that is deployed.

### 2i. Leave open, with the reason

- **Alternating scales on a new ETF where you cannot prove which is right** (KEO
  25 / 3.2; ENTL, where FMP's close matched but its volume did not).
- **Corruption that continues into the latest bars** (MILK, MMAX): deleting history
  does not stop the next night's bad bar.
- **The newest bar of an ETF FMP restates** (XNDX, MILK, MSEP, NODE): FMP serves a
  provisional last bar, sometimes at the wrong scale, and restates it the next night.
  Recheck tomorrow; never delete a newest bar on the first night.
- **A history back-adjusted by the vendor** (WZRD at 30,000–108,000 in 2023 on ~1M
  shares, then 808 → 0.88; SMUP opening at 6,229 on 46 shares): FMP has applied later
  reverse splits to its "unadjusted" feed. A delete loses real trading, a split row
  double-counts. Needs a rescale.
- **A whole history dated a day early** (FVI, WLL — no Friday bars, Sunday bars that
  are Monday sessions): check the weekday distribution. `prices delete --non-session`
  would delete every Monday. Needs a date shift.
- **Bars on MLK Day 1990–1997.** `ref.trading_calendar` marks them closed; NYSE was
  open until 1998. They are real bars.

## 3. Before any delete: probe the vendor

`source probe-prices --symbol S --date D` (3 requests) shows what FMP serves *now*.
Read the full output — `bar compared:` confirms the date it matched, and
`unadjusted close` is the value. If FMP has corrected the bar, **re-fetch** with
`ingest prices --symbols S --from D1 --to D2` instead of deleting: GEVX came back
from 251,705 to 20.57 that way, keeping seven sessions a delete would have lost.
A volume mismatch with a matching close (ENTL) means the vendor is mid-correction
or mixing instruments — hold.

## 4. Say what each batch will leave behind

- An interior bar deleted → a `gap` flag for that session on the next `dq run`, to
  accept.
- Enough interior bars deleted to drop below 80% of sessions → one `sparse_coverage`
  flag instead (DRAL after 22 deletes), which has no playbook tier yet.
- A security's `price_*` quarantines on a pre-launch date keep coming back while the
  loader re-reads that date (PBL 2022-12-19).
