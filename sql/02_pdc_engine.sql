-- Grain: defines a reusable table macro, pdc_fill_windows(drug_class, window_start,
-- window_end), rather than producing rows itself. Its output grain (when called) is
-- one row per pharmacy fill for the given drug_class with fill_date in
-- [window_start, window_end]: one row per member_id per fill, carrying that fill's
-- shifted covered_start / covered_end dates.
--
-- This is the core PDC (Proportion of Days Covered) logic shared by both measures.
-- Get it right once, here, instead of re-deriving it per measure.
--
-- Algorithm (see SPEC.md "Shared: PDC calculation"):
--   1. Order a member's fills within a drug class by fill_date.
--   2. Walk fills forward maintaining a running "covered through" date.
--      - If fill_date > covered_through: coverage resumes at fill_date (a gap).
--      - If fill_date <= covered_through (early refill): the new supply is shifted
--        forward to start the day after covered_through. Surplus days push out;
--        they are never double-counted inside an already-covered window.
--   3. covered_through advances by days_supply each fill.
--
-- Because consecutive covered windows never overlap by construction, each fill's
-- window [covered_start, covered_end] is disjoint from every other fill's window
-- for that member/drug_class. That means covered days can be computed downstream
-- by simply summing (covered_end - covered_start + 1) per fill after truncating
-- each window to the treatment period -- no double-counting is possible, and PDC
-- can never exceed 1.00 by construction.
--
-- This requires a genuine running recurrence (each row depends on the previous
-- row's computed value), which is not expressible as a plain SUM() OVER window
-- function. It's implemented here as a recursive CTE, wrapped in a table macro so
-- both measure files can call it with their own drug_class and date window.

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
