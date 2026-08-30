-- Grain: one row per member_id, for every member in the Measure 1 denominator
-- (Part D Medication Adherence for Diabetes Medications).
--
-- Denominator: members with >= 2 fills of a DIABETES-class drug on different
-- dates during the measurement year (2025).
-- Treatment period: first diabetes-med fill date (IPSD) through the earlier of
-- year end, death_date, or coverage_end.
-- Numerator: denominator members with PDC >= 0.80 on DIABETES-class drugs.
-- Exclusion implemented: hospice at any point in the measurement year.
--
-- NOT IMPLEMENTED (see README "Not implemented"): the real PQA spec also
-- excludes ESRD and certain insulin-only scenarios. Those exclusions require
-- diagnosis/drug detail this synthetic dataset does not model and are
-- deliberately left out rather than guessed at.

CREATE OR REPLACE TABLE measure1_partd_diabetes AS
WITH diabetes_fills_2025 AS (
    SELECT pf.member_id, pf.fill_date
    FROM pharmacy_fills pf
    JOIN drug_reference dr ON pf.drug_name = dr.drug_name
    WHERE dr.drug_class = 'DIABETES'
      AND pf.fill_date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
),
fill_counts AS (
    SELECT
        member_id,
        COUNT(DISTINCT fill_date) AS distinct_fill_dates,
        MIN(fill_date) AS ipsd
    FROM diabetes_fills_2025
    GROUP BY member_id
),
hospice_2025 AS (
    -- Measure 1 states this exclusion applies "at any point in the
    -- measurement year" explicitly, unlike SPD's hospice exclusion below.
    SELECT DISTINCT member_id
    FROM conditions
    WHERE condition_group = 'HOSPICE'
      AND dx_date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
),
denominator AS (
    SELECT fc.member_id, fc.ipsd, m.death_date, m.coverage_end
    FROM fill_counts fc
    JOIN members m ON m.member_id = fc.member_id
    WHERE fc.distinct_fill_dates >= 2
      AND fc.member_id NOT IN (SELECT member_id FROM hospice_2025)
),
treatment_period AS (
    SELECT
        member_id,
        ipsd,
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
        SUM(
            GREATEST(LEAST(w.covered_end, tp.period_end) - w.covered_start + 1, 0)
        ) AS covered_days
    FROM treatment_period tp
    JOIN pdc_fill_windows('DIABETES', DATE '2025-01-01', DATE '2025-12-31') w
        ON w.member_id = tp.member_id
    WHERE w.covered_start <= tp.period_end
    GROUP BY tp.member_id
)
SELECT
    tp.member_id,
    tp.ipsd,
    tp.period_end,
    (tp.period_end - tp.ipsd + 1) AS days_in_treatment_period,
    COALESCE(c.covered_days, 0) AS covered_days,
    COALESCE(c.covered_days, 0)::DOUBLE / (tp.period_end - tp.ipsd + 1) AS pdc,
    (COALESCE(c.covered_days, 0)::DOUBLE / (tp.period_end - tp.ipsd + 1)) >= 0.80 AS adherent
FROM treatment_period tp
LEFT JOIN covered c ON c.member_id = tp.member_id;
