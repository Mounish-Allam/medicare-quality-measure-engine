# Hand verification

Manual arithmetic for the two edge-case members SPEC.md calls out for
hand-checking, computed independently of the SQL and then compared against
`sql/03_measure_partd_diabetes.sql`'s actual output for the same members.
Both match.

## M_EARLY

Scenario: 30-day-supply diabetes fills refilled every 20 days, all year
(2025-01-01 through 2025-12-27). This tests the early-refill shift, since
every refill arrives 10 days before the previous fill's supply would have
run out.

Because every refill is early, each fill's covered window is shifted to
start the day after the previous window ends -- there is never a gap and
never an overlap. That means the covered windows tile the calendar exactly,
end to end, starting 2025-01-01:

| fill # | fill_date  | covered_start | covered_end |
|---|---|---|---|
| 1 | 2025-01-01 | 2025-01-01 | 2025-01-30 |
| 2 | 2025-01-21 | 2025-01-31 | 2025-03-01 |
| 3 | 2025-02-10 | 2025-03-02 | 2025-03-31 |
| ... | ... | ... | ... |
| 13 | 2025-08-29 | 2025-12-27 | 2026-01-25 |

(19 fills total in 2025; the engine's actual output for all 19 rows matches
this tiling exactly -- see `pdc_fill_windows('DIABETES', ...)` for
M_EARLY.)

Since the windows tile with zero gaps starting exactly at the treatment
period start (2025-01-01), every single day of the treatment period
(2025-01-01 through 2025-12-31 = 365 days) is covered by exactly one
window's supply, with the surplus from over-frequent refilling pushed out
past year end rather than double-counted.

```
days_in_treatment_period = 2025-12-31 - 2025-01-01 + 1 = 365
covered_days             = 365   (no gaps, no double-count)
PDC                       = 365 / 365 = 1.0000
```

**Query output:** `pdc = 1.0`, `adherent = true`. Matches. Critically, PDC
is exactly 1.0000 and not inflated above 1.00 despite 19 fills of 30-day
supply (570 nominal fill-days) being squeezed into a 365-day year -- proof
the shift logic is suppressing the surplus rather than accumulating it.

## M_GAP60

Scenario: on-time 30-day fills from 2025-01-01 through 2025-05-15 (five
fills, none early, none late), then a deliberate gap before the fill that
was supposed to land on 2025-06-14 is skipped, and refills resume 60 days
after coverage would otherwise have run out, continuing on schedule through
year end.

```
Fill 5: fill_date 2025-05-01, days_supply 30
  covered_start = 2025-05-01, covered_end = 2025-05-30

Gap: no fill arrives until 60 days after 2025-05-30 + 1
  next covered_start = 2025-05-30 + 1 + 60 = 2025-07-30

Fill 6: fill_date 2025-07-30, days_supply 30
  covered_start = 2025-07-30, covered_end = 2025-08-28
```

Uncovered gap days = 2025-05-31 through 2025-07-29 inclusive:
`1 (rest of May) + 30 (June) + 29 (July 1-29) = 60 days`, exactly the
intended 60-day true gap.

Fills 6-11 then run on-time through year end with no further gaps, covering
2025-07-30 through 2026-01-25 (truncated at 2025-12-31).

```
days_in_treatment_period = 2025-12-31 - 2025-01-01 + 1 = 365
covered_days             = 365 - 60 (the one true gap) = 305
PDC                       = 305 / 365 = 0.835616... -> 0.8356
```

**Query output:** `covered_days = 305`, `pdc = 0.8356`, `adherent = true`
(0.8356 >= 0.80). Matches.

## M_DIED (spot-checked, not required, included because it's easy to get wrong)

Fills continue on a normal 30-day schedule through 2025-07-30 (covered
through 2025-08-28 if uninterrupted), but `death_date = 2025-08-15`
truncates the treatment period there instead.

```
days_in_treatment_period = 2025-08-15 - 2025-01-01 + 1 = 227
covered_days              = 227 (fully covered up to the truncation point)
PDC                        = 227 / 227 = 1.0000
```

**Query output:** `treatment_period_end = 2025-08-15`,
`days_in_treatment_period = 227`, `covered_days = 227`, `pdc = 1.0`.
Matches, and confirms the treatment period is truncated at `death_date`
rather than letting the last fill's nominal supply run past it.
