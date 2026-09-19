"""
Synthetic data generator for the Medicare Quality Measure Engine.

Produces four tables matching the schema in SPEC.md. Nine members are
hardcoded edge cases (see docs/hand_verification.md); the rest are randomly
generated with a fixed seed. No measure logic lives here -- that's sql/*.sql.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

MEASUREMENT_YEAR_START = date(2025, 1, 1)
MEASUREMENT_YEAR_END = date(2025, 12, 31)
PRIOR_YEAR_START = date(2024, 1, 1)

PLAN_ID = "PLN001"

# Illustrative drug list, not an NCQA HEDIS value set (licensed, not
# redistributable here) -- see README.
DRUG_REFERENCE = [
    ("metformin", "DIABETES"),
    ("glipizide", "DIABETES"),
    ("sitagliptin", "DIABETES"),
    ("insulin_glargine", "DIABETES"),
    ("atorvastatin", "STATIN"),
    ("rosuvastatin", "STATIN"),
    ("simvastatin", "STATIN"),
    ("pravastatin", "STATIN"),
]

DIABETES_DRUGS = [d for d, c in DRUG_REFERENCE if c == "DIABETES"]
STATIN_DRUGS = [d for d, c in DRUG_REFERENCE if c == "STATIN"]

# Illustrative prescribing shares (metformin/atorvastatin dominant).
DIABETES_DRUG_WEIGHTS = [0.55, 0.15, 0.15, 0.15]  # metformin, glipizide, sitagliptin, insulin_glargine
STATIN_DRUG_WEIGHTS = [0.45, 0.25, 0.20, 0.10]  # atorvastatin, rosuvastatin, simvastatin, pravastatin

# Age bands weighted toward a Medicare-heavy population.
AGE_BANDS = [
    (22, 39, 0.08),
    (40, 64, 0.22),
    (65, 79, 0.45),
    (80, 90, 0.25),
]


class IdGen:
    """Sequential ID generator for fills so IDs are stable across runs."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:06d}"


def fill_series(
    start: date,
    end: date,
    days_supply: int,
    interval_days: int,
) -> list[tuple[date, int]]:
    """Generate (fill_date, days_supply) pairs on a fixed refill interval,
    starting at `start`, continuing while fill_date <= end."""
    out = []
    current = start
    while current <= end:
        out.append((current, days_supply))
        current = current + timedelta(days=interval_days)
    return out


def random_fill_series(
    rng: random.Random,
    start: date,
    end: date,
    days_supply_choices: list[int],
    adherence_quality: float,
) -> list[tuple[date, int]]:
    """Simulate a member's refill pattern over [start, end] with a given
    adherence quality in [0, 1]. Higher quality -> more on-time refills,
    fewer/smaller gaps. Not used for the 9 hardcoded edge cases."""
    out: list[tuple[date, int]] = []
    current = start
    while current <= end:
        days_supply = rng.choice(days_supply_choices)
        out.append((current, days_supply))
        if rng.random() < adherence_quality:
            # on-time to slightly early refill
            offset = max(1, days_supply - rng.randint(0, 5))
        else:
            # a real gap
            offset = days_supply + rng.randint(5, 90)
        current = current + timedelta(days=offset)
    return out


