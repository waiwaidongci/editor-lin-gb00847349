"""Assemble the publication decision and JSON-friendly report tables."""

from statistics import mean

from ..cohort.models import WINDOW_DAYS, ExperimentOptions
from ..cohort.snapshot import ArmSnapshot
from ..estimation.effects import estimate_effects
from ..review.allocation import allocation_check
from ..review.guardrails import guardrail_checks


def build_report(snapshot: dict[str, ArmSnapshot], options: ExperimentOptions) -> dict:
    control, treatment = snapshot["control"], snapshot["treatment"]
    mature_baselines = [row.pre_minutes for arm in snapshot.values() for row in arm.ready]
    for arm in snapshot.values():
        mature_baselines.extend(arm.missing_baselines)
    reference = mean(mature_baselines) if mature_baselines else None
    effects, diagnostics = estimate_effects(
        control.ready, treatment.ready, options.confidence_level, reference
    )
    allocations = [
        allocation_check("all_assigned", control.assigned, treatment.assigned,
                         options.treatment_share, options.srm_alpha),
        allocation_check("calendar_mature", control.mature, treatment.mature,
                         options.treatment_share, options.srm_alpha),
    ]
    guardrails = guardrail_checks(control.ready, treatment.ready, options)
    maturity = [
        {"arm": arm_name, "future": arm.future, "assigned": arm.assigned, "pending": arm.pending,
         "calendar_mature": arm.mature, "missing_outcome": arm.missing, "analyzed": len(arm.ready),
         "minimum_required": options.min_editors_per_arm}
        for arm_name, arm in snapshot.items()
    ]
    totals = {key: sum(arm[key] for arm in maturity) for key in (
        "future", "assigned", "pending", "calendar_mature", "missing_outcome", "analyzed"
    )}
    blockers = []
    notices = []
    if totals["pending"]:
        notices.append(f"{totals['pending']} 名已分配编辑仍在 {WINDOW_DAYS} 天观察窗内。")
    if totals["future"]:
        notices.append(f"{totals['future']} 名编辑的分配晚于分析日，已从当期分流检查排除。")
    if min(len(control.ready), len(treatment.ready)) < options.min_editors_per_arm:
        blockers.append(f"每组至少需要 {options.min_editors_per_arm} 名已成熟且结果齐全的编辑。")
    if totals["missing_outcome"]:
        blockers.append(f"{totals['missing_outcome']} 名编辑的窗口已成熟但仍缺实验期结果。")
    for allocation, label in zip(allocations, ("全部已分配编辑", "已成熟编辑")):
        if allocation["status"] == "mismatch":
            blockers.append(f"{label}的分流比例与预设比例不符。")
        elif allocation["status"] == "unavailable":
            blockers.append(f"{label}为零，无法检查分流比例。")
    if diagnostics["lin_status"] == "no_within_arm_baseline_variation":
        blockers.append("至少一组的预实验耗时变动不足，无法识别 Lin 交互回归。")
    elif diagnostics["lin_status"] == "high_leverage":
        blockers.append("至少一名编辑的杠杆值过高，无法计算 Lin 的 HC3 区间。")
    for guardrail, label in zip(guardrails, ("初审超时率", "初审返工率")):
        if guardrail["status"] == "breach":
            blockers.append(f"{label}的实验组增幅超过预设护栏。")
        elif guardrail["status"] == "unavailable":
            blockers.append(f"{label}缺少某一组的稿件分母。")
    return {
        "summary": {
            "as_of_date": options.as_of_date.isoformat(),
            "window_days": WINDOW_DAYS,
            "editor_counts": totals,
            "raw_difference_minutes": effects[0]["difference_minutes"],
            "lin_difference_minutes": effects[1]["difference_minutes"],
            **diagnostics,
        },
        "tables": {
            "effect_estimates": effects,
            "maturity": maturity,
            "allocation": allocations,
            "guardrails": guardrails,
        },
        "warnings": blockers + notices,
        "publishable": not blockers,
    }
