import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'plan_by_goal.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('plan_by_goal', SCRIPT)
goal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(goal)

MIB = 1 << 20


def row(path, size, age_days=100, now=1_800_000_000):
    mtime_ns = int((now - age_days * 86400) * 1_000_000_000)
    return {'path': path, 'logical_bytes': size,
            'fingerprint': [1, 2, size, mtime_ns, mtime_ns]}


def report(dirs=(), files=(), free=10 * MIB, total=100 * MIB, root='C:\\work'):
    return {'schema_version': 1, 'roots': [root], 'complete': True,
            'volumes': [{'path': root, 'total_bytes': total, 'free_bytes': free}],
            'largest_directories': list(dirs), 'largest_files': list(files)}


CLEAN = {'markers': [], 'complete': True, 'issues': [], 'entries_checked': 1}
MARKED = {'markers': ['C:\\work\\repo\\.git'], 'complete': True, 'issues': [], 'entries_checked': 1}
BROKEN = {'markers': [], 'complete': False, 'issues': ['discovery limit reached'],
          'entries_checked': 5000}


class ThresholdTests(unittest.TestCase):
    def test_deficit_is_the_gap_to_the_floor(self):
        rows = goal.threshold_deficit([{'path': 'C:\\', 'total_bytes': 100 * MIB,
                                        'free_bytes': 10 * MIB}], 20)
        self.assertEqual(rows[0]['deficit_bytes'], 10 * MIB)

    def test_no_deficit_when_already_above_the_floor(self):
        rows = goal.threshold_deficit([{'path': 'C:\\', 'total_bytes': 100 * MIB,
                                        'free_bytes': 40 * MIB}], 20)
        self.assertEqual(rows[0]['deficit_bytes'], 0)

    def test_volumes_without_capacity_are_refused(self):
        with self.assertRaises(ValueError):
            goal.threshold_deficit([{'path': 'C:\\'}], 20)


class CandidateTests(unittest.TestCase):
    def run_candidates(self, data, *, index=(), audit=False, min_age=0, scope=('C:\\work',)):
        with patch.object(goal.git_discovery, 'discover', return_value=CLEAN), \
                patch.object(goal.clean, 'is_denied', return_value=None):
            return goal.candidates(data, index=list(index), scope=list(scope),
                                   run_audit=audit, min_age_days=min_age, now=1_800_000_000)

    def test_the_scan_root_is_never_a_candidate(self):
        pool = self.run_candidates(report(dirs=[row('C:\\work', 90 * MIB)]))
        self.assertEqual(pool[0]['fatal'], 'this is the scan root; choose a subdirectory instead')

    def test_old_enough_directories_survive(self):
        pool = self.run_candidates(report(dirs=[row('C:\\work\\cache', 5 * MIB)]), min_age=30)
        self.assertIsNone(pool[0]['fatal'])

    def test_recent_entries_are_dropped_by_age(self):
        pool = self.run_candidates(report(dirs=[row('C:\\work\\fresh', 5 * MIB, age_days=2)]),
                                   min_age=30)
        self.assertEqual(pool, [])

    def test_gate_refusal_is_carried_as_fatal(self):
        with patch.object(goal.git_discovery, 'discover', return_value=CLEAN), \
                patch.object(goal.clean, 'is_denied', return_value='protected system tree'):
            pool = goal.candidates(report(dirs=[row('C:\\work\\x', 5 * MIB)]),
                                   index=[], scope=['C:\\work'], now=1_800_000_000)
        self.assertIn('protected system tree', pool[0]['fatal'])

    def test_incomplete_git_discovery_is_fatal(self):
        with patch.object(goal.git_discovery, 'discover', return_value=BROKEN), \
                patch.object(goal.clean, 'is_denied', return_value=None):
            pool = goal.candidates(report(dirs=[row('C:\\work\\big', 5 * MIB)]),
                                   index=[], scope=['C:\\work'], now=1_800_000_000)
        self.assertIn('Git discovery incomplete', pool[0]['fatal'])

    def test_missing_git_audit_blocks_a_marked_directory(self):
        with patch.object(goal.git_discovery, 'discover', return_value=MARKED), \
                patch.object(goal.clean, 'is_denied', return_value=None):
            pool = goal.candidates(report(dirs=[row('C:\\work\\repo', 5 * MIB)]),
                                   index=[], scope=['C:\\work'], now=1_800_000_000)
        self.assertIn('rerun with --git-audit', pool[0]['fatal'])

    def test_unsafe_git_verdict_blocks_the_draft(self):
        with patch.object(goal.git_discovery, 'discover', return_value=MARKED), \
                patch.object(goal.clean, 'is_denied', return_value=None), \
                patch.object(goal, 'worktree_root', return_value='C:\\work\\repo'), \
                patch.object(goal, '_audit',
                             return_value=({'verdict': 'LOCAL_ONLY_DATA_PRESENT'}, None)):
            pool = goal.candidates(report(dirs=[row('C:\\work\\repo', 5 * MIB)]),
                                   index=[], scope=['C:\\work'], run_audit=True,
                                   now=1_800_000_000)
        self.assertIn('LOCAL_ONLY_DATA_PRESENT', pool[0]['fatal'])

    def test_safe_git_verdict_is_attached_as_evidence(self):
        audit = {'verdict': 'SAFE_TO_CONSIDER', 'schema': 'x'}
        with patch.object(goal.git_discovery, 'discover', return_value=MARKED), \
                patch.object(goal.clean, 'is_denied', return_value=None), \
                patch.object(goal, 'worktree_root', return_value='C:\\work\\repo'), \
                patch.object(goal, '_audit', return_value=(audit, None)):
            pool = goal.candidates(report(dirs=[row('C:\\work\\repo', 5 * MIB)]),
                                   index=[], scope=['C:\\work'], run_audit=True,
                                   now=1_800_000_000)
        self.assertIsNone(pool[0]['fatal'])
        self.assertEqual(pool[0]['git'], audit)

    def test_safe_caches_outrank_larger_unclassified_directories(self):
        pool = self.run_candidates(
            report(dirs=[row('C:\\work\\unknown-big', 50 * MIB),
                         row('C:\\work\\npm', 1 * MIB)]),
            index=[{'root': 'C:\\work\\npm', 'risk': 'safe', 'reason': 'npm 缓存'}])
        self.assertEqual(pool[0]['path'], 'C:\\work\\npm')
        self.assertEqual(pool[0]['risk'], 'safe')


