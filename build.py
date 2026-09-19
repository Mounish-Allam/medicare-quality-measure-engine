"""
Build entry point: generates synthetic data, runs the full SQL pipeline
against a fresh DuckDB database, and exports the two output CSVs.

Usage:
    python build.py

Produces:
    output/measure_summary.csv
    output/member_detail.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

import generate_data

ROOT = Path(__file__).parent
SQL_DIR = ROOT / "sql"
OUTPUT_DIR = ROOT / "output"
DB_PATH = ROOT / "medicare_quality_measure_engine.duckdb"

SQL_FILES = [
    "01_create_tables.sql",
    "02_pdc_engine.sql",
    "03_measure_partd_diabetes.sql",
    "04_measure_spd.sql",
    "05_export_member_detail.sql",
]

# Floating-point tolerance for the "PDC must never exceed 1.00" invariant.
PDC_EPSILON = 1.0000001

# Expected outcome per edge-case member, from SPEC.md's edge-case table.
# Each predicate takes (measure1_row, measure2_row); a row is None if the
# member isn't in that table (excluded from the denominator/population).
EDGE_CASE_EXPECTATIONS = {
    "M_PERFECT": [
        ("in Measure 1 denominator", lambda m1, m2: m1 is not None),
        ("Measure 1 PDC ~= 1.00", lambda m1, m2: m1 and m1["pdc"] >= 0.99),
        ("Measure 1 adherent", lambda m1, m2: m1 and m1["adherent"]),
    ],
    "M_EARLY": [
        ("in Measure 1 denominator", lambda m1, m2: m1 is not None),
        ("Measure 1 PDC ~= 1.00, never > 1.00",
         lambda m1, m2: m1 and 0.99 <= m1["pdc"] <= PDC_EPSILON),
        ("Measure 1 adherent", lambda m1, m2: m1 and m1["adherent"]),
    ],
    "M_GAP60": [
        ("in Measure 1 denominator", lambda m1, m2: m1 is not None),
        ("Measure 1 PDC ~= 0.83 (60-day gap)",
         lambda m1, m2: m1 and 0.78 <= m1["pdc"] <= 0.88),
    ],
    "M_ONEFILL": [
        ("excluded from Measure 1 denominator (only 1 fill)",
         lambda m1, m2: m1 is None),
    ],
    "M_DIED": [
        ("in Measure 1 denominator", lambda m1, m2: m1 is not None),
        ("treatment period truncated at death_date 2025-08-15",
         lambda m1, m2: m1 and str(m1["period_end"]) == "2025-08-15"),
        ("Measure 1 PDC ~= 1.00 over the truncated period",
         lambda m1, m2: m1 and m1["pdc"] >= 0.99),
    ],
    "M_LATEENROLL": [
        ("excluded from SPD (fails continuous enrollment)",
         lambda m1, m2: m2 is None),
    ],
    "M_ASCVD": [
        ("excluded from SPD (ASCVD exclusion)", lambda m1, m2: m2 is None),
    ],
    "M_SWITCH": [
        ("in SPD population", lambda m1, m2: m2 is not None),
        ("SPD Rate 1 numerator (received statin)",
         lambda m1, m2: m2 and m2["rate1_numerator"]),
        ("SPD Rate 2 PDC ~= 1.00 across the drug-class switch",
         lambda m1, m2: m2 and m2["pdc"] is not None and m2["pdc"] >= 0.99),
        ("SPD Rate 2 adherent", lambda m1, m2: m2 and m2["rate2_numerator"]),
    ],
    "M_HOSPICE": [
        ("excluded from Measure 1 (hospice)", lambda m1, m2: m1 is None),
        ("excluded from SPD (hospice)", lambda m1, m2: m2 is None),
    ],
}

EDGE_CASE_MEMBERS = list(EDGE_CASE_EXPECTATIONS)


def run_sql_file(con: duckdb.DuckDBPyConnection, filename: str) -> None:
    sql_text = (SQL_DIR / filename).read_text()
    con.execute(sql_text)


def assert_no_pdc_over_one(con: duckdb.DuckDBPyConnection) -> None:
    """A PDC above 1.00 anywhere in the output is a bug -- fail the build."""
    bad = con.execute(
        f"""
        SELECT member_id, 'measure1_partd_diabetes' AS source, pdc
        FROM measure1_partd_diabetes
        WHERE pdc > {PDC_EPSILON}
        UNION ALL
        SELECT member_id, 'measure2_spd' AS source, pdc
        FROM measure2_spd
        WHERE pdc IS NOT NULL AND pdc > {PDC_EPSILON}
        """
    ).fetchall()
    if bad:
        raise AssertionError(f"PDC > 1.00 found for {len(bad)} row(s): {bad[:10]}")


def build_measure_summary(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE measure_summary AS
        SELECT
            'PART_D_DIABETES_ADHERENCE' AS measure,
            COUNT(*) AS denominator,
            SUM(CASE WHEN adherent THEN 1 ELSE 0 END) AS numerator,
            ROUND(SUM(CASE WHEN adherent THEN 1 ELSE 0 END)::DOUBLE / COUNT(*), 4) AS rate
        FROM measure1_partd_diabetes

        UNION ALL

        SELECT
            'SPD_RATE1_RECEIVED_STATIN' AS measure,
            COUNT(*) AS denominator,
            SUM(CASE WHEN rate1_numerator THEN 1 ELSE 0 END) AS numerator,
            ROUND(SUM(CASE WHEN rate1_numerator THEN 1 ELSE 0 END)::DOUBLE / COUNT(*), 4) AS rate
        FROM measure2_spd

        UNION ALL

        SELECT
            'SPD_RATE2_STATIN_ADHERENCE' AS measure,
            COUNT(*) AS denominator,
            SUM(CASE WHEN rate2_numerator THEN 1 ELSE 0 END) AS numerator,
            ROUND(SUM(CASE WHEN rate2_numerator THEN 1 ELSE 0 END)::DOUBLE / COUNT(*), 4) AS rate
        FROM measure2_spd
        WHERE rate1_numerator
        """
    )


