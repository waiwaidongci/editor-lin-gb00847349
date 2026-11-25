from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.clock import now_utc, utc_iso
from app.data.models import Dataset, Observation
from app.data.repository import freeze_dataset
from app.database import Session
from app.errors import Problem
from app.intake.models import EditorRevisionRecord, IntakeBatch
from app.ops.audit import audit
from domain.revisions import (
    RevisionConflict as DomainConflict,
    canonical_json,
    content_fingerprint,
    ensure_no_conflicts,
    normalize_revision,
    replay_revisions,
)


def stream_key_for_name(name: str) -> str:
    return 'name:' + name.strip()


def hash_long_key(key: str | None) -> str | None:
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) <= 120:
        return key
    return 'sha256:' + sha256(key.encode('utf-8')).hexdigest()


def describe_batch(batch: IntakeBatch, *, created: bool, statuses: list[dict] | None = None,
                   dataset: Dataset | None = None, materialization_error: str | None = None):
    return {
        'id': batch.id,
        'batch_id': batch.id,
        'stream': batch.stream_name,
        'stream_name': batch.stream_name,
        'stream_key': batch.stream_key,
        'batch_key': batch.batch_key,
        'idempotency_key': batch.batch_key,
        'checksum': batch.request_checksum,
        'request_checksum': batch.request_checksum,
        'record_count': batch.record_count,
        'inserted_count': batch.inserted_count,
        'duplicate_count': batch.duplicate_count,
        'created': created,
        'idempotent_replay': not created,
        'replayed': not created,
        'duplicate': not created,
        'changed': batch.inserted_count > 0,
        'accepted': True,
        'status': 'duplicate' if not created else 'imported',
        'created_by': batch.actor,
        'actor': batch.actor,
        'created_at': utc_iso(batch.created_at),
        'statuses': statuses or [],
        # Compatibility with the original /datasets import response.
        'dataset_id': dataset.id if dataset else None,
        'dataset': {
            'id': dataset.id, 'name': dataset.name, 'checksum': dataset.checksum,
            'row_count': dataset.row_count,
            'created_by': dataset.created_by,
            'created_at': utc_iso(dataset.created_at),
        } if dataset else None,
        'materialization_error': materialization_error,
    }


def _request_checksum(items: list[dict]) -> str:
    fingerprints = sorted(item['content_checksum'] for item in items)
    return sha256(canonical_json(fingerprints).encode('utf-8')).hexdigest()


