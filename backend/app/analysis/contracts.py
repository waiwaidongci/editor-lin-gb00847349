from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing import Annotated

from domain.revisions import canonical_json, parse_bound
from domain.spec import OPTIONS_MODEL


class RunStatus(StrEnum):
    queued = 'queued'
    completed = 'completed'
    failed = 'failed'


class Decision(StrEnum):
    approved = 'approved'
    rejected = 'rejected'


CUTOFF_ALIASES = (
    'received_before', 'received_through', 'received_until', 'receipt_cutoff',
    'receipt_date', 'receipt_deadline', 'intake_cutoff', 'inbox_cutoff',
    'submission_cutoff', 'records_cutoff', 'cutoff', 'cutoff_date', 'as_of_receipt',
)
DATE_ALIASES = ('analysis_date', 'analysis_day', 'as_of', 'date')


class RunOptions(OPTIONS_MODEL):
    """Analysis-day options plus the independent intake/receipt cutoff."""

    model_config = ConfigDict(
        extra='forbid', allow_inf_nan=False, str_strip_whitespace=True, frozen=True
    )

    received_cutoff: Any = None
    received_before: Any = None
    received_through: Any = None
    receipt_cutoff: Any = None
    receipt_date: Any = None
    intake_cutoff: Any = None
    inbox_cutoff: Any = None
    submission_cutoff: Any = None
    records_cutoff: Any = None
    cutoff: Any = None
    as_of_receipt: Any = None
    analysis_date: Any = None
    analysis_day: Any = None
    as_of: Any = None
    cutoff_date: Any = None
    receipt_deadline: Any = None
    received_until: Any = None
    cutoff_inclusive: bool | None = None

    @model_validator(mode='before')
    @classmethod
    def normalize_date_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ('analysis_date', 'analysis_day', 'as_of', 'date'):
                if data.get(key) and not data.get('as_of_date'):
                    data['as_of_date'] = data[key]
        return data

    def receipt_bound(self) -> tuple[Any, bool]:
        for name in ('received_cutoff', 'receipt_cutoff', 'receipt_deadline', 'cutoff_date',
                     'intake_cutoff', 'inbox_cutoff', 'submission_cutoff', 'records_cutoff',
                     'received_until', 'received_through', 'as_of_receipt', 'cutoff',
                     'receipt_date', 'received_before'):
            value = getattr(self, name)
            if value is not None:
                if self.cutoff_inclusive is not None:
                    inclusive = self.cutoff_inclusive
                else:
                    inclusive = name != 'received_before'
                return value, inclusive
        return None, True

    def has_receipt_cutoff(self) -> bool:
        return self.receipt_bound()[0] is not None

    def storage_dict(self) -> dict:
        data = self.model_dump(mode='json')
        for name in ('received_cutoff', *CUTOFF_ALIASES, *DATE_ALIASES, 'cutoff_inclusive'):
            data.pop(name, None)
        raw, inclusive = self.receipt_bound()
        if raw is not None:
            bound = parse_bound(raw)
            data['received_cutoff'] = bound.isoformat() if bound is not None else None
            data['cutoff_inclusive'] = inclusive
        return data

    def domain_options(self) -> dict:
        data = self.storage_dict()
        data.pop('received_cutoff', None)
        data.pop('cutoff_inclusive', None)
        return data


class RunRequest(BaseModel):
    model_config = ConfigDict(extra='allow')
    options: RunOptions = Field(default_factory=RunOptions)
    idempotency_key: str | None = Field(default=None, max_length=120)

    def effective_options(self) -> RunOptions:
        data = self.options.model_dump(mode='json')
        for key, value in (self.model_extra or {}).items():
            if key == 'options':
                continue
            data[key] = value
        return RunOptions.model_validate(data)


class RerunRequest(BaseModel):
    model_config = ConfigDict(extra='allow')
    in_place: bool = True
    clone: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120)

    def effective_in_place(self) -> bool:
        if self.clone:
            return False
        mode = (self.model_extra or {}).get('mode')
        if mode in {'new', 'clone', 'new_run'}:
            return False
        if mode in {'same', 'in_place', 'retry'}:
            return True
        return self.in_place


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Decision
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]


def describe_review(review):
    from app.clock import utc_iso

    if review is None:
        return None
    if isinstance(review, str):  # Defensive: old call sites passed an actor here.
        return {'reviewer': review}
    return {'decision': review.decision, 'note': review.note, 'reviewer': review.reviewer,
            'created_at': utc_iso(review.created_at)}


def replay_view(run, replay) -> dict | None:
    if replay is None:
        return None
    from app.clock import utc_iso

    adopted = replay.adopted or []
    fingerprint_input = sorted(
        (item.get('editor_id'), item.get('revision'), item.get('content_checksum'))
        for item in adopted
    )
    manifest_checksum = sha256(canonical_json(fingerprint_input).encode()).hexdigest()
    options = run.options or {}
    return {
        'stream_key': replay.stream_key,
        'stream': replay.stream_name,
        'stream_name': replay.stream_name,
        'snapshot_dataset_id': replay.snapshot_dataset_id,
        'source_run_id': replay.source_run_id,
        'rerun_of': replay.source_run_id,
        'analysis_date': options.get('as_of_date'),
        'as_of_date': options.get('as_of_date'),
        'received_cutoff': options.get('received_cutoff'),
        'cutoff_inclusive': bool(replay.cutoff_inclusive),
        'frozen_at': utc_iso(replay.frozen_at),
        'frozen': True,
        'queue_frozen': True,
        'adopted_revisions': adopted,
        'adoptions': adopted,
        'revision_count': len(adopted),
        'manifest_checksum': manifest_checksum,
        'adoption_checksum': manifest_checksum,
    }
