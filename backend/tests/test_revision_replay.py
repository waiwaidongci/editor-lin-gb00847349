import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.revisions import (  # noqa: E402
    RevisionConflict,
    replay_revisions,
    select_revisions,
)


BASE = {
    'arm': 'control',
    'assigned_date': '2026-11-01',
    'pre_minutes': 100.0,
}
OUTCOMES = {
    'post_minutes': 110.0,
    'assigned_manuscripts': 10,
    'sla_breaches': 0,
    'rework_manuscripts': 0,
}


def revision(editor, number, received, **changes):
    row = {'editor_id': editor, 'revision': number, 'received_at': received}
    if number == 1:
        row.update(BASE)
        row.update(OUTCOMES)
    row.update(changes)
    return row


class RevisionReplayTests(unittest.TestCase):
    def test_highest_revision_before_cutoff(self):
        records = [
            revision('E1', 1, '2026-11-18', post_minutes=11),
            revision('E1', 3, '2026-11-22', post_minutes=33,
                     assigned_manuscripts=2, sla_breaches=0, rework_manuscripts=0),
            revision('E1', 2, '2026-11-21', post_minutes=22,
                     assigned_manuscripts=1, sla_breaches=0, rework_manuscripts=0),
        ]
        selected = select_revisions(records, received_cutoff='2026-11-21')
        self.assertEqual(selected['E1']['revision'], 2)
        result = replay_revisions(records, received_cutoff='2026-11-20')
        self.assertEqual(result['rows'][0]['post_minutes'], 11)

    def test_late_lower_revision_does_not_override_newer(self):
        records = [
            revision('E1', 3, '2026-11-22', **BASE, post_minutes=33,
                     assigned_manuscripts=2, sla_breaches=0, rework_manuscripts=0),
            revision('E1', 2, '2026-11-19', post_minutes=22),
        ]
        result = replay_revisions(records)
        self.assertEqual(result['adopted_revisions'][0]['revision'], 3)
        self.assertEqual(result['rows'][0]['post_minutes'], 33)

    def test_withdrawal_keeps_assignment_but_marks_outcomes_missing(self):
        records = [
            revision('E2', 1, '2026-11-18', arm='treatment', pre_minutes=120),
            {'editor_id': 'E2', 'revision': 2, 'withdrawn': True,
             'received_at': '2026-11-20'},
        ]
        result = replay_revisions(records, received_cutoff='2026-11-25')
        row = result['rows'][0]
        self.assertEqual(row['arm'], 'treatment')
        self.assertEqual(row['assigned_date'], '2026-11-01')
        self.assertIsNone(row['post_minutes'])
        self.assertIsNone(row['assigned_manuscripts'])
        self.assertTrue(result['adopted_revisions'][0]['withdrawn'])

    def test_same_revision_different_content_conflicts(self):
        records = [
            revision('E3', 1, '2026-11-18', post_minutes=10),
            revision('E3', 1, '2026-11-18', post_minutes=11),
        ]
        with self.assertRaises(RevisionConflict):
            replay_revisions(records)

    def test_exact_duplicate_is_not_a_conflict(self):
        record = revision('E4', 1, '2026-11-18')
        result = replay_revisions([record, dict(record)])
        self.assertEqual(len(result['rows']), 1)


if __name__ == '__main__':
    unittest.main()
