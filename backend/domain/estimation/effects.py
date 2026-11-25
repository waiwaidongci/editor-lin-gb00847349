"""Fit Lin's interacted OLS by estimating a separate baseline slope in each arm."""

from math import fsum, sqrt
from statistics import mean
from typing import NamedTuple

from ..cohort.models import EditorRow
from .intervals import estimate, hc3_prediction_variance, raw_standard_error


class ArmFit(NamedTuple):
    prediction: float
    slope: float
    variance: float | None


def fit_arm(baselines: list[float], outcomes: list[float], reference: float) -> ArmFit | None:
    x_mean, y_mean = mean(baselines), mean(outcomes)
    centered_x = [x - x_mean for x in baselines]
    centered_y = [y - y_mean for y in outcomes]
    sxx = fsum(x * x for x in centered_x)
    if sxx <= len(baselines) * (1e-9 ** 2):
        return None
    slope = fsum(x * y for x, y in zip(centered_x, centered_y)) / sxx
    offset = reference - x_mean
    residuals = [y - slope * x for x, y in zip(centered_x, centered_y)]
    return ArmFit(y_mean + slope * offset, slope,
                  hc3_prediction_variance(centered_x, residuals, offset, sxx))


def estimate_effects(
    control: list[EditorRow], treatment: list[EditorRow], confidence: float,
    reference: float | None,
) -> tuple[list[dict], dict]:
    raw_diff = lin_diff = raw_se = lin_se = None
    diagnostics = {
        "lin_status": "insufficient_editors",
        "reference_baseline_minutes": reference,
        "baseline_difference_minutes": None,
        "control_slope": None,
        "treatment_slope": None,
        "slope_difference": None,
        "control_adjusted_minutes": None,
        "treatment_adjusted_minutes": None,
        "standard_error_reduction": None,
    }
    if reference is not None and control and treatment:
        control_x = [row.pre_minutes for row in control]
        treatment_x = [row.pre_minutes for row in treatment]
        control_y = [row.post_minutes for row in control]
        treatment_y = [row.post_minutes for row in treatment]
        raw_diff = mean(treatment_y) - mean(control_y)
        diagnostics["baseline_difference_minutes"] = mean(treatment_x) - mean(control_x)
        if min(len(control), len(treatment)) >= 2:
            raw_se = raw_standard_error(control_y, treatment_y)
            control_fit = fit_arm(control_x, control_y, reference)
            treatment_fit = fit_arm(treatment_x, treatment_y, reference)
            if control_fit is None or treatment_fit is None:
                diagnostics["lin_status"] = "no_within_arm_baseline_variation"
            else:
                lin_diff = treatment_fit.prediction - control_fit.prediction
                diagnostics.update(
                    control_slope=control_fit.slope,
                    treatment_slope=treatment_fit.slope,
                    slope_difference=treatment_fit.slope - control_fit.slope,
                    control_adjusted_minutes=control_fit.prediction,
                    treatment_adjusted_minutes=treatment_fit.prediction,
                )
                if control_fit.variance is None or treatment_fit.variance is None:
                    diagnostics["lin_status"] = "high_leverage"
                else:
                    lin_se = sqrt(control_fit.variance + treatment_fit.variance)
                    diagnostics["lin_status"] = "estimated"
                    if raw_se > 0:
                        diagnostics["standard_error_reduction"] = 1 - lin_se / raw_se
    return [estimate("raw", raw_diff, raw_se, confidence),
            estimate("lin", lin_diff, lin_se, confidence)], diagnostics