def check_edge_cases(con: duckdb.DuckDBPyConnection) -> None:
    """Check every hand-constructed edge-case member against the expected
    outcome from SPEC.md's edge-case table. Raises AssertionError -- failing
    the build -- if any member doesn't match."""
    m1 = {
        row[0]: {"pdc": row[1], "adherent": row[2], "period_end": row[3]}
        for row in con.execute(
            "SELECT member_id, pdc, adherent, period_end FROM measure1_partd_diabetes"
        ).fetchall()
    }
    m2 = {
        row[0]: {"rate1_numerator": row[1], "pdc": row[2], "rate2_numerator": row[3]}
        for row in con.execute(
            "SELECT member_id, rate1_numerator, pdc, rate2_numerator FROM measure2_spd"
        ).fetchall()
    }

    print("\nEdge-case member checks:")
    failures = []
    for mid, checks in EDGE_CASE_EXPECTATIONS.items():
        m1_row = m1.get(mid)
        m2_row = m2.get(mid)
        for description, predicate in checks:
            ok = bool(predicate(m1_row, m2_row))
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {mid}: {description}")
            if not ok:
                failures.append(f"{mid}: {description}")

    if failures:
        raise AssertionError(
            f"{len(failures)} edge-case check(s) failed:\n  " + "\n  ".join(failures)
        )


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    con = duckdb.connect(str(DB_PATH))

    print("Creating tables...")
    run_sql_file(con, "01_create_tables.sql")

    print("Generating synthetic data...")
    generate_data.populate(con)

    print("Building PDC engine...")
    run_sql_file(con, "02_pdc_engine.sql")

    print("Computing Measure 1 (Part D diabetes adherence)...")
    run_sql_file(con, "03_measure_partd_diabetes.sql")

    print("Computing Measure 2 (SPD)...")
    run_sql_file(con, "04_measure_spd.sql")

    print("Checking PDC <= 1.00 invariant...")
    assert_no_pdc_over_one(con)
    print("  OK: no PDC value exceeds 1.00")

    check_edge_cases(con)
    print("  OK: all 9 edge-case members match their expected outcome")

    print("Exporting member detail...")
    run_sql_file(con, "05_export_member_detail.sql")

    print("Building measure summary...")
    build_measure_summary(con)

    print("\nMeasure summary:")
    summary_rows = con.execute("SELECT * FROM measure_summary").fetchall()
    for row in summary_rows:
        print(f"  {row[0]:30} denom={row[1]:4} numer={row[2]:4} rate={row[3]}")

    con.execute(
        f"COPY measure_summary TO '{OUTPUT_DIR / 'measure_summary.csv'}' (HEADER, DELIMITER ',')"
    )
    con.execute(
        f"COPY member_detail TO '{OUTPUT_DIR / 'member_detail.csv'}' (HEADER, DELIMITER ',')"
    )
    print(f"\nWrote {OUTPUT_DIR / 'measure_summary.csv'}")
    print(f"Wrote {OUTPUT_DIR / 'member_detail.csv'}")

    con.close()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nBUILD FAILED: {e}", file=sys.stderr)
        sys.exit(1)
