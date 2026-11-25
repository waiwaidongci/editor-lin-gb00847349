from datetime import datetime

from sqlalchemy import func, select

from app.analysis.contracts import RunOptions
from app.analysis.models import AnalysisRun, RunReplay
from app.clock import now_utc
from app.data.models import Dataset, Observation
from app.data.repository import freeze_dataset, get_dataset
from app.database import Session
from app.errors import Problem
from app.intake.models import EditorRevisionRecord
from app.intake.repository import (
    ensure_dataset_revision_ledger,
    load_stream_revisions,
    revision_to_replay_input,
    stream_key_for_name,
)
from app.ops.audit import audit
from app.ops.redis_bus import wake_worker
from domain.revisions import (
    RevisionConflict as DomainConflict,
    RevisionReplayError,
    canonical_json,
    parse_bound,
    replay_revisions,
)
from hashlib import sha256


def _as_options(options: dict | RunOptions | None) -> RunOptions:
    if isinstance(options, RunOptions):
        return options
    return RunOptions.model_validate(options or {})


def _cutoff_datetime(options: RunOptions) -> datetime | None:
    raw, _ = options.receipt_bound()
    bound = parse_bound(raw)
    if bound is None:
        return None
    if isinstance(bound, datetime):
        return bound
    return datetime(bound.year, bound.month, bound.day)


def _create_replay_run_locked(
    db,
    *,
    stream_key: str,
    stream_name: str,
    options: RunOptions,
    actor: str,
    requested_dataset_id: int | None = None,
    source_run_id: int | None = None,
) -> AnalysisRun:
    now = now_utc()
    rows = load_stream_revisions(db, stream_key)
    if not rows:
        raise Problem(404, 'stream_not_found', 'No revision records are visible for this replay')
    records = [revision_to_replay_input(row) for row in rows]
    raw_cutoff, inclusive = options.receipt_bound()
    cutoff = parse_bound(raw_cutoff)
    try:
        outcome = replay_revisions(
            records,
            as_of_date=options.as_of_date,
            received_cutoff=cutoff,
            inclusive=inclusive,
            # The database query/transaction is the visibility fence; do not
            # apply a second wall-clock filter across multiple API processes.
        )
    except DomainConflict as exc:
        raise Problem(409, 'revision_content_conflict', str(exc)) from None
    except RevisionReplayError as exc:
        raise Problem(422, 'invalid_replay', str(exc)) from None

    frozen = freeze_dataset(db, stream_name, outcome['rows'], actor)
    run = AnalysisRun(
        dataset_id=requested_dataset_id or frozen.id,
        options=options.storage_dict(),
        status='queued',
        requested_by=actor,
    )
    db.add(run)
    db.flush()
    db.add(RunReplay(
        run_id=run.id,
        snapshot_dataset_id=frozen.id,
        source_run_id=source_run_id,
        stream_key=stream_key,
        stream_name=stream_name,
        received_cutoff=_cutoff_datetime(options),
        cutoff_inclusive=inclusive,
        frozen_at=now,
        adopted=outcome['adopted_revisions'],
    ))
    audit(db, actor, 'run_queued', f'run/{run.id}')
    return run


def _legacy_run_replay_locked(db, dataset: Dataset, run: AnalysisRun, now) -> RunReplay:
    observations = list(db.scalars(select(Observation).where(
        Observation.dataset_id == dataset.id).order_by(Observation.id)))
    adopted = []
    for observation in observations:
        payload = observation.payload or {}
        missing = all(payload.get(field) is None for field in (
            'post_minutes', 'assigned_manuscripts', 'sla_breaches', 'rework_manuscripts'))
        adopted.append({
            'editor_id': payload.get('editor_id'),
            'revision': 1,
            'revision_no': 1,
            'revision_number': 1,
            'adopted_revision': 1,
            'content_checksum': sha256(canonical_json(payload).encode()).hexdigest(),
            'withdrawn': False,
            'outcome_missing': missing,
            'result_missing': missing,
            'missing_outcome': missing,
            'received_at': None,
            'stored_at': None,
        })
    replay = RunReplay(
        run_id=run.id,
        snapshot_dataset_id=dataset.id,
        source_run_id=None,
        stream_key=stream_key_for_name(dataset.name),
        stream_name=dataset.name,
        received_cutoff=None,
        cutoff_inclusive=True,
        frozen_at=now,
        adopted=adopted,
    )
    db.add(replay)
    return replay


