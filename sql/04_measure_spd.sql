-- Grain: one row per member_id who is diabetic and otherwise eligible for
-- SPD (Statin Therapy for Patients With Diabetes, NCQA HEDIS). Rate 2's
-- denominator is exactly the members where rate1_numerator = true. See
-- SPEC.md "Measure 2" for the full definition.
--
-- NOT IMPLEMENTED: ESRD, cirrhosis, myalgia/myopathy/rhabdomyolysis,
-- pregnancy, IVF, palliative care, advanced illness with frailty -- not
-- modeled in this synthetic dataset. See README "Not implemented".

CREATE OR REPLACE TABLE measure2_spd AS
WITH diabetes_dx AS (
    SELECT member_id, COUNT(DISTINCT dx_date) AS distinct_dx_dates
    FROM conditions
    WHERE condition_group = 'DIABETES'
      AND dx_date BETWEEN DATE '2024-01-01' AND DATE '2025-12-31'
    GROUP BY member_id
),
diabetes_drug AS (
    SELECT DISTINCT pf.member_id
    FROM pharmacy_fills pf
    JOIN drug_reference dr ON pf.drug_name = dr.drug_name
    WHERE dr.drug_class = 'DIABETES'
      AND pf.fill_date BETWEEN DATE '2024-01-01' AND DATE '2025-12-31'
),
diabetes_identified AS (
    SELECT member_id FROM diabetes_dx WHERE distinct_dx_dates >= 2
    UNION
    SELECT member_id FROM diabetes_drug
),
ascvd_excluded AS (
    SELECT DISTINCT member_id
    FROM conditions
    WHERE condition_group = 'ASCVD'
      AND dx_date <= DATE '2025-12-31'
),
hospice_excluded AS (
    -- Spec gives ASCVD an explicit "on or before 2025-12-31" window and
    -- lists hospice in the same clause without repeating one -- read here as
    -- sharing it (ambiguous wording, documented in the README).
    SELECT DISTINCT member_id
    FROM conditions
    WHERE condition_group = 'HOSPICE'
      AND dx_date <= DATE '2025-12-31'
),
eligible AS (
    SELECT m.member_id
    FROM members m
    JOIN diabetes_identified di ON di.member_id = m.member_id
    -- 2025-12-31 is year-end, so age-as-of-anchor is just 2025 minus birth
    -- year -- no month/day comparison needed.
    WHERE (DATE_PART('year', DATE '2025-12-31') - DATE_PART('year', m.birth_date)) BETWEEN 40 AND 75
      AND m.coverage_start <= DATE '2024-01-01'
      AND (m.coverage_end IS NULL OR m.coverage_end >= DATE '2025-12-31')
      AND m.member_id NOT IN (SELECT member_id FROM ascvd_excluded)
      AND m.member_id NOT IN (SELECT member_id FROM hospice_excluded)
),
statin_fills_2025 AS (
    SELECT pf.member_id, MIN(pf.fill_date) AS statin_ipsd
    FROM pharmacy_fills pf
    JOIN drug_reference dr ON pf.drug_name = dr.drug_name
    WHERE dr.drug_class = 'STATIN'
      AND pf.fill_date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
    GROUP BY pf.member_id
),
rate1 AS (
    -- Unlike Measure 1, no truncation for death/coverage_end here -- runs to
    -- 2025-12-31 regardless, per SPEC.md.
    SELECT
        e.member_id,
        sf.statin_ipsd,
        (sf.member_id IS NOT NULL) AS rate1_numerator,
        CASE WHEN sf.member_id IS NOT NULL
             THEN DATE '2025-12-31' - sf.statin_ipsd + 1
        END AS days_in_treatment_period
    FROM eligible e
    LEFT JOIN statin_fills_2025 sf ON sf.member_id = e.member_id
),
covered AS (
    SELECT
        r.member_id,
        SUM(
            GREATEST(LEAST(w.covered_end, DATE '2025-12-31') - w.covered_start + 1, 0)
        ) AS covered_days
    FROM rate1 r
    JOIN pdc_fill_windows('STATIN', DATE '2025-01-01', DATE '2025-12-31') w
        ON w.member_id = r.member_id
    WHERE r.rate1_numerator
      AND w.covered_start <= DATE '2025-12-31'
    GROUP BY r.member_id
)
SELECT
    r.member_id,
    r.rate1_numerator,
    r.statin_ipsd,
    r.days_in_treatment_period,
    CASE WHEN r.rate1_numerator THEN COALESCE(c.covered_days, 0) END AS covered_days,
    CASE WHEN r.rate1_numerator
         THEN COALESCE(c.covered_days, 0)::DOUBLE / r.days_in_treatment_period
    END AS pdc,
    CASE WHEN r.rate1_numerator
         THEN (COALESCE(c.covered_days, 0)::DOUBLE / r.days_in_treatment_period) >= 0.80
         ELSE FALSE
    END AS rate2_numerator
FROM rate1 r
LEFT JOIN covered c ON c.member_id = r.member_id;
