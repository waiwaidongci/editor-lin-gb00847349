"""Assess observed changes in editorial quality using manuscript denominators."""

from fractions import Fraction

from ..cohort.models import EditorRow, ExperimentOptions


def guardrail_checks(
    control: list[EditorRow], treatment: list[EditorRow], options: ExperimentOptions
) -> list[dict]:
    control_total = sum(row.assigned_manuscripts for row in control)
    treatment_total = sum(row.assigned_manuscripts for row in treatment)
    results = []
    for metric, field, limit in (
        ("sla_breach_rate", "sla_breaches", options.max_sla_breach_increase),
        ("rework_rate", "rework_manuscripts", options.max_rework_increase),
    ):
        control_events = sum(getattr(row, field) for row in control)
        treatment_events = sum(getattr(row, field) for row in treatment)
        control_rate = Fraction(control_events, control_total) if control_total else None
        treatment_rate = Fraction(treatment_events, treatment_total) if treatment_total else None
        difference = None
        status = "unavailable"
        if control_rate is not None and treatment_rate is not None:
            difference = treatment_rate - control_rate
            status = "breach" if difference > Fraction(str(limit)) else "pass"
        results.append({
            "metric": metric,
            "control_events": control_events,
            "control_manuscripts": control_total,
            "control_rate": float(control_rate) if control_rate is not None else None,
            "treatment_events": treatment_events,
            "treatment_manuscripts": treatment_total,
            "treatment_rate": float(treatment_rate) if treatment_rate is not None else None,
            "difference": float(difference) if difference is not None else None,
            "max_increase": limit,
            "status": status,
        })
    return results
