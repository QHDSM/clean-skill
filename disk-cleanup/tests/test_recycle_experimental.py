"""Simulated behavior plus Windows COM preparation; never perform deletion."""
from pathlib import Path
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recycle_experimental as recycle


class FakeAdapter:
    def __init__(self, fail=None, perform_hr=0, abort_hr=0, aborted=False):
        self.calls = []
        self.fail = fail
        self.perform_hr = perform_hr
        self.abort_hr = abort_hr
        self.was_aborted = aborted

    def call(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise recycle.BackendError('simulated ' + name)

    def initialize(self): self.call('initialize')
    def create_operation(self): self.call('create_operation')
    def set_flags(self, flags): self.call('set_flags'); self.flags = flags
    def create_item(self, path): self.call('create_item')
    def queue_delete(self): self.call('queue_delete')
    def perform(self): self.call('perform'); return self.perform_hr
    def aborted(self): self.call('aborted'); return self.abort_hr, self.was_aborted
    def close(self): self.calls.append('close')


class ExperimentalRecycleTests(unittest.TestCase):
    def test_success_is_unknown_and_does_not_auto_confirm(self):
        fake = FakeAdapter()
        result = recycle.run_experiment('fixture', fake)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(fake.calls, ['initialize', 'create_operation', 'set_flags',
                                      'create_item', 'queue_delete', 'perform', 'aborted', 'close'])
        self.assertTrue(fake.flags & recycle.FOFX_RECYCLEONDELETE)
        self.assertTrue(fake.flags & recycle.FOFX_EARLYFAILURE)
        self.assertFalse(fake.flags & 0x10)  # FOF_NOCONFIRMATION

    def test_abort_is_queried_even_if_perform_fails(self):
        fake = FakeAdapter(perform_hr=-1)
        self.assertEqual(recycle.run_experiment('fixture', fake)['status'], 'UNKNOWN')
        self.assertIn('aborted', fake.calls)
        self.assertEqual(fake.calls[-1], 'close')

    def test_failed_preparation_never_performs(self):
        for stage in ('initialize', 'create_operation', 'set_flags', 'create_item', 'queue_delete'):
            with self.subTest(stage=stage):
                fake = FakeAdapter(fail=stage)
                self.assertEqual(recycle.run_experiment('fixture', fake)['status'], 'FAILED_BEFORE_PERFORM')
                self.assertNotIn('perform', fake.calls)
                self.assertEqual(fake.calls[-1], 'close')

    def test_failure_after_perform_is_unknown(self):
        for stage in ('perform', 'aborted'):
            with self.subTest(stage=stage):
                fake = FakeAdapter(fail=stage)
                self.assertEqual(recycle.run_experiment('fixture', fake)['status'], 'UNKNOWN')
                self.assertEqual(fake.calls[-1], 'close')


@unittest.skipUnless(os.name == 'nt', 'Windows Shell COM test')
class NativePreparationTests(unittest.TestCase):
    def test_queue_without_perform_preserves_fixture(self):
        with tempfile.TemporaryDirectory(prefix='disk-cleanup-com-') as temp:
            fixture = Path(temp) / 'fixture.txt'
            fixture.write_bytes(b'owned fixture')
            adapter = recycle.NativeAdapter()
            try:
                adapter.initialize()
                adapter.create_operation()
                adapter.set_flags(recycle.FLAGS)
                adapter.create_item(fixture)
                adapter.queue_delete()
            finally:
                adapter.close()
                adapter.close()  # Releasing an already closed adapter must be safe.
            self.assertEqual(fixture.read_bytes(), b'owned fixture')

    def test_missing_fixture_fails_before_perform(self):
        with tempfile.TemporaryDirectory(prefix='disk-cleanup-com-') as temp:
            adapter = recycle.NativeAdapter()
            result = recycle.run_experiment(Path(temp) / 'missing.txt', adapter)
            self.assertEqual(result['status'], 'FAILED_BEFORE_PERFORM')


if __name__ == '__main__':
    unittest.main()
