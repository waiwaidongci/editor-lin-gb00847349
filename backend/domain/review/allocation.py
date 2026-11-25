"""Check sample-ratio mismatch against the planned Bernoulli assignment share."""

from math import exp, fsum, lgamma, log, log1p


def binomial_two_sided(successes: int, total: int, share: float) -> float:
    log_p, log_q = log(share), log1p(-share)
    log_total_factorial = lgamma(total + 1)

    def log_mass(count: int) -> float:
        log_coefficient = log_total_factorial - (lgamma(count + 1) + lgamma(total - count + 1))
        log_probability = count * log_p + (total - count) * log_q
        return log_coefficient + log_probability

    observed = log_mass(successes)
    return min(1.0, fsum(exp(mass) for count in range(total + 1)
                         if (mass := log_mass(count)) <= observed + 1e-10))


def allocation_check(
    stage: str, control: int, treatment: int, treatment_share: float, alpha: float
) -> dict:
    total = control + treatment
    p_value = binomial_two_sided(treatment, total, treatment_share) if total else None
    return {
        "stage": stage,
        "control": control,
        "treatment": treatment,
        "expected_treatment_share": treatment_share,
        "observed_treatment_share": treatment / total if total else None,
        "p_value": p_value,
        "alpha": alpha,
        "status": "unavailable" if p_value is None else "mismatch" if p_value < alpha else "pass",
    }