def build_fixed_members() -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Return (members, pharmacy_fills, conditions) rows for the 9 required
    hardcoded edge-case members. Each scenario is documented inline; the
    expected result is the one listed in SPEC.md's edge-case table."""
    members: list[tuple] = []
    fills: list[tuple] = []
    conditions: list[tuple] = []
    fid = IdGen("FIX")

    def add_member(member_id, birth_date, sex, coverage_start, coverage_end, death_date):
        members.append((member_id, birth_date, sex, coverage_start, coverage_end, death_date, PLAN_ID))

    # M_PERFECT: clean 30-day fills all year -- PDC ~1.00.
    add_member("M_PERFECT", date(1958, 5, 10), "M", date(2023, 1, 1), None, None)
    for fdate, ds in fill_series(date(2025, 1, 1), MEASUREMENT_YEAR_END, 30, 30):
        fills.append((fid.next(), "M_PERFECT", "metformin", fdate, ds))

    # M_EARLY: 20-day refills of a 30-day supply -- tests the early-refill
    # shift; PDC ~1.00, never > 1.00.
    add_member("M_EARLY", date(1960, 3, 22), "F", date(2023, 1, 1), None, None)
    for fdate, ds in fill_series(date(2025, 1, 1), MEASUREMENT_YEAR_END, 30, 20):
        fills.append((fid.next(), "M_EARLY", "metformin", fdate, ds))

    # M_GAP60: a true ~60-day gap mid-year -- PDC ~0.80-0.85.
    add_member("M_GAP60", date(1962, 11, 2), "M", date(2023, 1, 1), None, None)
    pre_gap = fill_series(date(2025, 1, 1), date(2025, 5, 15), 30, 30)
    for fdate, ds in pre_gap:
        fills.append((fid.next(), "M_GAP60", "metformin", fdate, ds))
    last_fill_date, last_ds = pre_gap[-1]
    covered_through = last_fill_date + timedelta(days=last_ds - 1)
    resume_date = covered_through + timedelta(days=1 + 60)  # 60-day true gap
    for fdate, ds in fill_series(resume_date, MEASUREMENT_YEAR_END, 30, 30):
        fills.append((fid.next(), "M_GAP60", "metformin", fdate, ds))

    # M_ONEFILL: only one diabetes fill -- fails the >=2-fills denominator test.
    add_member("M_ONEFILL", date(1965, 7, 19), "F", date(2023, 1, 1), None, None)
    fills.append((fid.next(), "M_ONEFILL", "metformin", date(2025, 6, 1), 30))

    # M_DIED: dies 2025-08-15 -- treatment period must truncate at death_date.
    add_member("M_DIED", date(1955, 2, 14), "M", date(2023, 1, 1), None, date(2025, 8, 15))
    for fdate, ds in fill_series(date(2025, 1, 1), date(2025, 8, 15), 30, 30):
        fills.append((fid.next(), "M_DIED", "metformin", fdate, ds))

    # M_LATEENROLL: coverage starts 2025-03-01 -- fails SPD continuous enrollment.
    add_member("M_LATEENROLL", date(1970, 9, 9), "F", date(2025, 3, 1), None, None)
    fills.append((fid.next(), "M_LATEENROLL", "metformin", date(2025, 4, 1), 30))
    fills.append((fid.next(), "M_LATEENROLL", "metformin", date(2025, 5, 1), 30))

    # M_ASCVD: diabetic with an ASCVD dx -- excluded from SPD.
    add_member("M_ASCVD", date(1968, 1, 30), "M", date(2023, 1, 1), None, None)
    conditions.append(("M_ASCVD", "DIABETES", date(2024, 3, 1)))
    conditions.append(("M_ASCVD", "DIABETES", date(2025, 3, 1)))
    conditions.append(("M_ASCVD", "ASCVD", date(2025, 4, 15)))
    fills.append((fid.next(), "M_ASCVD", "atorvastatin", date(2025, 5, 1), 30))
    fills.append((fid.next(), "M_ASCVD", "atorvastatin", date(2025, 6, 1), 30))

    # M_SWITCH: atorvastatin then rosuvastatin mid-year -- coverage must carry
    # through the drug-class switch with no gap.
    add_member("M_SWITCH", date(1972, 6, 6), "F", date(2023, 1, 1), None, None)
    conditions.append(("M_SWITCH", "DIABETES", date(2024, 2, 1)))
    conditions.append(("M_SWITCH", "DIABETES", date(2025, 2, 1)))
    switch_date = date(2025, 7, 1)
    pre_switch = fill_series(date(2025, 1, 1), switch_date - timedelta(days=1), 30, 30)
    for fdate, ds in pre_switch:
        fills.append((fid.next(), "M_SWITCH", "atorvastatin", fdate, ds))
    last_fill_date, last_ds = pre_switch[-1]
    covered_through = last_fill_date + timedelta(days=last_ds - 1)
    post_switch_start = covered_through + timedelta(days=1)
    for fdate, ds in fill_series(post_switch_start, MEASUREMENT_YEAR_END, 30, 30):
        fills.append((fid.next(), "M_SWITCH", "rosuvastatin", fdate, ds))

    # M_HOSPICE: qualifying diabetic with a hospice dx -- excluded from both measures.
    add_member("M_HOSPICE", date(1959, 10, 20), "M", date(2023, 1, 1), None, None)
    conditions.append(("M_HOSPICE", "DIABETES", date(2024, 5, 1)))
    conditions.append(("M_HOSPICE", "DIABETES", date(2025, 5, 1)))
    conditions.append(("M_HOSPICE", "HOSPICE", date(2025, 6, 1)))
    fills.append((fid.next(), "M_HOSPICE", "metformin", date(2025, 1, 1), 30))
    fills.append((fid.next(), "M_HOSPICE", "metformin", date(2025, 2, 1), 30))
    fills.append((fid.next(), "M_HOSPICE", "atorvastatin", date(2025, 1, 1), 30))
    fills.append((fid.next(), "M_HOSPICE", "atorvastatin", date(2025, 2, 1), 30))

    return members, fills, conditions