class PickTests(unittest.TestCase):
    def item(self, path, size, fatal=None, risk='caution'):
        return {'path': path, 'bytes': size, 'kind': 'directory', 'fingerprint': [1, 2, 3, 4, 5],
                'risk': risk, 'reason': '', 'fatal': fatal, 'git': None}

    def test_stops_as_soon_as_the_goal_is_met(self):
        pool = [self.item('C:\\a', 3 * MIB), self.item('C:\\b', 3 * MIB)]
        chosen, _ = goal.pick(pool, 4 * MIB, 10)
        self.assertEqual([c['path'] for c in chosen], ['C:\\a', 'C:\\b'])

    def test_nested_targets_are_not_both_taken(self):
        pool = [self.item('C:\\a', 9 * MIB), self.item('C:\\a\\b', 8 * MIB)]
        chosen, skipped = goal.pick(pool, 20 * MIB, 10)
        self.assertEqual([c['path'] for c in chosen], ['C:\\a'])
        self.assertIn('nests', skipped[0]['why'])

    def test_batch_limit_is_respected(self):
        pool = [self.item(f'C:\\d{i}', 1 * MIB) for i in range(5)]
        chosen, _ = goal.pick(pool, 99 * MIB, 2)
        self.assertEqual(len(chosen), 2)

    def test_fatal_entries_are_skipped_with_their_reason(self):
        pool = [self.item('C:\\a', 1 * MIB, fatal='refused by clean.py: volume root')]
        chosen, skipped = goal.pick(pool, 1 * MIB, 10)
        self.assertEqual(chosen, [])
        self.assertIn('volume root', skipped[0]['why'])


