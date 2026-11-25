"""Deduplicate assignment units and classify the snapshot by calendar maturity."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .models import ARMS, WINDOW_DAYS, EditorRow

RowInput = Mapping[str, Any] | EditorRow


def record_key(row: RowInput) -> str:
    return row.editor_id if isinstance(row, EditorRow) else str(row["editor_id"]).strip()


def validated_editors(rows: Iterable[RowInput]) -> list[EditorRow]:
    if not isinstance(rows, Iterable) or isinstance(rows, (Mapping, str, bytes)):
        raise ValueError("编辑分配须提供为行序列")
    editors = {}
    for number, row in enumerate(rows, start=1):
        try:
            editor = EditorRow.model_validate(row)
        except ValueError as exc:
            raise ValueError(f"第 {number} 条编辑分配无效：{exc}") from exc
        key = record_key(editor)
        if key in editors:
            raise ValueError(f"编辑 {key} 有重复分配记录")
        editors[key] = editor
    return sorted(editors.values(), key=record_key)


def validate_rows(rows: Iterable[RowInput]) -> None:
    validated_editors(rows)


@dataclass
class ArmSnapshot:
    future: int = 0
    pending: int = 0
    missing_baselines: list[float] = field(default_factory=list)
    ready: list[EditorRow] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return len(self.missing_baselines)

    @property
    def mature(self) -> int:
        return self.missing + len(self.ready)

    @property
    def assigned(self) -> int:
        return self.pending + self.mature


def build_snapshot(editors: list[EditorRow], as_of_date: date) -> dict[str, ArmSnapshot]:
    snapshot = {arm: ArmSnapshot() for arm in ARMS}
    for editor in editors:
        arm = snapshot[editor.arm]
        age = (as_of_date - editor.assigned_date).days
        if age < 0:
            arm.future += 1
        elif age < WINDOW_DAYS:
            arm.pending += 1
        elif editor.post_minutes is None:
            arm.missing_baselines.append(editor.pre_minutes)
        else:
            arm.ready.append(editor)
    return snapshot