def build_random_members(
    rng: random.Random, count: int
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    members: list[tuple] = []
    fills: list[tuple] = []
    conditions: list[tuple] = []
    fid = IdGen("RND")

    n = 0
    while n < count:
        member_id = f"M{n + 1:04d}"

        lo, hi, _ = rng.choices(AGE_BANDS, weights=[w for _, _, w in AGE_BANDS])[0]
        age_years = rng.randint(lo, hi)
        birth_date = MEASUREMENT_YEAR_END - timedelta(days=int(age_years * 365.25))
        # Slight female skew, consistent with an older population.
        sex = rng.choices(["F", "M"], weights=[0.53, 0.47])[0]

        # ~85% continuously enrolled, ~10% late 2025 enrollees, ~5% mid-2024.
        r = rng.random()
        if r < 0.85:
            coverage_start = date(rng.randint(2018, 2023), rng.randint(1, 12), rng.randint(1, 28))
        elif r < 0.95:
            coverage_start = date(2025, rng.randint(1, 11), rng.randint(1, 28))
        else:
            coverage_start = date(2024, rng.randint(2, 12), rng.randint(1, 28))

        coverage_end = None
        if rng.random() < 0.06:
            coverage_end = date(2025, rng.randint(1, 12), rng.randint(1, 28))

        death_date = None
        if rng.random() < 0.03:
            death_date = date(2025, rng.randint(1, 12), rng.randint(1, 28))
            if coverage_end is None or coverage_end > death_date:
                coverage_end = death_date

        members.append((member_id, birth_date, sex, coverage_start, coverage_end, death_date, PLAN_ID))

        # Bound fill generation to when the member was covered and alive.
        coverage_effective_end = MEASUREMENT_YEAR_END
        if coverage_end is not None:
            coverage_effective_end = min(coverage_effective_end, coverage_end)
        if death_date is not None:
            coverage_effective_end = min(coverage_effective_end, death_date)

        has_diabetes = rng.random() < 0.55
        if has_diabetes:
            n_dx = rng.choice([0, 1, 2, 2, 3])
            dx_dates = sorted(
                {
                    date(
                        rng.choice([2024, 2025]),
                        rng.randint(1, 12),
                        rng.randint(1, 28),
                    )
                    for _ in range(n_dx)
                }
            )
            for d in dx_dates:
                conditions.append((member_id, "DIABETES", d))

            # Real PDC distributions are bimodal -- mix two beta distributions
            # instead of drawing from one.
            if rng.random() < 0.65:
                adherence = rng.betavariate(6, 1.5)  # high-adherence cluster, mean ~0.80
            else:
                adherence = rng.betavariate(1.5, 4)  # low-adherence cluster, mean ~0.27
            # 90-day supply correlates with better adherence.
            days_supply_choices = [90, 90, 30] if adherence >= 0.6 else [30, 30, 90]

            fill_start = date(2025, rng.randint(1, 4), rng.randint(1, 28))
            if fill_start <= coverage_effective_end:
                drug = rng.choices(DIABETES_DRUGS, weights=DIABETES_DRUG_WEIGHTS)[0]
                for fdate, ds in random_fill_series(
                    rng, fill_start, coverage_effective_end, days_supply_choices, adherence
                ):
                    fills.append((fid.next(), member_id, drug, fdate, ds))

            if rng.random() < 0.55:
                if rng.random() < 0.60:
                    statin_adherence = rng.betavariate(6, 1.5)
                else:
                    statin_adherence = rng.betavariate(1.5, 4)
                statin_days_supply_choices = [90, 90, 30] if statin_adherence >= 0.6 else [30, 30, 90]
                statin_start = date(2025, rng.randint(1, 6), rng.randint(1, 28))
                if statin_start <= coverage_effective_end:
                    statin_drug = rng.choices(STATIN_DRUGS, weights=STATIN_DRUG_WEIGHTS)[0]
                    for fdate, ds in random_fill_series(
                        rng, statin_start, coverage_effective_end, statin_days_supply_choices, statin_adherence
                    ):
                        fills.append((fid.next(), member_id, statin_drug, fdate, ds))

            # ~20% ASCVD prevalence among diagnosed diabetics.
            if rng.random() < 0.20:
                conditions.append((member_id, "ASCVD", date(2025, rng.randint(1, 12), rng.randint(1, 28))))

            if rng.random() < 0.02:
                conditions.append((member_id, "HOSPICE", date(2025, rng.randint(1, 12), rng.randint(1, 28))))

        n += 1

    return members, fills, conditions


def generate(total_members: int = 500, seed: int = 42) -> dict[str, list[tuple]]:
    """Build the full synthetic dataset. Returns a dict of table name ->
    list of row tuples, in the column order used by sql/01_create_tables.sql."""
    rng = random.Random(seed)

    fixed_members, fixed_fills, fixed_conditions = build_fixed_members()

    random_count = total_members - len(fixed_members)
    rand_members, rand_fills, rand_conditions = build_random_members(rng, random_count)

    return {
        "drug_reference": list(DRUG_REFERENCE),
        "members": fixed_members + rand_members,
        "pharmacy_fills": fixed_fills + rand_fills,
        "conditions": fixed_conditions + rand_conditions,
    }


def populate(con, seed: int = 42, total_members: int = 500) -> None:
    """Load generated data into an already-schema'd DuckDB connection
    (tables must already exist -- see sql/01_create_tables.sql)."""
    data = generate(total_members=total_members, seed=seed)

    con.executemany("INSERT INTO drug_reference VALUES (?, ?)", data["drug_reference"])
    con.executemany("INSERT INTO members VALUES (?, ?, ?, ?, ?, ?, ?)", data["members"])
    con.executemany("INSERT INTO pharmacy_fills VALUES (?, ?, ?, ?, ?)", data["pharmacy_fills"])
    con.executemany("INSERT INTO conditions VALUES (?, ?, ?)", data["conditions"])


if __name__ == "__main__":
    import duckdb

    con = duckdb.connect(":memory:")
    with open("sql/01_create_tables.sql") as f:
        con.execute(f.read())
    populate(con)
    for table in ("members", "drug_reference", "pharmacy_fills", "conditions"):
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {n} rows")
