"""Fixture-only staging/recovery experiment; not connected to clean.py.

No authorization, parent-handle binding or automatic crash recovery is
implemented. Never call this on user data or treat results as a cleanup success.
"""
import os
from pathlib import Path
import uuid

import stage_experimental
import windows_identity
import journal_experimental


def run_fixture(source, expected, backend, *, journal_path=None):
    source = Path(os.path.abspath(source))
    token = uuid.uuid4().hex
    journal_path = Path(journal_path) if journal_path else source.parent / ('.fixture-' + token + '.jsonl')
    try:
        journal = journal_experimental.Journal(journal_path)
    except OSError as exc:
        return {'status': 'JOURNAL_FAILED', 'source': str(source), 'detail': str(exc)}
    try:
        result = _run_fixture(source, expected, backend, journal, token)
        return {**result, 'journal': str(journal_path)}
    finally:
        journal.close()


def _run_fixture(source, expected, backend, journal, token):
    """Stage an owned fixture, call an injected backend, and attempt safe recovery.

    `backend` is injected by the test and must not be used with real user files.
    A missing staged path after backend invocation is always UNKNOWN.
    """
    source = Path(os.path.abspath(source))
    staged_name = '.disk-cleanup-fixture-' + token
    try:
        journal.append('INTENT', source=str(source), staged=str(source.with_name(staged_name)), identity=expected)
    except OSError as exc:
        return {'status': 'JOURNAL_FAILED', 'source': str(source), 'detail': str(exc)}
    try:
        staged = stage_experimental.stage_fixture(source, expected, staged_name)
    except (OSError, ValueError, KeyError) as exc:
        return {'status': 'STAGE_FAILED', 'source': str(source), 'detail': str(exc)}

    try:
        journal.append('BACKEND_INTENT')
        backend(staged)
        detail = 'backend returned; outcome is not verified'
    except Exception as exc:
        detail = f'backend raised: {exc}'

    if not os.path.lexists(staged):
        return {'status': 'UNKNOWN', 'source': str(source), 'staged': str(staged),
                'detail': detail + '; staged object is absent'}

    try:
        # Handle-based rename checks identity and refuses to replace any existing
        # original path, including one created after the backend was invoked.
        journal.append('RESTORE_INTENT')
        restored = stage_experimental.stage_fixture(staged, expected, source.name)
    except (OSError, ValueError, KeyError) as exc:
        return {'status': 'RECOVERY_REQUIRED', 'source': str(source), 'staged': str(staged),
                'detail': detail + f'; automatic recovery failed: {exc}'}
    try:
        actual = windows_identity.capture(restored)
        if (actual['volume_serial'], actual['file_id']) != (expected['volume_serial'], expected['file_id']):
            return {'status': 'RECOVERY_UNKNOWN', 'source': str(source), 'staged': str(staged),
                    'detail': detail + '; identity could not be confirmed after restore'}
        journal.append('RESTORED')
        return {'status': 'RESTORED', 'source': str(source), 'staged': str(staged), 'detail': detail}
    except (OSError, ValueError, KeyError) as exc:
        return {'status': 'RECOVERY_UNKNOWN', 'source': str(source), 'staged': str(staged),
                'detail': detail + f'; restored identity unavailable: {exc}'}
