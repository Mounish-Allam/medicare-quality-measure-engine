-- Grain: one row per member_id per measure, for every member who has a
-- computed PDC value (i.e. Measure 1's denominator, and SPD Rate 2's
-- denominator = SPD Rate 1's numerator). This feeds output/member_detail.csv.
--
-- gap_distance = pdc - 0.80: positive means the member cleared the adherence
-- threshold, negative means how far short they fell. Useful for a Tableau
-- distribution view of how close the population sits to the cut point.
--
-- age and sex are carried through from members (no new dimension table, no
-- new measure) purely so a downstream dashboard can filter/facet by them.

CREATE OR REPLACE TABLE member_detail AS
SELECT
    d.member_id,
    d.measure,
    m.sex,
    (DATE_PART('year', DATE '2025-12-31') - DATE_PART('year', m.birth_date))
        AS age_as_of_2025_12_31,
    d.treatment_period_start,
    d.treatment_period_end,
    d.days_in_treatment_period,
    d.covered_days,
    d.pdc,
    d.adherent,
    d.gap_distance
FROM (
    SELECT
        member_id,
        'PART_D_DIABETES_ADHERENCE' AS measure,
        ipsd AS treatment_period_start,
        period_end AS treatment_period_end,
        days_in_treatment_period,
        covered_days,
        ROUND(pdc, 4) AS pdc,
        adherent,
        ROUND(pdc - 0.80, 4) AS gap_distance
    FROM measure1_partd_diabetes

    UNION ALL

    SELECT
        member_id,
        'SPD_RATE2_STATIN_ADHERENCE' AS measure,
        statin_ipsd AS treatment_period_start,
        DATE '2025-12-31' AS treatment_period_end,
        days_in_treatment_period,
        covered_days,
        ROUND(pdc, 4) AS pdc,
        rate2_numerator AS adherent,
        ROUND(pdc - 0.80, 4) AS gap_distance
    FROM measure2_spd
    WHERE rate1_numerator
) d
JOIN members m ON m.member_id = d.member_id;
