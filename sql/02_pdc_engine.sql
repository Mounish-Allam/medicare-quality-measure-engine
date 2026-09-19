-- Grain: table macro pdc_fill_windows(drug_class, window_start, window_end) --
-- one row per member_id per fill, with that fill's shifted covered_start/
-- covered_end. Shared PDC logic for both measures (see SPEC.md "Shared: PDC
-- calculation").
--
-- Early refills shift forward instead of stacking, so windows never overlap
-- and PDC can't exceed 1.00 by construction. Needs a recursive CTE rather
-- than a window function because each row's shift depends on the previous
-- row's computed covered_end.

CREATE OR REPLACE MACRO pdc_fill_windows(class_name, window_start, window_end) AS TABLE
WITH RECURSIVE fills_ranked AS (
    SELECT
        pf.member_id,
        dr.drug_class,
        pf.fill_date,
        pf.days_supply,
        ROW_NUMBER() OVER (
            PARTITION BY pf.member_id
            ORDER BY pf.fill_date, pf.fill_id
        ) AS rn
    FROM pharmacy_fills pf
    JOIN drug_reference dr ON pf.drug_name = dr.drug_name
    WHERE dr.drug_class = class_name
      AND pf.fill_date BETWEEN window_start AND window_end
),
pdc_walk AS (
    -- base case: each member's first fill in the window starts its own coverage
    SELECT
        member_id,
        drug_class,
        rn,
        fill_date,
        days_supply,
        fill_date AS covered_start,
        fill_date + days_supply - 1 AS covered_end
    FROM fills_ranked
    WHERE rn = 1

    UNION ALL

    -- recursive step: shift forward on early refill, otherwise start fresh at fill_date
    SELECT
        f.member_id,
        f.drug_class,
        f.rn,
        f.fill_date,
        f.days_supply,
        CASE WHEN f.fill_date > w.covered_end THEN f.fill_date ELSE w.covered_end + 1 END
            AS covered_start,
        CASE WHEN f.fill_date > w.covered_end THEN f.fill_date ELSE w.covered_end + 1 END
            + f.days_supply - 1 AS covered_end
    FROM fills_ranked f
    JOIN pdc_walk w
        ON f.member_id = w.member_id
       AND f.rn = w.rn + 1
)
SELECT member_id, drug_class, rn, fill_date, days_supply, covered_start, covered_end
FROM pdc_walk;