def import_revision_batch(
    stream_name: str,
    raw_records: list[dict],
    actor: str,
    *,
    batch_key: str | None = None,
    default_received_at: Any = None,
) -> dict:
    stream_name = (stream_name or '').strip()
    if not 1 <= len(stream_name) <= 120:
        raise Problem(422, 'invalid_stream_name', 'Stream name must contain 1 to 120 characters')
    if not isinstance(raw_records, list) or not raw_records:
        raise Problem(422, 'invalid_row_count', 'Provide at least one revision record')

    from app.settings import settings
    if len(raw_records) > settings.max_rows:
        raise Problem(422, 'invalid_row_count', f'At most {settings.max_rows} records are accepted')

    now = now_utc()
    normalized: list[dict] = []
    for position, raw in enumerate(raw_records, 1):
        if not isinstance(raw, dict):
            raise Problem(422, 'invalid_revision', f'Record {position} must be an object')
        try:
            normalized.append(normalize_revision(raw, position, default_received_at or now))
        except Exception as exc:
            code = 'revision_conflict' if exc.__class__.__name__ == 'RevisionConflict' else 'invalid_revision'
            raise Problem(409 if code == 'revision_conflict' else 422, code, str(exc)) from None

    # Conflict detection runs after implicit revision numbers are assigned so
    # multiple revisionless supplements do not all collide at r1.
    request_checksum = _request_checksum(normalized)
    effective_key = hash_long_key(batch_key) or request_checksum
    skey = stream_key_for_name(stream_name)

    try:
        with Session.begin() as db:
            existing = db.scalar(select(IntakeBatch).where(
                IntakeBatch.stream_key == skey, IntakeBatch.batch_key == effective_key))
            if existing is not None:
                if existing.request_checksum != request_checksum:
                    raise Problem(
                        409, 'idempotency_key_conflict',
                        'This batch key was already used with different content',
                    )
                return describe_batch(existing, created=False)

            editor_ids = sorted({item['editor_id'] for item in normalized})
            old_rows = list(db.scalars(select(EditorRevisionRecord).where(
                EditorRevisionRecord.stream_key == skey,
                EditorRevisionRecord.editor_id.in_(editor_ids),
            )))
            old_by_key = {(row.editor_id, row.revision_no): row for row in old_rows}
            max_revision: dict[str, int] = {}
            for row in old_rows:
                max_revision[row.editor_id] = max(max_revision.get(row.editor_id, 0), row.revision_no)
            for item in normalized:
                editor_id = item['editor_id']
                if item.get('revision_explicit'):
                    max_revision[editor_id] = max(
                        max_revision.get(editor_id, 0), item['revision'])
                    continue
                next_revision = max_revision.get(editor_id, 0) + 1
                max_revision[editor_id] = next_revision
                item['revision'] = next_revision
                received_iso = None if item.get('received_implicit') else (
                    item['received_at'].isoformat() if item.get('received_at') else None)
                item['content_checksum'] = content_fingerprint(
                    editor_id, next_revision, item['data'], received_iso, item['withdrawn'])

            try:
                ensure_no_conflicts(normalized)
            except DomainConflict as exc:
                raise Problem(409, 'revision_content_conflict', str(exc)) from None

            conflicts = []
            for item in normalized:
                old = old_by_key.get((item['editor_id'], item['revision']))
                if old is not None and old.content_checksum != item['content_checksum']:
                    conflicts.append({
                        'editor_id': item['editor_id'],
                        'revision': item['revision'],
                        'existing_checksum': old.content_checksum,
                        'incoming_checksum': item['content_checksum'],
                    })
            if conflicts:
                first = conflicts[0]
                raise Problem(
                    409, 'revision_content_conflict',
                    f"编辑 {first['editor_id']} 的修订号 r{first['revision']} 已存在但内容不同；整批未写入",
                )

            batch = IntakeBatch(
                stream_key=skey, stream_name=stream_name, batch_key=effective_key,
                request_checksum=request_checksum, record_count=len(normalized),
                inserted_count=0, duplicate_count=0, actor=actor, created_at=now,
            )
            db.add(batch)
            db.flush()

            statuses: list[dict] = []
            inserted = duplicate = 0
            inserted_by_key: dict[tuple[str, int], EditorRevisionRecord] = {}
            for item in normalized:
                natural = (item['editor_id'], item['revision'])
                old = old_by_key.get(natural) or inserted_by_key.get(natural)
                if old is not None:
                    duplicate += 1
                    statuses.append({
                        'editor_id': item['editor_id'], 'revision': item['revision'],
                        'status': 'duplicate', 'record_id': old.id,
                    })
                    continue
                record = EditorRevisionRecord(
                    stream_key=skey, editor_id=item['editor_id'], revision_no=item['revision'],
                    batch_id=batch.id, content_checksum=item['content_checksum'],
                    received_at=item['received_at'] or now, stored_at=now,
                    withdrawn=item['withdrawn'], payload=item['data'],
                )
                db.add(record)
                db.flush()
                inserted_by_key[natural] = record
                inserted += 1
                statuses.append({
                    'editor_id': item['editor_id'], 'revision': item['revision'],
                    'status': 'inserted', 'record_id': record.id,
                })
            batch.inserted_count = inserted
            batch.duplicate_count = duplicate
            audit(db, actor, 'intake_batch_imported', f'intake_batch/{batch.id}')
            db.flush()
            result = describe_batch(batch, created=True, statuses=statuses)
            return result
    except IntegrityError:
        with Session() as db:
            winner = db.scalar(select(IntakeBatch).where(
                IntakeBatch.stream_key == skey, IntakeBatch.batch_key == effective_key))
            if winner is not None and winner.request_checksum == request_checksum:
                return describe_batch(winner, created=False)
        raise Problem(409, 'revision_conflict', 'A concurrent revision import conflicted; retry the request')


