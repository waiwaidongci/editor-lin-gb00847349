from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.analysis.contracts import RunOptions
from app.auth.dependencies import account, require
from app.auth.roles import Role
from app.clock import utc_iso
from app.database import Session
from app.ops.audit import AuditEvent
from domain.spec import METHOD, OPTIONS_MODEL, ROW_MODEL, SEED_NAME, TITLE


router = APIRouter(tags=['Metadata and audit'])
reviewer = require(Role.reviewer)


@router.get('/method')
def method(user=Depends(account)):
    return {'title': TITLE, 'method': METHOD, 'seed_name': SEED_NAME,
            'row_schema': ROW_MODEL.model_json_schema(),
            'options_schema': RunOptions.model_json_schema(),
            'analysis_options_schema': OPTIONS_MODEL.model_json_schema(),
            'replay_options': ['as_of_date', 'received_cutoff', 'received_before',
                               'intake_cutoff', 'cutoff_inclusive']}


@router.get('/audit')
def history(user=Depends(reviewer), limit: Annotated[int, Query(ge=1, le=100)] = 50):
    with Session() as db:
        events = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))
        return [{'actor': event.actor, 'action': event.action, 'subject': event.subject,
                 'created_at': utc_iso(event.created_at)} for event in events]
