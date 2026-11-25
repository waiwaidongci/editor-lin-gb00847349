import json
import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from app.analysis.contracts import (
    ReviewRequest,
    RerunRequest,
    RunRequest,
    describe_review,
    replay_view,
)
from app.analysis.models import AnalysisRun, Review, RunReplay
from app.analysis.repository import get_run, request_run, rerun_run
from app.analysis.review import record_review
from app.auth.dependencies import account, require
from app.auth.roles import Role
from app.clock import utc_iso
from app.database import Session


router = APIRouter(tags=['Analysis and review'])
analyst = require(Role.analyst)
reviewer = require(Role.reviewer)


def describe(run, review=None, replay=None):
    manifest = replay_view(run, replay)
    adopted = manifest['adopted_revisions'] if manifest else None
    return {'id': run.id, 'run_id': run.id, 'dataset_id': run.dataset_id, 'status': run.status,
            'state': run.status,
            'options': run.options, 'result': run.result, 'error': run.error,
            'requested_by': run.requested_by, 'created_at': utc_iso(run.created_at),
            'finished_at': utc_iso(run.finished_at) if run.finished_at else None,
            'review': describe_review(review),
            'replay': manifest,
            'manifest': manifest,
            'snapshot': manifest,
            'adopted_revisions': adopted,
            'selected_revisions': adopted,
            'revisions': adopted,
            'adopted': adopted,
            'snapshot_dataset_id': manifest['snapshot_dataset_id'] if manifest else None,
            'rerun_of': manifest['source_run_id'] if manifest else None}


@router.post('/datasets/{dataset_id}/runs', status_code=202)
def start(dataset_id: int, payload: RunRequest, user=Depends(analyst)):
    options = payload.effective_options()
    run = request_run(dataset_id, options, user.email)
    with Session() as db:
        full_run = get_run(db, run.id)
        return describe(full_run, db.get(Review, run.id), db.get(RunReplay, run.id))


@router.get('/runs/{run_id}')
def status(run_id: int, user=Depends(account)):
    with Session() as db:
        run = get_run(db, run_id)
        return describe(run, db.get(Review, run_id), db.get(RunReplay, run_id))


@router.post('/runs/{run_id}/rerun')
@router.post('/runs/{run_id}/replay')
def rerun(run_id: int, response: Response, payload: RerunRequest | None = None,
          user=Depends(analyst)):
    payload = payload or RerunRequest()
    run, created = rerun_run(run_id, user.email, in_place=payload.effective_in_place())
    if created:
        response.status_code = 201
    with Session() as db:
        return describe(get_run(db, run.id), db.get(Review, run.id), db.get(RunReplay, run.id))


@router.post('/runs/{run_id}/retry')
def retry(run_id: int, payload: RerunRequest | None = None, user=Depends(analyst)):
    payload = payload or RerunRequest(in_place=True)
    if payload is None:
        payload = RerunRequest(in_place=True)
    # Retry has conventional same-task semantics: keep the same run id and
    # reuse its frozen snapshot.  /rerun defaults to a comparable cloned run.
    payload.in_place = True
    run, _ = rerun_run(run_id, user.email, in_place=True)
    with Session() as db:
        return describe(get_run(db, run.id), db.get(Review, run.id), db.get(RunReplay, run.id))


@router.post('/runs/{run_id}/review', status_code=201)
def review(run_id: int, payload: ReviewRequest, user=Depends(reviewer)):
    return describe_review(record_review(run_id, payload.decision, payload.note, user.email))
