# Query walkthrough

A guided read of the three most important queries in this engine, for anyone
who wants to judge the SQL directly instead of trusting the README's summary.
Code excerpts are copied verbatim from `sql/`; see those files for full
context and comments.

## 1. Why PDC can't be a window function

PDC (Proportion of Days Covered) sounds like a job for `SUM() OVER (...)`
until you hit the early-refill case. If a member refills before their
current supply runs out, the new fill's coverage has to be *shifted forward*
in time to start the day after the existing supply ends -- otherwise you
either double-count the overlap or let PDC exceed 100%.

That shift makes each fill's covered window depend on the *previous fill's
computed window*, not just its own `fill_date` and `days_supply`. That's a
genuine running recurrence, which SQL's window functions can't express
directly (they see the input rows, not each other's outputs). The fix is a
recursive CTE that walks the fills one at a time, in order, carrying
`covered_end` forward as it goes.

## 2. The PDC engine (`sql/02_pdc_engine.sql`)

```sql
CREATE OR REPLACE MACRO pdc_fill_windows(class_name, window_start, window_end) AS TABLE
WITH RECURSIVE fills_ranked AS (
    SELECT
        pf.member_id, dr.drug_class, pf.fill_date, pf.days_supply,
        ROW_NUMBER() OVER (
            PARTITION BY pf.member_id ORDER BY pf.fill_date, pf.fill_id
        ) AS rn
    FROM pharmacy_fills pf
    JOIN drug_reference dr ON pf.drug_name = dr.drug_name
    WHERE dr.drug_class = class_name
      AND pf.fill_date BETWEEN window_start AND window_end
),
pdc_walk AS (
    SELECT member_id, drug_class, rn, fill_date, days_supply,
           fill_date AS covered_start,
           fill_date + days_supply - 1 AS covered_end
    FROM fills_ranked
    WHERE rn = 1

    UNION ALL

    SELECT f.member_id, f.drug_class, f.rn, f.fill_date, f.days_supply,
           CASE WHEN f.fill_date > w.covered_end THEN f.fill_date ELSE w.covered_end + 1 END,
           CASE WHEN f.fill_date > w.covered_end THEN f.fill_date ELSE w.covered_end + 1 END
               + f.days_supply - 1
    FROM fills_ranked f
    JOIN pdc_walk w ON f.member_id = w.member_id AND f.rn = w.rn + 1
)
SELECT member_id, drug_class, rn, fill_date, days_supply, covered_start, covered_end
FROM pdc_walk;
```

Two design choices worth calling out:

- **It's a table macro, not a view.** `drug_class` and the date window are
  parameters, so the exact same recurrence serves Measure 1 (`'DIABETES'`,
  calendar year 2025) and SPD Rate 2 (`'STATIN'`, calendar year 2025)
  without duplicating the recursive CTE.
- **The base case (`rn = 1`) and recursive case share one shift rule.**
  `CASE WHEN fill_date > covered_end THEN fill_date ELSE covered_end + 1 END`
  is the entire early-refill/gap decision: if the fill is early, coverage
  resumes the day after the old supply would have ended; if there's a real
  gap, coverage resumes at the fill date itself, and the gap days are
  simply never covered by anything.

Because each fill's window is built off the *previous* window's end, no two
fills' windows for the same member/drug_class can ever overlap -- which is
what guarantees PDC can never exceed 1.00 without needing a `LEAST(..., 1.0)`
clamp anywhere downstream. The `assert_no_pdc_over_one()` check in
`build.py` exists to catch a regression in this property, not to paper over
one.

## 3. Turning windows into a rate (`sql/03_measure_partd_diabetes.sql`)

The engine above returns raw fill windows; the measure query has to (a) find
each member's denominator eligibility and treatment period, then (b) sum
only the portion of each window that actually falls inside that period:

```sql
treatment_period AS (
    SELECT
        member_id, ipsd,
        LEAST(
            DATE '2025-12-31',
            COALESCE(death_date, DATE '2025-12-31'),
            COALESCE(coverage_end, DATE '2025-12-31')
        ) AS period_end
    FROM denominator
),
covered AS (
    SELECT
        tp.member_id,
        SUM(GREATEST(LEAST(w.covered_end, tp.period_end) - w.covered_start + 1, 0))
            AS covered_days
    FROM treatment_period tp
    JOIN pdc_fill_windows('DIABETES', DATE '2025-01-01', DATE '2025-12-31') w
        ON w.member_id = tp.member_id
    WHERE w.covered_start <= tp.period_end
    GROUP BY tp.member_id
)
```

`LEAST(w.covered_end, tp.period_end)` truncates a window that would run past
the treatment period (this is what makes `M_DIED`'s coverage stop at the
death date instead of running out the last fill's full 30-day supply).
`GREATEST(..., 0)` guards against a window that starts after the period ends
contributing a negative count. Because the windows from `pdc_fill_windows`
never overlap, this `SUM()` is a plain, safe aggregate -- no risk of
double-counting a day that two fills both claim.

## 4. Two rates, one shared population (`sql/04_measure_spd.sql`)

SPD's Rate 2 denominator is defined as Rate 1's numerator, which the query
expresses directly rather than re-deriving eligibility twice:

```sql
rate1 AS (
    SELECT e.member_id, sf.statin_ipsd, (sf.member_id IS NOT NULL) AS rate1_numerator
    FROM eligible e
    LEFT JOIN statin_fills_2025 sf ON sf.member_id = e.member_id
),
covered AS (
    SELECT r.member_id, SUM(...) AS covered_days
    FROM rate1 r
    JOIN pdc_fill_windows('STATIN', DATE '2025-01-01', DATE '2025-12-31') w
        ON w.member_id = r.member_id
    WHERE r.rate1_numerator
      AND w.covered_start <= DATE '2025-12-31'
    GROUP BY r.member_id
)
```

`rate1` is a `LEFT JOIN`, so every eligible member gets a row even with zero
statin fills -- `rate1_numerator` is just "did a matching row exist," and
`covered` (and therefore Rate 2 PDC) is only computed for members where it's
true. This is also where `M_SWITCH` gets tested: the join to
`pdc_fill_windows('STATIN', ...)` doesn't care that the underlying fills
are two different `drug_name` values (atorvastatin, then rosuvastatin) --
the recursive CTE partitions by `member_id` within the `STATIN` class, so
the switch is invisible to it and coverage carries through unbroken.
