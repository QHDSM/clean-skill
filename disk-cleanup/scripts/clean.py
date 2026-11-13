#!/usr/bin/env python3
"""Plan-gated cleanup executor for Windows.

Plan validation and dry-run only. Execution is disabled because the legacy Shell
API does not guarantee recycle-only behavior. Python 3.10+, standard library only.

Two modes:

  python scripts/clean.py --plan plan.json                 # dry-run: show what would happen
  python scripts/clean.py --plan plan.json --go            # execute
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
  5. Execution is disabled pending a verified recycle-only backend.
  6. Space inside the Recycle Bin is NOT reclaimed until it is emptied; the report
     says so instead of claiming the space was freed.
"""
import argparse
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
import tree_evidence
import windows_identity

SCHEMA = 'disk-cleanup-plan/v1'
ACTIONS = {'trash', 'keep', 'report'}
RISKS = {'safe', 'caution', 'never'}
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

    targets = plan.get('targets')
    if not isinstance(targets, list) or not targets:
        raise GateError('targets must be a non-empty array')
    if len(targets) > BATCH_LIMIT:
        raise GateError(f'targets exceed the batch limit of {BATCH_LIMIT}')

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

        git = evidence.get('git')
        if git is not None:
            if not isinstance(git, dict):
                raise GateError(f'{where}.evidence.git must be an object or null')
            verdict = git.get('verdict')
            if risk == 'safe' and verdict and verdict != 'SAFE_TO_CONSIDER':
                if not auth.get('git_acknowledged'):
                    raise GateError(
                        f'{where} records Git risk {verdict!r} but risk is "safe" and '
                        'authorization.git_acknowledged is not true')

    return plan, hashlib.sha256(raw).hexdigest()


def load_plan(path):
    return load_plan_with_digest(path)[0]


def _has_drive(value):
    return '\x00' not in value and bool(re.match(r'^[a-zA-Z]:[\\/]', value)) and len(value) >= 3


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

def trash_one(path):
    """Refuse execution until a backend guarantees recycle-only behavior."""
    return False, 'execution disabled: recycle-only behavior is not guaranteed'


def volume_free(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0] + '\\'
    try:
        return shutil.disk_usage(drive).free
    except OSError:
        return None


# --------------------------------------------------------------------------- audit

def write_audit(plan_path, records, plan_digest):
    """Append one JSONL record per action. Paths stay local; never uploaded."""
    directory = Path.home() / '.disk-cleanup'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / f'audit-{time.strftime("%Y%m%d")}.jsonl'
        with log.open('a', encoding='utf-8') as handle:
            for record in records:
                handle.write(json.dumps({
                    'ts': int(time.time()),
                    'plan': plan_path,
                    'plan_digest': plan_digest,
                    **record,
                }, ensure_ascii=False) + '\n')
        return str(log)
    except OSError:
        return None


# ---------------------------------------------------------------------------- main

def run(args):
    if args.go:
        raise GateError('execution disabled: recycle-only behavior is not guaranteed')
    if not args.plan and not args.path:
        raise GateError('either --plan or --path is required')

    records, results = [], []
    refused, skipped = [], []
    before, after = {}, {}

    if args.plan:
        plan, digest = load_plan_with_digest(args.plan)
        targets = plan['targets']
    else:
        # Single-path mode: double entry proves the operator knows the exact target.
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
            'evidence': {'fingerprint': list(scan.fingerprint(st))},
        }]
        # Single-path mode still passes the same preflight as plan mode.
        for target in targets:
            denied = is_denied(Path(target['path']))
            if denied:
                raise GateError(f'refused: {denied}')

    for target in targets:
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
        entry['fingerprint'] = target['evidence']['fingerprint']
        entry['git_discovery'] = git_discovery.discover(path)
        entry['directory_contents_verified'] = False if os.path.isdir(path) else None

        if not args.go:
            entry.update(result='dry_run', detail='preview only; execution disabled')
            results.append(entry)
            continue

        key = os.path.splitdrive(os.path.abspath(path))[0].upper()
        if key not in before:
            before[key] = volume_free(path)
        moved, detail = trash_one(path)
        entry.update(result='trashed' if moved else 'failed', detail=detail)
        if moved:
            records.append({k: entry.get(k) for k in
                            ('path', 'action', 'risk', 'result', 'detail', 'size_bytes')})
        else:
            refused.append(entry)
        results.append(entry)

    if args.go:
        for target in targets:
            key = os.path.splitdrive(os.path.abspath(target['path']))[0].upper()
            if key in before and key not in after:
                after[key] = volume_free(target['path'])

    log_path = write_audit(args.plan, records, digest) if (args.go and records) else None

    trashed = [r for r in results if r['result'] == 'trashed']
    freed = sum(r.get('size_bytes') or 0 for r in trashed)
    return {
        'mode': 'EXECUTED' if args.go else 'DRY-RUN',
        'plan': args.plan,
        'plan_digest': digest,
        'execution_available': False,
        'preview_warning': 'Directory contents and Git risks are not fully verified.',
        'summary': {
            'total': len(results),
            'trashed': len(trashed),
            'dry_run': len([r for r in results if r['result'] == 'dry_run']),
            'skipped': len(skipped),
            'refused': len(refused),
            'not_executed': len([r for r in results if r['result'] == 'not_executed']),
            'failed': len([r for r in results if r['result'] == 'failed']),
            'moved_bytes': freed,
        },
        'volumes': [{'drive': d, 'free_before': before[d], 'free_after': after.get(d)}
                    for d in sorted(before)],
        'recycle_bin_note': (
            'Moved items sit in the Recycle Bin. Space is NOT reclaimed until it is '
            'emptied; the volume figures above may show no change.' if trashed else None),
        'audit_log': log_path,
        'results': results,
    }


def render(report):
    lines = [f"=== {report['mode']} ===", f"plan: {report['plan'] or '(single path)'}"]
    lines.append('Execution disabled. ' + report['preview_warning'])
    summary = report['summary']
    lines.append(
        'total %d | trashed %d | dry-run %d | skipped %d | refused %d | failed %d'
        % (summary['total'], summary['trashed'], summary['dry_run'],
           summary['skipped'], summary['refused'], summary['failed']))
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
        comparison = item.get('tree_metadata_comparison')
        if comparison and comparison.get('matched'):
            lines.append('             Tree metadata matched; contents and concurrent changes remain unverified')
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
    if report['recycle_bin_note']:
        lines.append('')
        lines.append('note: ' + report['recycle_bin_note'])
    if report['audit_log']:
        lines.append('audit: ' + report['audit_log'])
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
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    args = parser.parse_args(argv)

    try:
        report = run(args)
    except GateError as exc:
        return die(2, str(exc))

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
