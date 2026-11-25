"""Reproducible Bernoulli assignment and simulated editorial activity."""

from datetime import date, timedelta
from random import Random

SEED_NAME = "澄鉴期刊任务栏实验_20261125.csv"
RANDOM_SEED = 840625


def seed() -> list[dict]:
    assignment_rng = Random(RANDOM_SEED)
    activity_rng = Random(RANDOM_SEED + 1)
    rows = []
    for i in range(120):
        treatment = assignment_rng.random() < 0.5
        mature = i < 104
        assigned_date = (date(2026, 11, 3) + timedelta(days=i % 9) if mature
                         else date(2026, 11, 15) + timedelta(days=i % 4))
        pre_minutes = round(activity_rng.triangular(380, 1450, 820), 2)
        row = {
            "editor_id": f"E-{i + 1:03d}",
            "arm": "treatment" if treatment else "control",
            "assigned_date": assigned_date.isoformat(),
            "pre_minutes": pre_minutes,
            "post_minutes": None,
            "assigned_manuscripts": None,
            "sla_breaches": None,
            "rework_manuscripts": None,
        }
        if mature:
            manuscripts = activity_rng.randint(18, 42)
            row.update(
                post_minutes=round(170 + 0.85 * pre_minutes + activity_rng.gauss(0, 65)
                                   - 65 * treatment, 2),
                assigned_manuscripts=manuscripts,
                sla_breaches=sum(activity_rng.random() < 0.055 for _ in range(manuscripts)),
                rework_manuscripts=sum(activity_rng.random() < 0.09 - 0.008 * treatment
                                       for _ in range(manuscripts)),
            )
        rows.append(row)
    return rows
