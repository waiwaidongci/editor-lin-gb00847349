import json
import logging

from sqlalchemy import select

from app.analysis.contracts import RunOptions, RunStatus, replay_view
from app.analysis.models import AnalysisRun, RunReplay
from app.clock import now_utc
from app.data.repository import observation_query
from app.database import Session
from app.ops.audit import audit
from domain.spec import analyze, validated_editors


def validate_result(result):
    if (not isinstance(result, dict) or not isinstance(result.get('summary'), dict)
            or not isinstance(result.get('tables'), dict) or not isinstance(result.get('warnings'), list)
            or not isinstance(result.get('publishable'), bool)):
        raise TypeError('Unexpected domain result structure')
    for table in result['tables'].values():
        if not isinstance(table, list) or any(not isinstance(row, dict) for row in table):
            raise TypeError('A result table must be a list of row objects')
    json.dumps(result, allow_nan=False)
    return result


def run_next() -> bool:
    with Session.begin() as db:
        query = (select(AnalysisRun).where(AnalysisRun.status == RunStatus.queued)
                 .order_by(AnalysisRun.id).limit(1).with_for_update(skip_locked=True))
        run = db.scalar(query)
        if run is None:
            return False
        try:
            replay = db.get(RunReplay, run.id)
            dataset_id = replay.snapshot_dataset_id if replay is not None else run.dataset_id
            rows = validated_editors(list(db.scalars(observation_query(dataset_id))))
            options = RunOptions.model_validate(run.options or {})
            result = analyze(rows, options.domain_options())
            if replay is not None:
                manifest = replay_view(run, replay)
                result['replay'] = manifest
                result['adopted_revisions'] = manifest['adopted_revisions']
                result['selected_revisions'] = manifest['adopted_revisions']
                result['revisions'] = manifest['adopted_revisions']
            run.result = validate_result(result)
            run.status = RunStatus.completed
        except ValueError as exc:
            run.status = RunStatus.failed
            run.error = str(exc)[:2000]
        except Exception:
            logging.getLogger(__name__).exception('Analysis %s failed unexpectedly', run.id)
            run.status = RunStatus.failed
            run.error = 'Unexpected analysis error. See worker logs.'
        run.finished_at = now_utc()
        audit(db, 'worker', f'run_{run.status}', f'run/{run.id}')
        return True