def request_run(dataset_id: int, options: dict | RunOptions, actor: str) -> AnalysisRun:
    run_options = _as_options(options)
    with Session.begin() as db:
        dataset = get_dataset(db, dataset_id)
        stream_key = stream_key_for_name(dataset.name)
        revision_count = db.scalar(select(func.count()).select_from(EditorRevisionRecord).where(
            EditorRevisionRecord.stream_key == stream_key))
        if run_options.has_receipt_cutoff() or revision_count:
            stream_key = ensure_dataset_revision_ledger(db, dataset)
            run = _create_replay_run_locked(
                db,
                stream_key=stream_key,
                stream_name=dataset.name,
                options=run_options,
                actor=actor,
                requested_dataset_id=dataset.id,
            )
        else:
            run = AnalysisRun(dataset_id=dataset_id, options=run_options.storage_dict(),
                              status='queued', requested_by=actor)
            db.add(run)
            db.flush()
            _legacy_run_replay_locked(db, dataset, run, now_utc())
            audit(db, actor, 'run_queued', f'run/{run.id}')
    wake_worker()
    return run


def request_stream_run(stream_name: str, options: dict | RunOptions, actor: str) -> AnalysisRun:
    name = (stream_name or '').strip()
    if not 1 <= len(name) <= 120:
        raise Problem(422, 'invalid_stream_name', 'Stream name must contain 1 to 120 characters')
    run_options = _as_options(options)
    with Session.begin() as db:
        stream_key = stream_key_for_name(name)
        count = db.scalar(select(func.count()).select_from(EditorRevisionRecord).where(
            EditorRevisionRecord.stream_key == stream_key))
        if not count:
            raise Problem(404, 'stream_not_found', f'No revision ledger named {name}')
        run = _create_replay_run_locked(
            db, stream_key=stream_key, stream_name=name,
            options=run_options, actor=actor,
        )
        run_id = run.id
    wake_worker()
    # Return a fresh detached-ish object through the session factory pattern.
    with Session() as db:
        return get_run(db, run_id)


def rerun_run(run_id: int, actor: str, *, in_place: bool = False) -> tuple[AnalysisRun, bool]:
    with Session.begin() as db:
        previous = get_run(db, run_id, lock=True)
        replay = db.get(RunReplay, run_id)
        if in_place:
            previous.status = 'queued'
            previous.error = None
            previous.finished_at = None
            audit(db, actor, 'run_rerun', f'run/{previous.id}')
            run = previous
            created = False
        else:
            run = AnalysisRun(
                dataset_id=previous.dataset_id,
                options=dict(previous.options or {}),
                status='queued',
                requested_by=actor,
            )
            db.add(run)
            db.flush()
            if replay is not None:
                db.add(RunReplay(
                    run_id=run.id,
                    snapshot_dataset_id=replay.snapshot_dataset_id,
                    source_run_id=previous.id,
                    stream_key=replay.stream_key,
                    stream_name=replay.stream_name,
                    received_cutoff=replay.received_cutoff,
                    cutoff_inclusive=replay.cutoff_inclusive,
                    frozen_at=replay.frozen_at,
                    adopted=replay.adopted,
                ))
            audit(db, actor, 'run_queued', f'run/{run.id}')
            created = True
        run_id_to_wake = run.id
    wake_worker()
    with Session() as db:
        return get_run(db, run_id_to_wake), created


def get_run(db, run_id: int, lock: bool = False) -> AnalysisRun:
    query = select(AnalysisRun).where(AnalysisRun.id == run_id)
    run = db.scalar(query.with_for_update() if lock else query)
    if run is None:
        raise Problem(404, 'run_not_found', 'Analysis run does not exist')
    return run
