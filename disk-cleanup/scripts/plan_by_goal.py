#!/usr/bin/env python3
"""Turn a capacity goal into a plan.json draft, or refuse to invent one.

The goal is a *move* goal, not a free-space goal. Everything this skill does
ends in the Recycle Bin, and the bin keeps the bytes until it is emptied, so a
plan can promise "20 GiB leaves its current location" and cannot promise "20 GiB
of free space appears". The report says both numbers and never conflates them.

Selection is deliberately dumb: largest first, stop when the goal is met, never
widen the scope, never lower the risk label to make the numbers work. If the
candidates cannot reach the goal, the shortfall is reported and the plan is
still written -- a partial plan the user can inspect beats a padded one.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clean
import git_discovery
import scan

SCHEMA_GOAL = 'disk-cleanup-goal/v1'
PLAN_SCHEMA = 'disk-cleanup-plan/v1'


def size_arg(text):
    return scan.size_arg(text)


def human(count):
    if count is None:
        return 'unknown'
    for unit, scale in (('TiB', 1 << 40), ('GiB', 1 << 30), ('MiB', 1 << 20), ('KiB', 1 << 10)):
        if abs(count) >= scale:
            return f'{count / scale:,.2f} {unit}'
    return f'{count} B'


def load_scan(path):
    with open(path, encoding='utf-8') as fh:
        report = json.load(fh)
    if report.get('schema_version') != 1:
        raise ValueError('scan report must have schema_version 1')
    return report


def threshold_deficit(volumes, percent):
    """Bytes that must leave each volume to reach the free-percent floor."""
    deficits = []
    for volume in volumes:
        total, free = volume.get('total_bytes'), volume.get('free_bytes')
        if not total or free is None:
            continue
        floor = int(total * percent / 100.0)
        deficits.append({'volume': volume['path'], 'total_bytes': total,
                         'free_bytes': free, 'floor_bytes': floor,
                         'deficit_bytes': max(0, floor - free)})
    if not deficits:
        raise ValueError('the scan report has no volume capacity to compare against')
    return deficits


GIT_AUDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'git_audit.py')


def worktree_root(path):
    """Nearest ancestor (or the path itself) that holds a .git marker."""
    current = Path(os.path.abspath(path))
    for parent in (current, *current.parents):
        if os.path.lexists(os.path.join(str(parent), '.git')):
            return str(parent)
    return None


def _audit(root, cache):
    if root in cache:
        return cache[root]
    import subprocess
    try:
        proc = subprocess.run([sys.executable, GIT_AUDIT, root, '--format', 'json'],
                              capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        cache[root] = (None, str(exc)[:200])
        return cache[root]
    if proc.returncode != 0:
        cache[root] = (None, (proc.stderr.strip() or 'git_audit failed')[:200])
        return cache[root]
    try:
        cache[root] = (json.loads(proc.stdout), None)
    except json.JSONDecodeError as exc:
        cache[root] = (None, f'git_audit output was not JSON: {exc}')
    return cache[root]


def git_assessment(path, kind, *, run_audit, cache):
    """Decide whether a candidate can carry Git evidence the gate will accept."""
    probe = path if kind == 'directory' else os.path.dirname(path) or path
    found = git_discovery.discover(probe)
    if not found['complete']:
        # The gate refuses a target whose Git discovery is incomplete, so a draft
        # that proposes one is dead on arrival.
        detail = '; '.join(found['issues'][:2]) or 'limit reached'
        return f'Git discovery incomplete ({detail}): the preview would refuse this', None
    if not found['markers']:
        return None, None
    root = worktree_root(probe)
    if not run_audit:
        return ('Git marker found (%s): rerun with --git-audit, or run git_audit.py '
                'and add evidence.git yourself' % ', '.join(found['markers'][:2])), None
    if not root:
        return 'Git marker found but no worktree root could be identified', None
    audit, error = _audit(root, cache)
    if error:
        return f'git_audit failed: {error}', None
    verdict = audit.get('verdict')
    if verdict != 'SAFE_TO_CONSIDER':
        return (f'Git verdict {verdict}: local-only data may be lost, so it needs '
                'explicit confirmation rather than a draft'), None
    return None, audit


def candidates(report, *, index, scope, run_audit=False, min_age_days=0, excluded=(), now=None):
    """Candidates from both top lists, each assessed before it can be chosen.

    Three things make a candidate unusable no matter how large it is: the gate
    refuses it, it *is* the scan root (picking the root would "solve" any goal by
    proposing the whole tree), or it sits in a Git worktree whose audit is
    missing or unfavourable. Those are reported as skipped with the reason,
    never dropped silently and never quietly re-labelled.
    """
    now = time.time() if now is None else now
    roots = {scan.normalized(root) for root in scope}
    audits = {}
    pool = []
    for key, kind in (('largest_directories', 'directory'), ('largest_files', 'file')):
        for row in report.get(key) or []:
            path = row.get('path')
            size = row.get('logical_bytes')
            fingerprint = row.get('fingerprint') or []
            if not path or not isinstance(size, int) or size <= 0:
                continue
            if len(fingerprint) < 5:
                continue
            if min_age_days:
                age_days = (now - fingerprint[3] / 1_000_000_000) / 86400
                if age_days < min_age_days:
                    continue
            if any(scan.within(path, root) or scan.within(root, path) for root in excluded):
                continue
            risk, reason = classify({'path': path}, index)
            fatal = None
            if scan.normalized(path) in roots:
                fatal = 'this is the scan root; choose a subdirectory instead'
            elif clean.is_denied(path):
                fatal = 'refused by clean.py: ' + clean.is_denied(path)
            git_evidence = None
            if not fatal:
                fatal, git_evidence = git_assessment(path, kind, run_audit=run_audit, cache=audits)
            pool.append({'path': path, 'bytes': size, 'kind': kind,
                         'fingerprint': fingerprint, 'risk': risk, 'reason': reason,
                         'fatal': fatal, 'git': git_evidence})
    # Verified caches first, then whatever is largest. Size alone would always
    # put an unclassified directory ahead of a known cache.
    pool.sort(key=lambda item: (0 if item['risk'] == 'safe' else 1, -item['bytes'], item['path']))
    return pool


def pick(pool, required, max_targets):
    """Walk the ranked pool until the goal is met; never lower a risk label."""
    chosen, skipped = [], []
    for item in pool:
        if len(chosen) >= max_targets:
            skipped.append(dict(item, why=f'batch limit {max_targets} reached'))
            continue
        if item['fatal']:
            skipped.append(dict(item, why=item['fatal']))
            continue
        nested = next((other for other in chosen
                       if scan.within(item['path'], other['path'])
                       or scan.within(other['path'], item['path'])), None)
        if nested:
            skipped.append(dict(item, why='nests with an already chosen target'))
            continue
        chosen.append(item)
        if sum(entry['bytes'] for entry in chosen) >= required:
            break
    return chosen, skipped


def registry_index(path):
    """Map known cache roots to (risk, reason) so a verified cache keeps its label."""
    if not path or not os.path.exists(path):
        return []
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import targets
    registry = targets.load(path)
    index = []
    for cleaner in registry['cleaners']:
        if cleaner['status'] != 'release' or cleaner['risk'] == 'never':
            continue
        for expanded in targets.expand(cleaner)[0]:
            index.append({'root': expanded, 'risk': cleaner['risk'],
                          'reason': f"{cleaner['label']}：{cleaner.get('regenerates') or cleaner['description']}"})
    return index


def classify(candidate, index):
    for entry in index:
        if scan.within(candidate['path'], entry['root']):
            return entry['risk'], entry['reason']
    return 'caution', '按逻辑体积排序的候选，用途与可重建性尚未核实'


def build_plan(report, chosen, index, *, goal, scope, excluded, note):
    rows = []
    for item in chosen:
        evidence = {'fingerprint': item['fingerprint']}
        if item['kind'] == 'file':
            evidence['size_bytes'] = item['bytes']  # directories have no meaningful own size
        if item.get('git'):
            evidence['git'] = item['git']
        rows.append({'path': item['path'], 'action': 'trash', 'risk': item['risk'],
                     'reason': item['reason'], 'evidence': evidence})
    return {'schema': PLAN_SCHEMA, 'generated_unix': int(time.time()),
            'authorization': {'scope': list(scope), 'excluded': list(excluded)},
            'goal': goal, 'targets': rows, 'note': note}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Draft a plan.json from a capacity goal.')
    parser.add_argument('--scan', required=True, help='scan.py --format json output')
    parser.add_argument('--target', help='bytes that must leave their current location, e.g. 20GiB')
    parser.add_argument('--free-below', type=float,
                        help='trigger when free space is under this percent of the volume')
    parser.add_argument('--min-age-days', type=float, default=30)
    parser.add_argument('--max-targets', type=int, default=clean.BATCH_LIMIT)
    parser.add_argument('--registry', help='references/targets.json, to keep verified caches labelled safe')
    parser.add_argument('--git-audit', action='store_true',
                        help='run git_audit.py for candidates inside a worktree; only SAFE_TO_CONSIDER is drafted')
    parser.add_argument('--out', default='plan.json')
    parser.add_argument('--exclude', action='append', default=[])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if not args.target and args.free_below is None:
        print(json.dumps({'ok': False, 'error':
                          'a goal is required: --target 20GiB or --free-below 15. '
                          'This tool will not invent a target.'}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.max_targets > clean.BATCH_LIMIT:
        print(json.dumps({'ok': False, 'error': f'--max-targets cannot exceed {clean.BATCH_LIMIT}'},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    try:
        report = load_scan(args.scan)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    volumes = report.get('volumes') or []
    scope = [volume['path'] for volume in volumes]
    if not scope:
        print(json.dumps({'ok': False, 'error': 'the scan report has no volumes to scope a plan to'},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    requested = 0
    basis = []
    if args.target:
        try:
            requested = max(requested, size_arg(args.target))
        except (ValueError, argparse.ArgumentTypeError) as exc:
            print(json.dumps({'ok': False, 'error': f'--target: {exc}'}, ensure_ascii=False),
                  file=sys.stderr)
            return 2
        basis.append(f'requested move: {args.target}')
    if args.free_below is not None:
        try:
            deficits = threshold_deficit(volumes, args.free_below)
        except ValueError as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        worst = max(deficits, key=lambda item: item['deficit_bytes'])
        requested = max(requested, worst['deficit_bytes'])
        basis.append(f"free-below {args.free_below}%: {worst['volume']} has "
                     f"{human(worst['free_bytes'])} free of {human(worst['total_bytes'])}, "
                     f"needs {human(worst['deficit_bytes'])} moved out")

    index = registry_index(args.registry) if args.registry else []
    pool = candidates(report, index=index, scope=scope, run_audit=args.git_audit,
                      min_age_days=args.min_age_days, excluded=args.exclude)
    chosen, skipped = pick(pool, requested, args.max_targets)

    estimated = sum(item['bytes'] for item in chosen)
    goal = {'schema': SCHEMA_GOAL, 'kind': 'move_bytes',
            'requested_bytes': requested, 'estimated_bytes': estimated,
            'shortfall_bytes': max(0, requested - estimated),
            'met': estimated >= requested,
            'basis': '; '.join(basis),
            'policy': {'min_age_days': args.min_age_days,
                       'max_targets': args.max_targets,
                       'registry': os.path.basename(args.registry) if args.registry else None},
            'caveats': [
                'A move goal, not a free-space goal: the bytes sit in the Recycle Bin until it is emptied.',
                'estimated_bytes is scan-time logical size; it is not measured release and not allocated size.',
                'Directory entries carry no evidence.size_bytes; their logical size cannot be a self-fingerprint.',
            ]}
    plan = build_plan(report, chosen, index, goal=goal, scope=scope,
                      excluded=args.exclude,
                      note='Draft written by plan_by_goal.py; review it with clean.py --plan before --go.')

    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)
        fh.write('\n')

    result = {'ok': True, 'plan': args.out, 'goal': goal,
              'targets': len(chosen), 'skipped': len(skipped),
              'top_skipped': skipped[:5],
              'scan_basis': {'roots': report.get('roots'), 'complete': report.get('complete'),
                             'concurrent_dirs': (report.get('concurrent_change') or {}).get('directories_changed')}}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"goal       : move {human(requested)}")
        print(f"basis      : {goal['basis']}")
        print(f"selected   : {len(chosen)} target(s), {human(estimated)} at scan time")
        if goal['met']:
            print('status     : goal reached on paper')
        else:
            print(f"status     : SHORT by {human(goal['shortfall_bytes'])} -- "
                  'do not widen the scope to hide it; report the gap and ask')
        print(f"plan       : {args.out}")
        print(f"next       : python clean.py --plan {args.out}")
        if not chosen and skipped:
            print('why nothing was picked:')
            for entry in skipped[:5]:
                print(f"  - {entry['path']}: {entry['why']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