class PlanTests(unittest.TestCase):
    def test_directories_omit_size_bytes_but_files_keep_it(self):
        chosen = [
            {'path': 'C:\\a', 'bytes': 5, 'kind': 'directory', 'fingerprint': [1, 2, 3, 4, 5],
             'risk': 'safe', 'reason': 'r', 'git': None},
            {'path': 'C:\\b.bin', 'bytes': 7, 'kind': 'file', 'fingerprint': [1, 2, 3, 4, 5],
             'risk': 'caution', 'reason': 'r', 'git': {'verdict': 'SAFE_TO_CONSIDER'}},
        ]
        plan = goal.build_plan({}, chosen, [], goal={}, scope=['C:\\'], excluded=[], note='n')
        self.assertNotIn('size_bytes', plan['targets'][0]['evidence'])
        self.assertEqual(plan['targets'][1]['evidence']['size_bytes'], 7)
        self.assertEqual(plan['targets'][1]['evidence']['git']['verdict'], 'SAFE_TO_CONSIDER')

    def test_plan_shape_satisfies_the_executor_contract(self):
        chosen = [{'path': 'C:\\a', 'bytes': 5, 'kind': 'directory',
                   'fingerprint': [1, 2, 3, 4, 5], 'risk': 'caution', 'reason': 'r', 'git': None}]
        plan = goal.build_plan({}, chosen, [], goal={}, scope=['C:\\'], excluded=[], note='n')
        self.assertEqual(plan['schema'], 'disk-cleanup-plan/v1')
        self.assertEqual(plan['targets'][0]['action'], 'trash')
        self.assertEqual(len(plan['targets'][0]['evidence']['fingerprint']), 5)


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        with tempfile.TemporaryDirectory() as tmp:
            scan_path = os.path.join(tmp, 'scan.json')
            with open(scan_path, 'w', encoding='utf-8') as fh:
                json.dump(report(dirs=[row('C:\\work\\cache', 30 * MIB)]), fh)
            argv = [arg.replace('SCAN', scan_path).replace('OUT', os.path.join(tmp, 'plan.json'))
                    for arg in argv]
            code = goal.main(argv)
            plan = None
            out = os.path.join(tmp, 'plan.json')
            if os.path.exists(out):
                plan = json.load(open(out, encoding='utf-8'))
            return code, plan

    def test_no_goal_is_refused(self):
        code, plan = self.run_main(['--scan', 'SCAN', '--out', 'OUT'])
        self.assertEqual(code, 2)
        self.assertIsNone(plan)

    def test_target_produces_a_plan(self):
        with patch.object(goal.git_discovery, 'discover', return_value=CLEAN), \
                patch.object(goal.clean, 'is_denied', return_value=None):
            code, plan = self.run_main(['--scan', 'SCAN', '--target', '5MiB',
                                        '--min-age-days', '0', '--out', 'OUT'])
        self.assertEqual(code, 0)
        self.assertEqual(plan['goal']['requested_bytes'], 5 * MIB)
        self.assertTrue(plan['goal']['met'])

    def test_shortfall_is_reported_not_padded(self):
        with patch.object(goal.git_discovery, 'discover', return_value=CLEAN), \
                patch.object(goal.clean, 'is_denied', return_value=None):
            code, plan = self.run_main(['--scan', 'SCAN', '--target', '99MiB',
                                        '--min-age-days', '0', '--out', 'OUT'])
        self.assertEqual(code, 0)
        self.assertFalse(plan['goal']['met'])
        self.assertGreater(plan['goal']['shortfall_bytes'], 0)

    def test_bad_size_is_refused_with_exit_2(self):
        code, _ = self.run_main(['--scan', 'SCAN', '--target', 'lots', '--out', 'OUT'])
        self.assertEqual(code, 2)


class HumanTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(goal.human(1 << 30), '1.00 GiB')
        self.assertEqual(goal.human(0), '0 B')
        self.assertEqual(goal.human(None), 'unknown')


class GoalGateTests(unittest.TestCase):
    """clean.py must refuse a goal whose own numbers disagree."""

    def valid(self, **overrides):
        goal_obj = {'schema': 'disk-cleanup-goal/v1', 'kind': 'move_bytes',
                    'requested_bytes': 100, 'estimated_bytes': 80,
                    'shortfall_bytes': 20, 'met': False}
        goal_obj.update(overrides)
        return goal_obj

    def test_valid_goal_passes(self):
        goal.clean.validate_goal(self.valid())
        goal.clean.validate_goal(self.valid(estimated_bytes=120, shortfall_bytes=0, met=True))

    def test_wrong_schema_is_refused(self):
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(schema='nope'))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(kind='free_bytes'))

    def test_negative_or_float_is_refused(self):
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(requested_bytes=-1))
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(requested_bytes=1.5))

    def test_met_cannot_contradict_the_numbers(self):
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(met=True))
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(estimated_bytes=120, shortfall_bytes=0, met=False))

    def test_shortfall_must_match(self):
        with self.assertRaises(goal.clean.GateError):
            goal.clean.validate_goal(self.valid(shortfall_bytes=0))


if __name__ == '__main__':
    unittest.main()
