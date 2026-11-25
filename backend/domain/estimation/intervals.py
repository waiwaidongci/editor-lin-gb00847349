"""Editor-level sampling uncertainty for the unadjusted and adjusted effects."""

from math import fsum, sqrt
from statistics import NormalDist, variance


def raw_standard_error(control: list[float], treatment: list[float]) -> float:
    return sqrt(variance(control) / len(control) + variance(treatment) / len(treatment))


def hc3_prediction_variance(
    centered_x: list[float], residuals: list[float], offset: float, sxx: float
) -> float | None:
    """Variance of one arm's OLS mean prediction at the pooled reference value."""
    base_weight = 1 / len(centered_x)
    contributions = []
    for x, residual in zip(centered_x, residuals):
        leverage_complement = 1 - (base_weight + x * x / sxx)
        if leverage_complement <= 1e-12:
            return None
        weight = base_weight + offset * x / sxx
        contributions.append((weight * residual / leverage_complement) ** 2)
    return fsum(contributions)


def estimate(
    method: str, difference: float | None, standard_error: float | None, confidence: float
) -> dict:
    lower = upper = None
    if difference is not None and standard_error is not None:
        margin = NormalDist().inv_cdf((1 + confidence) / 2) * standard_error
        lower, upper = difference - margin, difference + margin
    return {
        "method": method,
        "difference_minutes": difference,
        "standard_error_minutes": standard_error,
        "ci_lower_minutes": lower,
        "ci_upper_minutes": upper,
        "confidence_level": confidence,
    }
