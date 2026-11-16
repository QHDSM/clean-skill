#!/usr/bin/env python3
"""Plan-gated cleanup executor for Windows.

Execution is Recycle Bin only, and every trashed object carries proof that it
really landed in the bin. Python 3.10+, standard library only.

Three modes:

  python scripts/clean.py --plan plan.json                 # dry-run: show what would happen
  python scripts/clean.py --plan plan.json --go            # execute
  python scripts/clean.py --undo <audit-log.jsonl>         # restore a run's trash
  python scripts/clean.py --path 'C:\\temp\\x' --confirm-path 'C:\\temp\\x' --go
                                                           # single-path mode, double entry

Safety model (enforced here, not in prose):
  1. The plan is the ONLY input. The executor never re-derives targets from a scan.
  2. A gate validates the whole plan before anything runs; one bad target rejects all.
  3. Every path passes preflight: absolute, allowed scope, not excluded, not denied,
     and no .git anywhere on the path chain.
  4. Every target is re-verified against the fingerprint recorded at scan time
     (st_dev, st_ino, st_size, st_mtime_ns); st_ctime is recorded but not compared,
     because a read bumps it on Windows. A changed object is skipped.
  5. The backend holds a handle across the delete and reads the object's final
     path back from the kernel: inside the Recycle Bin means recycled, inside
     the NTFS deleted store means permanently deleted. Only the first is
     success. A progress-sink veto additionally refuses any delete the Shell
     did not mark as a recycle, which is the only guard against the Shell
     silently falling back to permanent deletion on a volume with no bin.
  6. An object that is in use is refused before the Shell is asked, not after:
     the backend's own handle open is the probe, and it fails with a sharing
     violation instead of producing a half-finished move.
  7. Space inside the Recycle Bin is NOT reclaimed until it is emptied; the report
     says so instead of claiming the space was freed.
  8. The audit log doubles as the undo log: it records the metadata/index pair,
     so restore never has to guess or invoke a localized Shell verb.
"""
import argparse
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # reuse check_path / unsafe / fingerprint; read-only helpers only
import git_discovery
import locks
import recycle_backend
import recycle_bin
import tree_evidence
import windows_identity
import windows_parents
import windows_paths

SCHEMA = 'disk-cleanup-plan/v1'
ACTIONS = {'trash', 'keep', 'report'}
RISKS = {'safe', 'caution', 'never'}
GIT_VERDICTS = {'SAFE_TO_CONSIDER', 'LOCAL_ONLY_DATA_PRESENT', 'LOCAL_MODIFICATIONS', 'UNVERIFIED'}
BATCH_LIMIT = 200

# Two levels of protection, deliberately different:
#   DENY_TREE  - the whole subtree is off limits (system internals, boot, recovery)
#   DENY_SELF  - only the directory itself is off limits; named caches below it are
#                legitimate. ProgramData is the important case: the root is protected
#                but C:\ProgramData\Package Cache is a normal cleanup target.
DENY_TREE = (
    'c:\\windows', 'c:\\$recycle.bin', 'c:\\system volume information',
    'c:\\recovery', 'c:\\boot', 'c:\\perflogs', 'c:\\config.msi',
)
DENY_SELF = {
    'c:\\program files', 'c:\\program files (x86)', 'c:\\programdata',
    'c:\\users', 'c:\\users\\public', 'c:\\users\\default',
}
PERSONAL_TOP = {
    'documents', 'desktop', 'downloads', 'pictures', 'music', 'videos', 'movies',
    'onedrive', 'appdata', 'favorites', 'contacts', 'links', 'saved games', 'searches',
}
DRIVE_ROOT = re.compile(r'^[a-z]:$')
PROFILE_ROOT = re.compile(r'^[a-z]:\\users\\[^\\]+$')
PERSONAL_DIR = re.compile(r'^[a-z]:\\users\\[^\\]+\\([^\\]+)$')


class GateError(Exception):
    """The plan itself is unacceptable. Nothing is executed."""


def die(code, message):
    print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False), file=sys.stderr)
    return code


GOAL_SCHEMA = 'disk-cleanup-goal/v1'
GOAL_KINDS = {'move_bytes'}


def validate_goal(goal):
    """Validate a capacity goal, and refuse one whose own numbers disagree.

    The goal is a *move* goal: this executor only ever fills the Recycle Bin, so
    a plan may say how many bytes leave their current location, never how much
    free space appeared. `met` and `shortfall_bytes` are recomputed rather than
    trusted, so a plan cannot claim success by editing a boolean.
    """
    if not isinstance(goal, dict):
        raise GateError('goal must be an object or null')
    if goal.get('schema') != GOAL_SCHEMA:
        raise GateError(f'goal.schema must be {GOAL_SCHEMA!r}')
    if goal.get('kind') not in GOAL_KINDS:
        raise GateError(f'goal.kind must be one of {sorted(GOAL_KINDS)}')
    for field in ('requested_bytes', 'estimated_bytes'):
        value = goal.get(field)
        if type(value) is not int or value < 0:
            raise GateError(f'goal.{field} must be a non-negative integer')
    shortfall = max(0, goal['requested_bytes'] - goal['estimated_bytes'])
    if 'shortfall_bytes' in goal and goal['shortfall_bytes'] != shortfall:
        raise GateError('goal.shortfall_bytes does not match requested minus estimated')
    if 'met' in goal and goal['met'] is not (shortfall == 0):
        raise GateError('goal.met does not match requested minus estimated')


# ------------------------------------------------------------------- run lock

# Only one execution may move objects at a time: two runs interleaving would
# both see the same object as present, both reserve its bytes, and the second
# would report a move that the first already made. Dry-runs do not take it.
_MUTEX_NAME = 'Local\\disk-cleanup-execution'
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_lock_handle = None


def acquire_execution_lock():
    """Take the per-user run lock. Returns (ok, reason); never raises."""
    global _lock_handle
    if _lock_handle is not None:
        return False, 'another cleanup run is in progress in this process'
    if os.name != 'nt':
        return True, ''  # the executor itself is Windows-only
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return False, 'cannot create the run mutex (error %d)' % ctypes.get_last_error()
    waited = kernel32.WaitForSingleObject(handle, 0)
    if waited not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        kernel32.CloseHandle(handle)
        return False, 'another cleanup run is in progress; wait for it to finish'
    _lock_handle = handle
    return True, ''


