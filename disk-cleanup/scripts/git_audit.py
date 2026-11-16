#!/usr/bin/env python3
"""只读 Git 风险审计：判断一个仓库是否可能含有"仅存在于本地"的数据。

只调用 git 只读子命令，不写入任何仓库状态。
输出 JSON；退出码 0 = 完成，2 = 部分失败，3 = 非 Git 目录。

判定口径（与 std-microblock/windows-disk-cleaner-skill 的 GitAudit 对齐，
但用纯 git CLI 实现，不依赖 libgit2）：
  - local_only_refs 非空  -> 存在未被远端证明覆盖的本地引用
  - 有未推送提交          -> tracked_history_covered = False
  - 有暂存/修改/冲突      -> 工作区有仅本地状态
  - 有未跟踪文件          -> 未进 Git
  - 有 stash              -> 普通 push 不保护 stash
  - ignored 数量          -> ignore 不等于可丢弃
每个结论都给出证据来源；不能判定的写进 unknown，不猜测。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

GIT_TIMEOUT = 30


def run_git(repo: str, args: list[str]) -> tuple[int, str, str]:
    """返回 (returncode, stdout, stderr)。失败不抛异常。"""
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after %ds" % GIT_TIMEOUT


def discover(start: str) -> tuple[str | None, str | None]:
    """返回 (仓库根, 错误说明)。裸仓库没有工作树，用 git 目录本身作根。"""
    code, out, err = run_git(start, ["rev-parse", "--show-toplevel"])
    if code == 0:
        return out.strip(), None
    bare_code, bare_out, _ = run_git(start, ["rev-parse", "--is-bare-repository"])
    if bare_code == 0 and bare_out.strip() == "true":
        dir_code, dir_out, _ = run_git(start, ["rev-parse", "--absolute-git-dir"])
        if dir_code == 0 and dir_out.strip():
            return dir_out.strip(), None
    return None, (err.strip() or out.strip() or "not a git repository")


def repo_state(repo: str) -> dict:
    """仓库形态：git 目录、当前分支、是否 detached、是否裸仓库。"""
    state = {"error": None, "git_dir": None, "branch": None,
             "detached": False, "bare": False}
    code, out, _ = run_git(repo, ["rev-parse", "--absolute-git-dir"])
    if code == 0:
        state["git_dir"] = out.strip() or None
    code, out, _ = run_git(repo, ["rev-parse", "--is-bare-repository"])
    if code == 0:
        state["bare"] = out.strip() == "true"
    code, out, err = run_git(repo, ["symbolic-ref", "-q", "--short", "HEAD"])
    if code == 0:
        state["branch"] = out.strip() or None
    elif code == 1:
        state["detached"] = True
    else:
        state["error"] = err.strip() or "symbolic-ref failed"
    return state


def coverage_audit(repo: str, has_remotes: bool) -> dict:
    """统计本地 refs 上、但不在任何 remote-tracking ref 上的提交数。

    这是对"本地历史是否已被远端覆盖"的直接计数，不依赖分支上游配置；
    remote-tracking ref 本身可能是缓存的，所以它证明的是"与本地记录的远端一致"。
    """
    if not has_remotes:
        return {"error": None, "local_commits_not_on_a_remote": None,
                "note": "no remotes configured; nothing can be proven covered"}
    code, out, err = run_git(repo, ["rev-list", "--count", "--branches", "--tags",
                                    "--not", "--remotes"])
    if code != 0:
        return {"error": err.strip() or "rev-list failed",
                "local_commits_not_on_a_remote": None}
    try:
        return {"error": None, "local_commits_not_on_a_remote": int(out.strip())}
    except ValueError:
        return {"error": "unparsable rev-list count: %r" % out.strip(),
                "local_commits_not_on_a_remote": None}


def porcelain_audit(repo: str) -> dict:
    """工作区状态计数。用一次 porcelain 调用拿全部状态。"""
    code, out, err = run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all",
                                    "--ignored=matching"])
    if code != 0:
        return {"error": err.strip() or out.strip() or "status failed", "counts": {}}

    modified = staged = untracked = ignored = conflicts = 0
    for line in out.splitlines():
        if not line:
            continue
        x, y = line[0], line[1]
        if x == "?" and y == "?":
            untracked += 1
            continue
        if x == "!" and y == "!":
            ignored += 1
            continue
        if x + y in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
            conflicts += 1
            continue
        if x not in " ?":
            staged += 1
        if y not in " ?":
            modified += 1
    return {
        "error": None,
        "counts": {
            "modified": modified, "staged": staged, "untracked": untracked,
            "ignored": ignored, "conflicts": conflicts,
        },
    }


def stash_count(repo: str) -> tuple[int, str | None]:
    code, out, err = run_git(repo, ["stash", "list"])
    if code != 0:
        return 0, err.strip() or "stash list failed"
    return len([l for l in out.splitlines() if l.strip()]), None


def upstream_audit(repo: str) -> dict:
    """本地分支相对上游的领先/落后情况（基于本地缓存的 remote-tracking ref）。"""
    code, out, err = run_git(repo, ["for-each-ref",
                                    "--format=%(refname:short)|%(upstream:short)|%(upstream:track)",
                                    "refs/heads/"])
    if code != 0:
        return {"error": err.strip() or out.strip() or "for-each-ref failed", "branches": []}

    branches, local_only, unpushed, unknown = [], [], 0, []
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, upstream, track = (line.split("|") + ["", ""])[:3]
        ahead = behind = 0
        if track == '[gone]':
            unknown.append("branch:%s upstream is gone" % name)
            local_only.append("branch:%s (upstream gone)" % name)
        elif track:
            if not re.fullmatch(r'\[(?:ahead \d+|behind \d+)(?:, (?:ahead \d+|behind \d+))?\]', track):
                unknown.append("branch:%s unrecognized tracking state %r" % (name, track))
            else:
                ahead_match = re.search(r'\bahead (\d+)', track)
                behind_match = re.search(r'\bbehind (\d+)', track)
                ahead = int(ahead_match.group(1)) if ahead_match else 0
                behind = int(behind_match.group(1)) if behind_match else 0
        entry = {"branch": name, "upstream": upstream or None,
                 "ahead": ahead, "behind": behind}
        branches.append(entry)
        if not upstream:
            local_only.append("branch:%s (no upstream)" % name)
        elif ahead > 0:
            unpushed += ahead
            local_only.append("branch:%s (ahead %d, not pushed)" % (name, ahead))
    return {"error": None, "branches": branches, "local_only_refs": local_only,
            "unpushed_commit_count": unpushed, "unknown": unknown}


def remote_state(repo: str) -> dict:
    code, out, err = run_git(repo, ["remote", "-v"])
    if code != 0:
        return {"error": err.strip() or "remote -v failed", "remotes": []}
    remotes = sorted({l.split()[0] for l in out.splitlines() if l.strip()})
    return {"error": None, "remotes": remotes,
            "verified": False,
            "note": "remote-tracking refs are CACHED, not proof of current remote "
                    "contents; run 'git fetch' to verify"}


def worktree_state(repo: str) -> dict:
    code, out, err = run_git(repo, ["worktree", "list", "--porcelain"])
    if code != 0:
        return {"error": err.strip() or "worktree list failed", "linked_worktrees": 0}
    n = max(0, len([l for l in out.splitlines() if l.startswith("worktree ")]) - 1)
    return {"error": None, "linked_worktrees": n}


def audit(path: str) -> dict:
    repo, err = discover(path)
    if repo is None:
        return {"is_git_repo": False, "error": err}

    state = repo_state(repo)
    bare = state["bare"]
    if bare:
        st = {"error": None, "counts": {}}
        stashes, stash_err = 0, None
    else:
        st = porcelain_audit(repo)
        stashes, stash_err = stash_count(repo)
    counts = st.get("counts", {})
    up = upstream_audit(repo)
    rem = remote_state(repo)
    wt = worktree_state(repo)
    cover = coverage_audit(repo, bool(rem.get("remotes")))

    risks, unknown, caveats = [], [], []

    if st.get("error"):
        unknown.append("worktree status unknown: %s" % st["error"])
    if stash_err:
        unknown.append("stash status unknown: %s" % stash_err)
    if up.get("error"):
        unknown.append("branch tracking unknown: %s" % up["error"])
    unknown.extend(up.get("unknown", []))
    if rem.get("error"):
        unknown.append("remote configuration unknown: %s" % rem["error"])
    if wt.get("error"):
        unknown.append("linked worktree status unknown: %s" % wt["error"])
    if state["error"]:
        unknown.append("HEAD state unknown: %s" % state["error"])
    if state["detached"]:
        unknown.append("detached HEAD: the checked-out commit may not be on any branch")
    if cover.get("error"):
        unknown.append("commit coverage unknown: %s" % cover["error"])
    caveats.append("local tags, reflogs and unreachable commits are not audited")
    if rem.get("remotes"):
        caveats.append(rem["note"])
    if bare:
        risks.append("bare repository: no work tree, its refs and objects exist only here")

    modified = counts.get("modified", 0)
    staged = counts.get("staged", 0)
    conflicts = counts.get("conflicts", 0)
    untracked = counts.get("untracked", 0)
    ignored = counts.get("ignored", 0)

    if modified or staged or conflicts:
        risks.append("tracked changes/conflicts exist only locally")
    if untracked:
        risks.append("untracked worktree entries are not in Git")
    if ignored:
        risks.append("ignored entries are LOCAL-ONLY too; ignore does not mean disposable")
    if stashes:
        risks.append("local stashes are not protected by ordinary push")
    if wt.get("linked_worktrees"):
        risks.append("%d linked worktree(s); their private state is not covered by this report"
                     % wt["linked_worktrees"])
    if not rem.get("remotes") and not rem.get("error"):
        risks.append("no remote configured")

    local_only = list(up.get("local_only_refs", []))
    if local_only:
        risks.append("local refs/commits are not proven covered by advertised remotes")
    not_on_remote = cover.get("local_commits_not_on_a_remote")
    if not_on_remote:
        risks.append("%d commit(s) reachable from local refs are not on any remote-tracking ref"
                     % not_on_remote)

    tracked_history_covered = (
        bool(rem.get("remotes"))
        and cover.get("error") is None
        and not_on_remote == 0
        and up.get("error") is None
        and not state["detached"]
    )

    all_content_synced = (
        tracked_history_covered and not bare
        and modified == 0 and staged == 0 and untracked == 0
        and ignored == 0 and conflicts == 0 and stashes == 0
        and not risks and not unknown
    )

    return {
        "is_git_repo": True,
        "repo_root": repo,
        "git_dir": state["git_dir"],
        "branch": state["branch"],
        "detached": state["detached"],
        "bare": bare,
        "remote_verified": False,
        "checked_unix": int(time.time()),
        "counts": counts,
        "stashes": stashes,
        "remotes": rem.get("remotes", []),
        "branches": up.get("branches", []),
        "unpushed_commit_count": up.get("unpushed_commit_count", 0),
        "local_commits_not_on_a_remote": not_on_remote,
        "local_only_refs": local_only,
        "linked_worktrees": wt.get("linked_worktrees", 0),
        "risks": risks,
        "unknown": unknown,
        "caveats": caveats,
        "tracked_history_covered": tracked_history_covered,
        "all_content_synced": all_content_synced,
        "needs_confirmation": not all_content_synced,
        "verdict": ("SAFE_TO_CONSIDER" if all_content_synced
                    else "LOCAL_ONLY_DATA_PRESENT" if local_only or up.get("unpushed_commit_count", 0)
                    or not_on_remote
                    else "LOCAL_MODIFICATIONS" if (modified or staged or conflicts)
                    else "UNVERIFIED"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Git risk audit")
    ap.add_argument("path", help="repository or a path inside it")
    ap.add_argument("--format", choices=["json", "text"], default="json")
    args = ap.parse_args()

    if not os.path.isdir(args.path):
        print(json.dumps({"is_git_repo": False, "error": "path not found"}), file=sys.stderr)
        return 2

    result = audit(args.path)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result.get("is_git_repo"):
            print("not a git repository: %s" % result.get("error"))
        else:
            print("verdict: %s" % result.get("verdict"))
            print("repo:    %s" % result.get("repo_root"))
            print("git_dir: %s | branch: %s | bare: %s | detached: %s"
                  % (result.get("git_dir"), result.get("branch"),
                     result.get("bare"), result.get("detached")))
            print("commits not on a remote-tracking ref: %s"
                  % result.get("local_commits_not_on_a_remote"))
            for k, v in (result.get("counts") or {}).items():
                print("  %-10s %s" % (k, v))
            print("  stashes    %s" % result.get("stashes"))
            for r in result.get("risks", []):
                print("RISK: %s" % r)
            for u in result.get("unknown", []):
                print("UNKNOWN: %s" % u)
            for c in result.get("caveats", []):
                print("CAVEAT: %s" % c)

    if not result.get("is_git_repo"):
        return 3
    if result.get("unknown"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
