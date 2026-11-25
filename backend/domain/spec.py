"""Public interface for the academic editorial task-bar experiment."""

from collections.abc import Iterable, Mapping
from typing import Any

from .cohort.models import EditorRow, ExperimentOptions
from .cohort.snapshot import RowInput, build_snapshot, record_key, validate_rows, validated_editors
from .reporting.demo import SEED_NAME, seed
from .reporting.report import build_report

TITLE = "学术期刊编辑任务栏实验"
METHOD = "editor-lin-hc3"
ROW_MODEL = EditorRow
OPTIONS_MODEL = ExperimentOptions

__all__ = ["TITLE", "METHOD", "SEED_NAME", "ROW_MODEL", "OPTIONS_MODEL",
           "record_key", "validate_rows", "analyze", "seed"]


def analyze(
    rows: Iterable[RowInput], options: Mapping[str, Any] | ExperimentOptions | None = None
) -> dict:
    """Analyze one assignment ledger at the requested calendar cutoff."""
    settings = ExperimentOptions.model_validate({} if options is None else options)
    return build_report(build_snapshot(validated_editors(rows), settings.as_of_date), settings)