def revision_to_replay_input(row: EditorRevisionRecord) -> dict:
    return {
        'editor_id': row.editor_id,
        'revision': row.revision_no,
        'data': row.payload or {},
        'content_checksum': row.content_checksum,
        'received_at': row.received_at,
        'stored_at': row.stored_at,
        'withdrawn': row.withdrawn,
        'batch_key': row.batch_id,
    }


def load_stream_revisions(db, stream_key: str) -> list[EditorRevisionRecord]:
    return list(db.scalars(select(EditorRevisionRecord).where(
        EditorRevisionRecord.stream_key == stream_key
    ).order_by(EditorRevisionRecord.editor_id, EditorRevisionRecord.revision_no,
               EditorRevisionRecord.id)))


def replay_stream(
    stream_name: str,
    *,
    received_cutoff: Any = None,
    inclusive: bool = True,
    visible_at: datetime | None = None,
    as_of_date: Any = None,
):
    skey = stream_key_for_name(stream_name)
    with Session() as db:
        rows = load_stream_revisions(db, skey)
        if not rows:
            raise Problem(404, 'stream_not_found', f'No revision ledger named {stream_name}')
        records = [revision_to_replay_input(row) for row in rows]
    try:
        return replay_revisions(
            records, as_of_date=as_of_date, received_cutoff=received_cutoff,
            inclusive=inclusive, visible_at=visible_at,
        )
    except DomainConflict as exc:
        raise Problem(409, 'revision_content_conflict', str(exc)) from None
    except Exception as exc:
        if exc.__class__.__name__ == 'RevisionReplayError':
            raise Problem(422, 'invalid_replay', str(exc)) from None
        raise


def materialize_stream_dataset(stream_name: str, actor: str = 'replay-freeze'):
    """Best-effort current snapshot used to make POST /datasets responses familiar."""
    skey = stream_key_for_name(stream_name)
    with Session.begin() as db:
        rows = load_stream_revisions(db, skey)
        if not rows:
            return None, None
        records = [revision_to_replay_input(row) for row in rows]
        try:
            outcome = replay_revisions(records)
        except Exception as exc:
            return None, str(exc)
        dataset = freeze_dataset(db, stream_name, outcome['rows'], actor)
        db.flush()
        return dataset, None


def ensure_dataset_revision_ledger(db, dataset: Dataset) -> str:
    """Represent an original immutable dataset as revision r1 of its name stream."""
    skey = stream_key_for_name(dataset.name)
    existing_count = db.scalar(select(func.count()).select_from(EditorRevisionRecord).where(
        EditorRevisionRecord.stream_key == skey))
    if existing_count:
        return skey

    batch_key = f'baseline-dataset-{dataset.id}'
    batch = db.scalar(select(IntakeBatch).where(
        IntakeBatch.stream_key == skey, IntakeBatch.batch_key == batch_key))
    if batch is None:
        batch = IntakeBatch(
            stream_key=skey, stream_name=dataset.name, batch_key=batch_key,
            request_checksum=dataset.checksum, record_count=dataset.row_count,
            inserted_count=dataset.row_count, duplicate_count=0,
            actor=dataset.created_by, created_at=dataset.created_at or now_utc(),
        )
        db.add(batch)
        db.flush()

    observations = list(db.scalars(select(Observation).where(
        Observation.dataset_id == dataset.id).order_by(Observation.id)))
    when = dataset.created_at or now_utc()
    for obs in observations:
        payload = dict(obs.payload)
        # A blank quartet means "outcome not returned yet"; it is not necessarily
        # an explicit withdrawal, so do not set the withdrawal tombstone here.
        checksum = sha256(canonical_json(payload).encode()).hexdigest()
        exists = db.scalar(select(EditorRevisionRecord).where(
            EditorRevisionRecord.stream_key == skey,
            EditorRevisionRecord.editor_id == payload['editor_id'],
            EditorRevisionRecord.revision_no == 1,
        ))
        if exists is None:
            db.add(EditorRevisionRecord(
                stream_key=skey, editor_id=payload['editor_id'], revision_no=1,
                batch_id=batch.id, content_checksum=checksum,
                received_at=when, stored_at=when, withdrawn=False, payload=payload,
            ))
    db.flush()
    return skey
