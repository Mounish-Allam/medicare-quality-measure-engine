-- Grain: one row per member_id, for every member who is diabetic and
-- otherwise eligible for SPD (Statin Therapy for Patients With Diabetes,
-- NCQA HEDIS). Rate 1 and Rate 2 flags live side by side on the same row;
-- Rate 2's denominator is exactly the members where rate1_numerator = true.
--
-- Age: 40-75 as of the fixed anchor date 2025-12-31 (never the system date).
-- Because 2025-12-31 is the last day of the year, age-as-of-anchor always
-- equals 2025 minus birth year -- every birthday in a given year has already
-- occurred by December 31 of that year, so no month/day comparison is needed.
--
-- Continuous enrollment: enrolled for the measurement year (2025) and the
-- prior year (2024), read literally as coverage_start on/before 2024-01-01
-- and coverage_end (if any) on/after 2025-12-31.
--
-- Diabetes identification: >= 2 DIABETES condition rows on different dates in
-- 2024 or 2025, OR >= 1 DIABETES-class drug fill in 2024 or 2025.
--
-- Exclusions implemented: any ASCVD condition row on or before 2025-12-31;
-- hospice. SPEC.md gives the ASCVD exclusion an explicit "on or
-- before 2025-12-31" qualifier and lists hospice in the same clause ("any
-- ASCVD condition row on or before 2025-12-31; hospice") without repeating
-- a separate time window, so hospice is implemented with the same
-- on-or-before-2025-12-31 qualifier here. This is a reading of ambiguous
-- wording, not an invented threshold -- documented here and in the README.
--
-- NOT IMPLEMENTED (see README "Not implemented"): the real HEDIS spec also
-- excludes ESRD, cirrhosis, myalgia/myopathy/rhabdomyolysis, pregnancy, IVF,
-- palliative care, and advanced illness with frailty. None of those are
-- modeled in this synthetic dataset and are deliberately left out.
--
-- Rate 2 treatment period differs from Measure 1: it runs from the earliest
-- 2025 statin fill (IPSD) through 2025-12-31 with NO truncation for death or
-- coverage_end -- that's what SPEC.md specifies for this measure.

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
    SELECT DISTINCT member_id
    FROM conditions
    WHERE condition_group = 'HOSPICE'
      AND dx_date <= DATE '2025-12-31'
),
eligible AS (
    SELECT m.member_id
    FROM members m
    JOIN diabetes_identified di ON di.member_id = m.member_id
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
    SELECT
        e.member_id,
        sf.statin_ipsd,
        (sf.member_id IS NOT NULL) AS rate1_numerator
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
    CASE WHEN r.rate1_numerator THEN (DATE '2025-12-31' - r.statin_ipsd + 1) END
        AS days_in_treatment_period,
    CASE WHEN r.rate1_numerator THEN COALESCE(c.covered_days, 0) END AS covered_days,
    CASE WHEN r.rate1_numerator
         THEN COALESCE(c.covered_days, 0)::DOUBLE / (DATE '2025-12-31' - r.statin_ipsd + 1)
    END AS pdc,
    CASE WHEN r.rate1_numerator
         THEN (COALESCE(c.covered_days, 0)::DOUBLE / (DATE '2025-12-31' - r.statin_ipsd + 1)) >= 0.80
         ELSE FALSE
    END AS rate2_numerator
FROM rate1 r
LEFT JOIN covered c ON c.member_id = r.member_id;
