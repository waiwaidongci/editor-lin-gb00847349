"""CSV contract for one randomized editor and their fixed observation windows."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

ARMS = ("control", "treatment")
WINDOW_DAYS = 14


def parse_day(value: object) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    raise ValueError("日期须使用 YYYY-MM-DD")


Day = Annotated[date, BeforeValidator(parse_day)]
Minutes = Annotated[float, Field(ge=0, le=WINDOW_DAYS * 24 * 60)]
Manuscripts = Annotated[int, Field(ge=0, le=1_000_000)]


class DomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, str_strip_whitespace=True, frozen=True
    )


class EditorRow(DomainModel):
    editor_id: str = Field(min_length=1, max_length=80)
    arm: Literal["control", "treatment"]
    assigned_date: Day
    pre_minutes: Minutes
    post_minutes: Minutes | None = None
    assigned_manuscripts: Manuscripts | None = None
    sla_breaches: Manuscripts | None = None
    rework_manuscripts: Manuscripts | None = None

    @field_validator(
        "pre_minutes", "post_minutes", "assigned_manuscripts", "sla_breaches",
        "rework_manuscripts", mode="before"
    )
    @classmethod
    def parse_csv_number(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("须提供数值")
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def check_snapshot(self) -> "EditorRow":
        outcome = (self.post_minutes, self.assigned_manuscripts,
                   self.sla_breaches, self.rework_manuscripts)
        if all(value is None for value in outcome):
            return self
        if any(value is None for value in outcome):
            raise ValueError("实验期耗时、稿件数和两项护栏计数须一并填写或留空")
        if max(self.sla_breaches, self.rework_manuscripts) > self.assigned_manuscripts:
            raise ValueError("护栏计数不可超过实验期分配稿件数")
        if self.assigned_manuscripts == 0 and self.post_minutes != 0:
            raise ValueError("实验期无分配稿件时，初审耗时须为零")
        return self


class ExperimentOptions(DomainModel):
    as_of_date: Day = date(2026, 11, 25)
    min_editors_per_arm: int = Field(default=20, ge=2, le=100_000)
    treatment_share: float = Field(default=0.5, ge=0.05, le=0.95)
    srm_alpha: float = Field(default=0.01, gt=0, le=0.2)
    confidence_level: float = Field(default=0.95, ge=0.80, le=0.999)
    max_sla_breach_increase: float = Field(default=0.03, ge=0, le=1)
    max_rework_increase: float = Field(default=0.025, ge=0, le=1)

    @field_validator(
        "min_editors_per_arm", "treatment_share", "srm_alpha", "confidence_level",
        "max_sla_breach_increase", "max_rework_increase", mode="before"
    )
    @classmethod
    def reject_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("须提供数值")
        return value