def release_execution_lock():
    global _lock_handle
    if _lock_handle is None:
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.ReleaseMutex(_lock_handle)
    kernel32.CloseHandle(_lock_handle)
    _lock_handle = None


# --------------------------------------------------------------------------- gate

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def _bad_json_constant(value):
    raise GateError(f'invalid JSON constant: {value}')


def load_plan_with_digest(path):
    """Validate and hash exactly the same bytes, without reopening the plan."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise GateError(f'cannot read plan: {exc}')
    try:
        plan = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=_bad_json_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GateError(f'plan is not valid JSON: {exc}')
    if not isinstance(plan, dict):
        raise GateError('plan must be a JSON object')
    if plan.get('schema') != SCHEMA:
        raise GateError(f'schema must be {SCHEMA!r}, got {plan.get("schema")!r}')

    auth = plan.get('authorization')
    if not isinstance(auth, dict):
        raise GateError('authorization must be an object')
    scope = auth.get('scope')
    if not isinstance(scope, list) or not scope:
        raise GateError('authorization.scope must be a non-empty array')
    for item in scope:
        if not isinstance(item, str) or not _has_drive(item):
            raise GateError(f'scope entry is not an absolute drive path: {item!r}')
    excluded = auth.get('excluded', [])
    if not isinstance(excluded, list) or not all(isinstance(x, str) and _has_drive(x) for x in excluded):
        raise GateError('authorization.excluded must be an array of absolute drive paths')
    if auth.get('permanent_delete', False) is not False:
        raise GateError('this executor never performs permanent deletion')
    if 'git_acknowledged' in auth and type(auth['git_acknowledged']) is not bool:
        raise GateError('authorization.git_acknowledged must be boolean')

    goal = plan.get('goal')
    if goal is not None:
        validate_goal(goal)

    targets = plan.get('targets')
    if not isinstance(targets, list) or not targets:
        raise GateError('targets must be a non-empty array')
    if len(targets) > BATCH_LIMIT:
        raise GateError(f'targets exceed the batch limit of {BATCH_LIMIT}')

    claimed = {}
    for index, target in enumerate(targets):
        where = f'targets[{index}]'
        if not isinstance(target, dict):
            raise GateError(f'{where} must be an object')
        path_value = target.get('path')
        if not isinstance(path_value, str) or not _has_drive(path_value):
            raise GateError(f'{where}.path must be an absolute drive path')
        action = target.get('action')
        if not isinstance(action, str) or action not in ACTIONS:
            raise GateError(f'{where}.action must be one of {sorted(ACTIONS)}')
        risk = target.get('risk')
        if not isinstance(risk, str) or risk not in RISKS:
            raise GateError(f'{where}.risk must be one of {sorted(RISKS)}')
        reason = target.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise GateError(f'{where}.reason must be a non-empty string')

        evidence = target.get('evidence')
        if not isinstance(evidence, dict):
            raise GateError(f'{where}.evidence must be an object')
        fp = evidence.get('fingerprint')
        if not isinstance(fp, list) or len(fp) != 5:
            raise GateError(f'{where}.evidence.fingerprint must be 5 integers')
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in fp):
            raise GateError(f'{where}.evidence.fingerprint must be 5 integers')
        parents = evidence.get('windows_parent_chain')
        if parents is not None:
            try:
                windows_parents.validate(path_value, parents)
            except (OSError, ValueError) as exc:
                raise GateError(f'{where}.evidence.windows_parent_chain: {exc}')
        file_id = evidence.get('windows_file_id')
        if file_id is not None:
            if (not isinstance(file_id, dict) or file_id.get('schema') != windows_identity.SCHEMA
                    or not isinstance(file_id.get('path'), str)
                    or not _has_drive(file_id['path'])
                    or scan.normalized(file_id['path']) != scan.normalized(path_value)
                    or type(file_id.get('volume_serial')) is not int
                    or not 0 < file_id['volume_serial'] < (1 << 64)
                    or not isinstance(file_id.get('file_id'), str)
                    or not re.fullmatch(r'[0-9a-f]{32}', file_id['file_id'])
                    or file_id['file_id'] == '0' * 32):
                raise GateError(f'{where}.evidence.windows_file_id must match the target')
        tree = evidence.get('tree_metadata')
        if tree is not None:
            if (not isinstance(tree, dict) or tree.get('schema') != tree_evidence.SCHEMA
                    or tree.get('complete') is not True
                    or not isinstance(tree.get('root'), str)
                    or not _has_drive(tree['root'])
                    or scan.normalized(tree['root']) != scan.normalized(path_value)
                    or not isinstance(tree.get('digest'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', tree['digest'])):
                raise GateError(f'{where}.evidence.tree_metadata must be complete and match the target')

        # Scope containment and exclusion are checked here so a bad plan runs nothing.
        if not any(scan.within(path_value, root) for root in scope):
            raise GateError(f'{where}.path is outside every authorized scope: {path_value}')
        if any(scan.within(path_value, root) or scan.within(root, path_value)
               for root in excluded):
            raise GateError(f'{where}.path overlaps an excluded root: {path_value}')
        key = scan.normalized(path_value)
        if key in claimed:
            raise GateError(
                f'{where}.path repeats an earlier target ({claimed[key]}): {path_value}')
        claimed[key] = where

        git = evidence.get('git')
        if git is not None:
            if not isinstance(git, dict):
                raise GateError(f'{where}.evidence.git must be an object or null')
            verdict = git.get('verdict')
            if not isinstance(verdict, str) or verdict not in GIT_VERDICTS:
                raise GateError(f'{where}.evidence.git.verdict must be a recognized verdict')
            safe_evidence = (verdict == 'SAFE_TO_CONSIDER' and git.get('all_content_synced') is True
                             and git.get('unknown') == [] and git.get('risks') == [])
            if action == 'trash' and not safe_evidence:
                if auth.get('git_acknowledged') is not True:
                    raise GateError(
                        f'{where} records unverified Git evidence {verdict!r} and '
                        'authorization.git_acknowledged is not true')

    _reject_overlapping_targets(claimed)
    return plan, hashlib.sha256(raw).hexdigest()


def _reject_overlapping_targets(claimed):
    """A target inside another target cannot be verified once the parent moves.

    Trashing a directory takes its children with it, so a plan that lists both
    would move the child twice, budget it twice, and then report the second move
    against a path that no longer exists. Rejecting the plan keeps the report
    honest and the capacity budget exact.
    """
    for path, where in sorted(claimed.items()):
        parts = path.split(os.sep)
        for depth in range(2, len(parts)):
            parent = os.sep.join(parts[:depth])
            if parent in claimed:
                raise GateError(
                    f'{where}.path is inside another target ({claimed[parent]}): {path}')


def load_plan(path):
    return load_plan_with_digest(path)[0]


def _has_drive(value):
    return windows_paths.supported(value)


# ----------------------------------------------------------------------- preflight

def is_denied(path):
    """Return a refusal reason for protected locations, else None."""
    text = str(path).replace('/', '\\').lower().rstrip('\\')
    if DRIVE_ROOT.match(text):
        return 'volume root'
    if text in DENY_SELF:
        return 'protected system location'
    for root in DENY_TREE:
        if text == root or text.startswith(root + '\\'):
            return 'inside a protected system tree'
    if PROFILE_ROOT.match(text):
        return 'user profile root'
    match = PERSONAL_DIR.match(text)
    if match and match.group(1) in PERSONAL_TOP:
        return 'top-level personal folder'
    if any(part.lower() == '.git' for part in Path(path).parts):
        return 'inside a .git directory'
    return None


def preflight(target, plan):
    """Return (ok, reason) for one target. Never raises for a bad target."""
    raw = target['path']
    if not os.path.lexists(raw):
        return False, 'does not exist'
    try:
        path = scan.check_path(raw)          # rejects link/reparse/offline ancestors
    except (OSError, ValueError) as exc:
        return False, str(exc)
    if not os.path.lexists(path):
        return False, 'does not exist'
    denied = is_denied(path)
    if denied:
        return False, denied
    if target['risk'] == 'never':
        return False, 'risk=never is never executed'
    return True, 'ok'


# ------------------------------------------------------------------------ reverify

def reverify(target):
    """Compare the live object against the fingerprint recorded at scan time.

    Only fields that survive a read are compared. On Windows, opening a file can
    update its last-access time, which in turn bumps NTFS ChangeTime (st_ctime).
    Counting st_ctime here would turn "the agent read this file to judge it" into
    "the file changed", so the comparison stops at size and mtime.
    """
    path = Path(target['path'])
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False, 'gone since the scan'
    except OSError as exc:
        return False, f'cannot stat: {exc}'
    if scan.unsafe(st):
        return False, 'is now a link, reparse point or offline object'

    expected = target['evidence']['fingerprint']
    current = list(scan.fingerprint(st))
    # st_ino is legitimately 0 on some Windows volumes; comparing it then is noise.
    if current[1] == 0 or expected[1] == 0:
        pairs = (2, 3)                       # size, mtime_ns
        if any(current[i] != expected[i] for i in pairs):
            return False, 'changed since the scan (identity unavailable)'
        return True, 'ok (st_ino unavailable; identity confidence reduced)'
    pairs = (0, 1, 2, 3)                     # dev, ino, size, mtime_ns
    if any(current[i] != expected[i] for i in pairs):
        return False, 'changed since the scan'
    return True, 'ok'


# ------------------------------------------------------------------------- execute

def plan_evidence(target):
    """The facts the plan rested on, shaped for a check under the hold.

    ``reverify`` compares against the path just before the lock probe; between
    that check and the Shell call the path can be repointed. The backend
    re-reads these values through the handle it holds, so the comparison is
    about the object that would actually move.
    """
    evidence = target.get('evidence') or {}
    fingerprint = evidence.get('fingerprint') or []
    expected = {'size_bytes': evidence.get('size_bytes'),
                'mtime_ns': fingerprint[3] if len(fingerprint) > 3 else None}
    recorded = evidence.get('windows_file_id')
    if isinstance(recorded, dict) and recorded.get('file_id'):
        expected['identity'] = {'volume_serial': recorded.get('volume_serial'),
                                'file_id': recorded.get('file_id')}
    return expected


def trash_one(path, *, size_bytes=0, expected=None):
    """Trash one object and return the backend's proof-carrying verdict.

    The verdict's ``status`` is the only success signal. REFUSED, FAILED and
    UNKNOWN are all reported as failures; none of them is upgraded to "probably
    fine", because the whole point of the backend is that "gone" is not evidence.
    """
    kwargs = {'size_bytes': size_bytes or 0}
    if expected:
        kwargs['expected'] = expected
    return recycle_backend.recycle(path, **kwargs)


def volume_free(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0] + '\\'
    try:
        return shutil.disk_usage(drive).free
    except OSError:
        return None


# --------------------------------------------------------------------------- audit

RUN_DIR = Path.home() / '.disk-cleanup' / 'runs'


class AuditJournal:
    """Append-only run manifest: one JSONL file per run, flushed per record.

    One file per run, not one per day, because the same file is also the undo
    manifest: ``--undo`` must restore exactly the objects from one run and never
    rescan a shared day log that also holds other runs' records.

    The file is appended to and fsynced as each object is trashed instead of
    being written once at the end. A crash, a Ctrl+C or a power cut therefore
    leaves a usable manifest for everything that already moved; writing the whole
    run at the end would lose precisely the case the manifest exists for. A
    truncated final line is skipped by the reader, not treated as fatal.
    """

    def __init__(self, path, handle, plan_path, plan_digest):
        self.path = str(path)
        self.run_id = path.stem
        self._handle = handle
        self._plan = plan_path
        self._digest = plan_digest
        self._seq = 0
        self.records = []
        self.broken = False

    @classmethod
    def create(cls, plan_path, plan_digest):
        """Open a fresh manifest, or return None when it cannot be created."""
        import uuid

        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        # Exclusive create, retried: two runs in the same second must never share
        # a file, because that file is also the undo manifest.
        for _ in range(8):
            stamp = time.strftime('%Y%m%d-%H%M%S')
            log = RUN_DIR / f'audit-{stamp}-{uuid.uuid4().hex[:6]}.jsonl'
            try:
                handle = log.open('x', encoding='utf-8')
            except FileExistsError:
                continue
            except OSError:
                return None
            return cls(log, handle, plan_path, plan_digest)
        return None

    def append(self, record):
        """Add one undo record and force it to disk. False when it did not land."""
        if self._handle is None:
            return False
        row = {'run_id': self.run_id, 'seq': self._seq, 'ts': int(time.time()),
               'plan': self._plan, 'plan_digest': self._digest, **record}
        try:
            self._handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except (OSError, ValueError):
            self.broken = True
            return False
        self._seq += 1
        self.records.append(row)
        return True

    def close(self):
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def latest_run_log():
    """Newest run manifest, or None when no run has ever been recorded."""
    try:
        candidates = sorted(RUN_DIR.glob('audit-*.jsonl'))
    except OSError:
        return None
    return str(candidates[-1]) if candidates else None


def _read_audit_log(path):
    """Read an audit log without trusting it: unknown lines are counted, not fatal."""
    records, issues = [], []
    try:
        with open(path, encoding='utf-8') as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    issues.append(f'line {number}: not JSON')
                    continue
                if not isinstance(record, dict):
                    issues.append(f'line {number}: not an object')
                    continue
                records.append(record)
    except OSError as exc:
        raise GateError(f'cannot read audit log: {exc}')
    return records, issues


def _recover_attempted(record, done):
    """Undo one intent line whose companion record never made it to disk.

    The run was cut off between the Shell move and the record. The bin still has
    the object, so look it up by original path inside a tight window around the
    intent timestamp. The window is what keeps this from grabbing an older
    deletion of the same path; the intent line is also why this cannot run for
    an object the run never reached.
    """
    import recycle_bin

    path = record.get('path')
    if not path or recycle_bin.canonical_path(path) in done:
        return None
    item = {'path': path, 'action': 'restore', 'recovered': True}
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    if not drive:
        item.update(result='skipped', detail='record has no absolute path')
        return item
    stamp = record.get('ts')
    try:
        entry = recycle_bin.find_entry(drive, path,
                                       deleted_unix=stamp if stamp else None,
                                       tolerance=5 if stamp else 120)
    except (OSError, ValueError) as exc:
        item.update(result='failed', detail=f'cannot look in the Recycle Bin: {exc}')
        return item
    if entry is None:
        item.update(result='skipped',
                    detail='the run ended before this object was recorded and no '
                           'matching Recycle Bin entry exists')
        return item
    outcome = recycle_bin.restore(entry)
    item.update(result='restored' if outcome['ok'] else 'failed',
                detail='recovered after an incomplete run: ' + outcome['detail'],
                restored_to=outcome.get('restored_to'),
                index_removed=outcome.get('i_removed'))
    return item


def _verify_restored(record, path):
    """Check that the object at `path` is the one the run moved.

    A restore by rename keeps the file identity, and the journal recorded it
    while the object was still in place, so "the path is back" can be checked
    instead of assumed. Anything that cannot be re-read stays honest: the
    answer is None with the reason, never a guess.
    """
    expected = record.get('identity_before') or None
    expected_size = record.get('size_bytes')
    if not (isinstance(expected, dict) and expected.get('file_id')):
        return {'verified': None,
                'verification': 'the run recorded no file identity for this object'}
    try:
        actual = recycle_backend.identity_of(path)
    except (OSError, ValueError, recycle_backend.BackendUnavailable) as exc:
        return {'verified': None, 'verification': f'cannot re-read the restored object: {exc}'}
    same = (actual.get('volume_serial') == expected.get('volume_serial')
            and str(actual.get('file_id', '')).lower() == str(expected['file_id']).lower())
    if not same:
        return {'verified': False,
                'verification': 'the restored object is not the one the run trashed '
                                '(file identity differs)'}
    if expected_size is not None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            return {'verified': None, 'verification': f'cannot stat the restored object: {exc}'}
        if not stat.S_ISDIR(info.st_mode) and info.st_size != expected_size:
            return {'verified': False,
                    'verification': f'its size changed while it was in the Recycle Bin '
                                    f'({expected_size} -> {info.st_size} bytes)'}
    return {'verified': True, 'verification': 'the file identity matches the recorded object'}


def undo_run(args):
    """Restore the objects a previous run trashed, using only the audit log.

    The log names the exact metadata/index pair, so restore never scans the bin
    for "something similar" and never depends on a localized Shell verb. Every
    record is restored independently and nothing is ever overwritten: if the
    original path has been reused, that object stays in the bin and the conflict
    is reported instead of resolved by force.
    """
    import recycle_bin

    path = Path(args.undo) if getattr(args, 'undo', None) else None
    if path is None:
        newest = latest_run_log()
        if not newest:
            raise GateError('no run manifest found under ' + str(RUN_DIR))
        path = Path(newest)
    if not path.is_absolute():
        raise GateError('--undo must be an absolute path to an audit log')
    if not path.is_file():
        raise GateError(f'audit log not found: {path}')
    records, log_issues = _read_audit_log(path)

    # Canonical, not raw: the log holds the path the caller had, and the bin
    # holds the path the Shell saw. The same object can appear under both.
    done = {recycle_bin.canonical_path(record.get('path') or '')
            for record in records if record.get('result') == 'trashed'}
    results = []
    for record in records:
        if record.get('result') == 'attempted':
            item = _recover_attempted(record, done)
            if item is not None:
                results.append(item)
            continue
        if record.get('result') != 'trashed':
            continue
        original = record.get('path')
        entry = record.get('recycle_bin') or {}
        item = {'path': original, 'action': 'restore'}
        if not original or not entry.get('i_path'):
            item.update(result='skipped', detail='record has no recycle metadata')
            results.append(item)
            continue
        parsed = recycle_bin.parse_entry(entry['i_path'])
        if not parsed['valid']:
            item.update(result='skipped',
                        detail='metadata record is unreadable: ' + '; '.join(parsed['issues']))
            results.append(item)
            continue
        # The bin's own record of where the object came from must agree with the
        # log. A mismatch means one of the two was rewritten after the run.
        # The two sides can spell the same path differently -- the log holds
        # the short name the caller had, the bin holds the long name the Shell
        # saw -- so compare canonically. A real disagreement still refuses.
        if not recycle_bin.same_path(parsed['original_path'], original):
            item.update(result='refused',
                        detail='the bin metadata names a different original path: '
                               + parsed['original_path'])
            results.append(item)
            continue
        outcome = recycle_bin.restore(parsed)
        item.update(result='restored' if outcome['ok'] else 'failed',
                    detail=outcome['detail'],
                    restored_to=outcome.get('restored_to'),
                    index_removed=outcome.get('i_removed'))
        if outcome['ok']:
            item.update(_verify_restored(record, item['path']))
        results.append(item)

    restored = [r for r in results if r['result'] == 'restored']
    return {
        'mode': 'UNDO',
        'plan': None,
        'plan_digest': None,
        'audit_log': str(path),
        'execution_available': recycle_backend.availability()['available'],
        'preview_warning': None,
        'log_issues': log_issues,
        'summary': {
            'total': len(results),
            'trashed': len(restored),
            'dry_run': 0,
            'skipped': len([r for r in results if r['result'] == 'skipped']),
            'refused': len([r for r in results if r['result'] == 'refused']),
            'not_executed': 0,
            'failed': len([r for r in results if r['result'] == 'failed']),
            'recovered': len([r for r in results if r.get('recovered')]),
            'verified': len([r for r in restored if r.get('verified') is True]),
            'unverified': len([r for r in restored if r.get('verified') is not True]),
            'moved_bytes': 0,
        },
        'volumes': [],
        'recycle_bin_note': None,
        'results': results,
    }


# ---------------------------------------------------------------------------- main

def bin_budget(drive, profiles, budgets):
    """Per-volume Recycle Bin budget, resolved once per run.

    Capacity is enforced per volume and counts what the bin already holds, so a
    batch can overflow a bin that would accept each object on its own. The
    numbers are only trusted when the whole bin could be read: an unreadable
    bin (another user's SID, or a locked entry) makes the headroom unknown, and
    unknown is reported as such instead of being treated as zero.
    """
    if drive in budgets:
        return budgets[drive]
    try:
        profile = profiles.get(drive)
        if profile is None:
            profile = profiles[drive] = recycle_backend.volume_profile(drive)
        capacity_mb = profile.get('max_capacity_mb')
        capacity = capacity_mb * 1024 * 1024 if isinstance(capacity_mb, int) else None
        try:
            usage = recycle_bin.bin_usage(drive)
        except (OSError, ValueError) as exc:
            usage = {'bytes': None, 'complete': False, 'issues': [str(exc)], 'entries': 0}
    except recycle_backend.BackendUnavailable as exc:
        budgets[drive] = {'drive': drive, 'capacity_bytes': None, 'used_bytes': None,
                          'reserved_bytes': 0, 'complete': False,
                          'issues': [str(exc)], 'entries': 0}
        return budgets[drive]
    budgets[drive] = {
        'drive': drive,
        'capacity_bytes': capacity,
        'used_bytes': usage['bytes'] if usage['complete'] else None,
        'reserved_bytes': 0,
        'complete': bool(usage['complete']),
        'issues': list(usage.get('issues') or []),
        'entries': usage.get('entries'),
    }
    return budgets[drive]


def _budget_fit(budget, size_bytes):
    """Can this object still be added on top of what the bin already holds?"""
    capacity, used = budget['capacity_bytes'], budget['used_bytes']
    if capacity is None:
        return True, None, 'capacity unknown; the per-object check and the callback still apply'
    if used is None:
        return True, None, 'current bin usage unknown; the per-object check and the callback still apply'
    room = capacity - used - budget['reserved_bytes']
    if (size_bytes or 0) > room:
        return False, room, (f'the batch would exceed the Recycle Bin capacity for '
                            f'{budget["drive"]}: {room} bytes left after earlier targets, '
                            f'{size_bytes or 0} needed; split the batch or empty the bin')
    return True, room, None


def run(args):
    if getattr(args, 'undo', None) or getattr(args, 'undo_last', False):
        return undo_run(args)
    if args.go:
        backend = recycle_backend.availability()
        if not backend['available']:
            raise GateError(f'execution unavailable: {backend["reason"]}')
    if not args.plan and not args.path:
        raise GateError('either --plan or --path is required')

    results = []
    audit_warning = None
    refused, skipped = [], []
    before, after = {}, {}

    if args.plan:
        plan, digest = load_plan_with_digest(args.plan)
        targets = plan['targets']
    else:
        # Single-path mode: double entry proves the operator knows the exact target.
        if args.path and not _has_drive(args.path):
            raise GateError('--path must use a supported absolute drive path')
        if args.confirm_path and not _has_drive(args.confirm_path):
            raise GateError('--confirm-path must use a supported absolute drive path')
        first, second = Path(args.path), Path(args.confirm_path or '')
        if not args.confirm_path:
            raise GateError('--path requires --confirm-path with the same absolute path')
        if not first.is_absolute() or not second.is_absolute():
            raise GateError('--path and --confirm-path must both be absolute')
        if os.path.normcase(os.path.abspath(first)) != os.path.normcase(os.path.abspath(second)):
            raise GateError('--confirm-path must name the same object as --path')
        try:
            st = os.lstat(first)
        except OSError as exc:
            raise GateError(f'cannot stat {first}: {exc}')
        risk = args.risk
        if risk not in RISKS - {'never'}:
            raise GateError(f'--risk must be safe or caution, got {risk!r}')
        plan = None
        digest = 'single-path'
        targets = [{
            'path': str(first), 'action': 'trash', 'risk': risk,
            'reason': args.reason or 'explicit single-path request',
            'evidence': {'fingerprint': list(scan.fingerprint(st)),
                         'size_bytes': None if stat.S_ISDIR(st.st_mode) else st.st_size},
        }]
        # Single-path mode still passes the same preflight as plan mode.
        for target in targets:
            denied = is_denied(Path(target['path']))
            if denied:
                raise GateError(f'refused: {denied}')

    aborted_after = None
    # Created before the first target: if the process dies mid-run, every object
    # already in the bin still has its undo record on disk.
    journal = AuditJournal.create(args.plan, digest) if args.go else None
    # Recycle Bin capacity is per volume, so the preview resolves it once.
    profiles = {}
    budgets = {}

    for position, target in enumerate(targets):
        path = target['path']
        entry = {'path': path, 'action': target['action'], 'risk': target['risk']}

        if target['action'] != 'trash':
            entry.update(result='not_executed', detail=f"action={target['action']}")
            results.append(entry)
            continue

        ok, why = preflight(target, plan)
        if not ok:
            entry.update(result='refused', detail=why)
            refused.append(entry)
            results.append(entry)
            continue

        ok, why = reverify(target)
        if not ok:
            entry.update(result='skipped', detail=why)
            skipped.append(entry)
            results.append(entry)
            continue

        entry['directory_contents_verified'] = False if os.path.isdir(path) else None
        discovery = entry['git_discovery'] = git_discovery.discover(path)
        if not discovery['complete'] or (discovery['markers'] and target['evidence'].get('git') is None):
            detail = 'Git discovery incomplete' if not discovery['complete'] else 'Git markers found but audit evidence missing'
            entry.update(result='refused', detail=detail)
            refused.append(entry)
            results.append(entry)
            continue

        file_id = target['evidence'].get('windows_file_id')
        if file_id is not None:
            try:
                current_id = windows_identity.capture(path)
            except (OSError, ValueError) as exc:
                entry.update(result='skipped', detail=f'Windows file identity unavailable: {exc}')
                skipped.append(entry)
                results.append(entry)
                continue
            if (current_id['volume_serial'], current_id['file_id']) != (file_id['volume_serial'], file_id['file_id']):
                entry.update(result='skipped', detail='Windows file identity changed')
                skipped.append(entry)
                results.append(entry)
                continue
            entry['windows_file_id_comparison'] = 'matched'

        parents = target['evidence'].get('windows_parent_chain')
        if parents is not None:
            try:
                expected_chain = windows_parents.validate(path, parents)
                with windows_parents.PinnedParents(Path(path).parent, expected_chain):
                    entry['windows_parent_chain_comparison'] = 'matched_at_inspection'
            except (OSError, ValueError) as exc:
                entry.update(result='skipped', detail=f'Windows parent chain unavailable or changed: {exc}')
                skipped.append(entry)
                results.append(entry)
                continue

        tree = target['evidence'].get('tree_metadata')
        if tree is not None:
            current_tree = tree_evidence.capture(path)
            if not current_tree['complete'] or current_tree['digest'] != tree['digest']:
                entry.update(result='skipped', detail='directory metadata changed or comparison incomplete',
                             tree_metadata_comparison=current_tree)
                skipped.append(entry)
                results.append(entry)
                continue
            entry['tree_metadata_comparison'] = {'matched': True,
                                                  'entries': current_tree['entries']}

        entry['size_bytes'] = target['evidence'].get('size_bytes')
        if entry['size_bytes'] is None:
            # A target without a recorded size would be budgeted as zero bytes,
            # letting a single huge file skip the capacity gate entirely.
            try:
                info = os.lstat(path)
                entry['size_bytes'] = None if stat.S_ISDIR(info.st_mode) else info.st_size
            except OSError:
                entry['size_bytes'] = None
        entry['fingerprint'] = target['evidence']['fingerprint']

        # An object that something else holds open cannot be recycles; asking the
        # Shell anyway produces a half-finished move at best. Probe first, and in
        # a dry run also say so, because "this will be refused" is the useful
        # prediction. The full Restart Manager lookup (which names the process)
        # only runs when the probe already says no, plus during --go.
        if args.go:
            held = locks.blockers(path)
        else:
            probe = locks.probe_deletable(path)
            held = {'deletable': probe['deletable'], 'reason': probe['reason'],
                    'processes': [], 'issues': probe.get('issues', []),
                    'complete': probe['deletable'] is not None}
        if held['deletable'] is not True:
            processes = [p for p in held.get('processes', []) if p.get('pid')]
            detail = held['reason'] or 'deletability unknown'
            if processes:
                detail += ' (held by ' + ', '.join(
                    f"{p.get('app') or 'unknown'} [pid {p['pid']}]" for p in processes[:4]) + ')'
            entry.update(result='refused', detail=detail,
                         blockers={'reason': held['reason'], 'processes': processes,
                                   'complete': held.get('complete')})
            refused.append(entry)
            results.append(entry)
            continue

        if not args.go:
            # Ask the same questions --go would ask, so a plan whose objects cannot
            # fit in the bin is visible before anything moves. Windows silently
            # offers a permanent delete for an oversized object; we refuse, but the
            # preview should say so up front.
            drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
            try:
                profile = profiles.get(drive)
                if profile is None:
                    profile = profiles[drive] = recycle_backend.volume_profile(drive)
                fits, why = recycle_backend.fits_capacity(profile, entry.get('size_bytes') or 0)
                budget = bin_budget(drive, profiles, budgets)
                batch_fits, room, batch_why = _budget_fit(budget, entry.get('size_bytes'))
                if fits and not batch_fits:
                    fits, why = False, batch_why
                entry['bin_fit'] = {
                    'fits': fits, 'reason': why,
                    'max_capacity_mb': profile.get('max_capacity_mb'),
                    'verified_capable': profile.get('verified_capable'),
                    'issues': profile.get('issues', []),
                    'batch_fits': batch_fits,
                    'room_bytes': room,
                    'bin_used_bytes': budget['used_bytes'],
                    'bin_usage_complete': budget['complete'],
                    'bin_usage_issues': budget['issues'],
                }
                if fits:
                    budget['reserved_bytes'] += entry.get('size_bytes') or 0
            except recycle_backend.BackendUnavailable as exc:
                entry['bin_fit'] = {'fits': None, 'reason': str(exc),
                                    'max_capacity_mb': None,
                                    'verified_capable': None, 'issues': []}
            entry.update(result='dry_run', detail='preview only; execution disabled')
            results.append(entry)
            continue

        key = os.path.splitdrive(os.path.abspath(path))[0].upper()
        budget = bin_budget(key, profiles, budgets)
        batch_fits, room, batch_why = _budget_fit(budget, entry.get('size_bytes'))
        if not batch_fits:
            entry['bin_fit'] = {'fits': False, 'reason': batch_why, 'batch_fits': False,
                                'room_bytes': room,
                                'bin_used_bytes': budget['used_bytes'],
                                'bin_usage_complete': budget['complete'],
                                'bin_usage_issues': budget['issues']}
            entry.update(result='refused', detail=batch_why)
            refused.append(entry)
            results.append(entry)
            continue
        budget['reserved_bytes'] += entry.get('size_bytes') or 0
        if key not in before:
            before[key] = volume_free(path)
        if journal is not None:
            # Written before the move: if the process dies between the Shell
            # putting the object in the bin and the record below landing, this
            # intent line is what lets --undo find the object again.
            journal.append({'result': 'attempted', 'path': path,
                            'action': target['action'], 'risk': target['risk'],
                            'size_bytes': entry.get('size_bytes')})
        outcome = trash_one(path, size_bytes=entry['size_bytes'],
                            expected=plan_evidence(target))
        moved = outcome['status'] == 'RECYCLED'
        entry.update(result='trashed' if moved else 'failed', detail=outcome['detail'])
        entry['recycle'] = {
            'status': outcome['status'],
            'proof': outcome.get('proof'),
            'recycle_bin': outcome.get('recycle_bin'),
            'volume': {k: outcome.get('volume', {}).get(k) for k in
                       ('drive', 'filesystem', 'verified_capable', 'issues')},
        }
        if moved:
            if journal is None:
                if audit_warning is None:
                    audit_warning = ('no undo manifest could be created; objects moved so '
                                     'far can still be restored from the Recycle Bin, but '
                                     'not by --undo')
            elif not journal.append({
                    'path': path, 'action': target['action'], 'risk': target['risk'],
                    'result': 'trashed', 'detail': outcome['detail'],
                    'size_bytes': entry.get('size_bytes'),
                    'identity_before': outcome.get('identity_before'),
                    'recycle_bin': outcome.get('recycle_bin'),
                    'proof': outcome.get('proof'),
            }):
                audit_warning = (f'the undo record for {path} could not be written; '
                                 'that object can still be restored from the Recycle '
                                 'Bin, but not by --undo')
        else:
            refused.append(entry)
        results.append(entry)

        if outcome['status'] == 'PERMANENTLY_DELETED':
            # The volume guarantee just failed for real: stop instead of asking
            # the Shell to delete anything else and hoping the next one is
            # recycled. Remaining targets are reported as skipped, not untouched
            # silently.
            aborted_after = path
            for later in targets[position + 1:]:
                skip = {'path': later['path'], 'action': later['action'],
                        'risk': later['risk'], 'result': 'skipped',
                        'detail': 'aborted after a permanent deletion was observed'}
                skipped.append(skip)
                results.append(skip)
            break

    if args.go:
        for target in targets:
            key = os.path.splitdrive(os.path.abspath(target['path']))[0].upper()
            if key in before and key not in after:
                after[key] = volume_free(target['path'])

    if journal is not None:
        journal.close()
    log_path = journal.path if (journal is not None and not journal.broken
                                and journal.records) else None
    if args.go and log_path is None and audit_warning is None and journal is not None \
            and journal.records:
        audit_warning = ('the run manifest could not be written; these objects can still be '
                         'restored from the Recycle Bin, but not by --undo')

    trashed = [r for r in results if r['result'] == 'trashed']
    freed = sum(r.get('size_bytes') or 0 for r in trashed)
    backend = recycle_backend.availability()
    goal_report = None
    if plan and plan.get('goal'):
        goal = plan['goal']
        planned = sum((t.get('evidence') or {}).get('size_bytes') or 0 for t in plan['targets'])
        goal_report = {
            'requested_bytes': goal['requested_bytes'],
            'estimated_bytes': goal['estimated_bytes'],
            'planned_evidence_bytes': planned,
            'unmeasured_targets': sum(1 for t in plan['targets']
                                      if (t.get('evidence') or {}).get('size_bytes') is None),
            'shortfall_bytes': max(0, goal['requested_bytes'] - goal['estimated_bytes']),
            'met_on_paper': goal['estimated_bytes'] >= goal['requested_bytes'],
            'reclaimed_bytes': 0,
            'note': ('estimated is scan-time logical size. reclaimed stays 0 because moving to '
                     'the Recycle Bin frees no space until it is emptied; moved_bytes also '
                     'under-reports directories, which have no size_bytes of their own.'),
        }
    return {
        'mode': 'EXECUTED' if args.go else 'DRY-RUN',
        'plan': args.plan,
        'plan_digest': digest,
        'goal': goal_report,
        'execution_available': backend['available'],
        'execution_unavailable_reason': None if backend['available'] else backend['reason'],
        'preview_warning': 'Directory contents and Git risks are not fully verified.',
        'summary': {
            'total': len(results),
            'trashed': len(trashed),
            'dry_run': len([r for r in results if r['result'] == 'dry_run']),
            'skipped': len(skipped),
            'refused': len(refused),
            'not_executed': len([r for r in results if r['result'] == 'not_executed']),
            'failed': len([r for r in results if r['result'] == 'failed']),
            'capacity_warnings': len([r for r in results
                                      if (r.get('bin_fit') or {}).get('fits') is False]),
            'volume_warnings': len([r for r in results
                                    if (r.get('bin_fit') or {}).get('verified_capable') is False]),
            'moved_bytes': freed,
        },
        'volumes': [{'drive': d, 'free_before': before[d], 'free_after': after.get(d)}
                    for d in sorted(before)],
        'capacity': [budgets[d] for d in sorted(budgets)],
        'recycle_bin_note': (
            'Moved items sit in the Recycle Bin. Space is NOT reclaimed until it is '
            'emptied; the volume figures above may show no change.' if trashed else None),
        'audit_log': log_path,
        'audit_warning': audit_warning,
        'aborted_after': aborted_after,
        'results': results,
    }


def render(report):
    lines = [f"=== {report['mode']} ===", f"plan: {report['plan'] or '(single path)'}"]
    if report['mode'] == 'UNDO':
        lines.append('audit: ' + report['audit_log'])
    elif not report.get('execution_available'):
        lines.append('Execution unavailable: '
                     + (report.get('execution_unavailable_reason') or 'unknown'))
    elif report['mode'] == 'DRY-RUN':
        lines.append('Dry-run only; add --go to execute. ' + report['preview_warning'])
    summary = report['summary']
    verb = 'restored' if report['mode'] == 'UNDO' else 'trashed'
    lines.append(
        'total %d | %s %d | dry-run %d | skipped %d | refused %d | failed %d'
        % (summary['total'], verb, summary['trashed'], summary['dry_run'],
           summary['skipped'], summary['refused'], summary['failed']))
    if report['mode'] == 'UNDO' and summary.get('trashed'):
        lines.append('  restores verified against the recorded file identity: %d/%d'
                     % (summary.get('verified', 0), summary['trashed']))
    goal = report.get('goal')
    if goal:
        state = ('goal met on paper' if goal['met_on_paper']
                 else 'SHORT by %d MiB' % (goal['shortfall_bytes'] >> 20))
        lines.append('  goal: move %d MiB, estimated %d MiB at scan time -- %s'
                     % (goal['requested_bytes'] >> 20, goal['estimated_bytes'] >> 20, state))
        lines.append('  goal: reclaimed 0 B until the Recycle Bin is emptied; '
                     '%d target(s) carry no size_bytes of their own'
                     % goal['unmeasured_targets'])
    if summary.get('capacity_warnings'):
        lines.append('  WARNING: %d target(s) exceed the Recycle Bin capacity; --go would '
                     'refuse them. Split or shrink the batch.' % summary['capacity_warnings'])
    if summary.get('volume_warnings'):
        lines.append('  WARNING: %d target(s) are on volumes that cannot be proven '
                     'recycle-capable.' % summary['volume_warnings'])
    for item in report['results']:
        lines.append('  %-10s %s' % (item['result'], item['path']))
        discovery = item.get('git_discovery')
        if discovery:
            for marker in discovery['markers']:
                lines.append('             Git marker: ' + marker)
            if not discovery['complete']:
                lines.append('             Git discovery incomplete: ' + '; '.join(discovery['issues']))
        if item.get('directory_contents_verified') is False:
            lines.append('             Directory contents not verified by fingerprint')
        if item.get('windows_file_id_comparison') == 'matched':
            lines.append('             Windows file identity matched at inspection time')
        if item.get('windows_parent_chain_comparison') == 'matched_at_inspection':
            lines.append('             Windows parent identities matched; inspection handles released')
        comparison = item.get('tree_metadata_comparison')
        if comparison and comparison.get('matched'):
            lines.append('             Tree metadata matched; contents and concurrent changes remain unverified')
        fit = item.get('bin_fit')
        if fit and fit.get('fits') is False:
            lines.append('             ↳ Recycle Bin would not hold it: %s' % fit['reason'])
        elif fit and fit.get('verified_capable') is False:
            lines.append('             ↳ Volume not proven recycle-capable: %s'
                         % '; '.join(fit.get('issues') or ['unknown']))
        if item.get('verified') is False:
            lines.append('             RESTORED BUT NOT THE SAME OBJECT: ' + item['verification'])
        if item.get('detail') and item['result'] != 'dry_run':
            lines.append('             ↳ %s' % item['detail'])
    if report['volumes']:
        lines.append('')
        for vol in report['volumes']:
            if vol['free_after'] is None:
                lines.append('  %s free: %s' % (vol['drive'], vol['free_before']))
            else:
                lines.append('  %s free: %s -> %s'
                             % (vol['drive'], vol['free_before'], vol['free_after']))
    for budget in report.get('capacity') or []:
        if budget['capacity_bytes'] is None:
            lines.append('  %s Recycle Bin capacity: unknown' % budget['drive'])
        elif budget['used_bytes'] is None:
            lines.append('  %s Recycle Bin: capacity %d MiB, current usage unknown (%s)'
                         % (budget['drive'], budget['capacity_bytes'] // (1024 * 1024),
                            '; '.join(budget['issues']) or 'unreadable'))
        else:
            lines.append('  %s Recycle Bin: %d MiB of %d MiB used, %d MiB left for this batch'
                         % (budget['drive'], budget['used_bytes'] // (1024 * 1024),
                            budget['capacity_bytes'] // (1024 * 1024),
                            (budget['capacity_bytes'] - budget['used_bytes']) // (1024 * 1024)))
    if report['recycle_bin_note']:
        lines.append('')
        lines.append('note: ' + report['recycle_bin_note'])
    if report['audit_log']:
        lines.append('audit: ' + report['audit_log'])
    if report.get('audit_warning'):
        lines.append('WARNING: ' + report['audit_warning'])
    if report.get('aborted_after'):
        lines.append('ABORTED: a permanent deletion was observed at '
                     + report['aborted_after'] + '; nothing after it was executed')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Plan-gated cleanup executor (dry-run by default, Recycle Bin only)')
    parser.add_argument('--plan', help='plan JSON produced by the agent')
    parser.add_argument('--path', help='single target (requires --confirm-path)')
    parser.add_argument('--confirm-path', help='must equal --path exactly')
    parser.add_argument('--risk', choices=['safe', 'caution'], default='caution')
    parser.add_argument('--reason', help='why this target is disposable')
    parser.add_argument('--go', action='store_true', help='execute (default is dry-run)')
    parser.add_argument('--undo', help='restore what an audit log says was trashed')
    parser.add_argument('--undo-last', action='store_true',
                        help='restore the most recent run')
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args(argv)

    needs_lock = bool(args.go or args.undo or args.undo_last)
    if needs_lock:
        locked, reason = acquire_execution_lock()
        if not locked:
            return die(2, reason)
    try:
        report = run(args)
    except GateError as exc:
        return die(2, str(exc))
    finally:
        if needs_lock:
            release_execution_lock()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    summary = report['summary']
    if summary['refused'] or summary['failed']:
        return 3
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.exit(main())
