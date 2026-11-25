"""Compatibility module for code expecting ``domain.replay``."""

from domain.revisions import *  # noqa: F401,F403
from domain.revisions import (
    replay_revisions,
    select_revisions,
)


def replay_at(records, *, as_of_date=None, received_cutoff=None, intake_cutoff=None,
              received_before=None, **kwargs):
    cutoff = received_cutoff or intake_cutoff or received_before
    inclusive = kwargs.pop('inclusive', received_before is None)
    return replay_revisions(
        records, as_of_date=as_of_date, received_cutoff=cutoff,
        inclusive=inclusive, **kwargs,
    )


def replay_as_of(records, analysis_date=None, cutoff=None, **kwargs):
    return replay_at(records, as_of_date=analysis_date, received_cutoff=cutoff, **kwargs)


def build_replay(records, as_of_date=None, received_cutoff=None, **kwargs):
    return replay_revisions(records, as_of_date=as_of_date,
                            received_cutoff=received_cutoff, **kwargs)


def select_revision_rows(records, **kwargs):
    return replay_revisions(records, validate=False, **kwargs)['rows']
