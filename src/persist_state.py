#!/usr/bin/env python3
"""Best-effort state branch storage without changing the caller's checkout."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from detective import TestObservation, load_state, save_state, update_state


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def persist(mode, branch, state_file, window_size=200):
    if not branch:
        return
    worktree = None
    orphan_branches = []
    try:
        git("rev-parse", "--show-toplevel")
        git("check-ref-format", f"refs/heads/{branch}")
        if mode == "save" and os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            pr = event.get("pull_request", {})
            if pr.get("head", {}).get("repo", {}).get("full_name") != pr.get("base", {}).get("repo", {}).get("full_name"):
                print("::warning::State persistence skipped for a fork pull request.")
                return
        state_path = Path(state_file).resolve()
        if mode == "save" and not state_path.is_file():
            print("::warning::State persistence skipped: no updated state file.")
            return
        # A fixed branch-internal filename also supports absolute workspace paths.
        with tempfile.TemporaryDirectory(prefix="flaky-detective-") as tmp:
            worktree = str(Path(tmp) / "state")
            git("worktree", "add", "--detach", worktree, "HEAD")
            local = load_state(state_path) if mode == "save" else None
            for attempt in range(3):
                refs = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
                if refs:
                    git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
                    revision = git("rev-parse", "FETCH_HEAD")
                    git("checkout", "--detach", revision, cwd=worktree)
                else:
                    orphan = f"ftd-{os.getpid()}-{attempt}"
                    git("checkout", "--orphan", orphan, cwd=worktree)
                    orphan_branches.append(orphan)
                    git("rm", "-rf", "--ignore-unmatch", ".", cwd=worktree)
                stored = Path(worktree) / "state.json"
                if mode == "load":
                    if stored.is_file():
                        save_state(state_path, load_state(stored))
                    return
                merged = load_state(stored)
                observations = []
                for test_id, record in local["tests"].items():
                    for entry in record["history"]:
                        observations.append(TestObservation(
                            test_id=test_id, message=record.get("last_failure_message", ""),
                            **entry,
                        ))
                update_state(merged, observations, window_size=window_size)
                merged["job_flakes"] = local.get("job_flakes", [])
                save_state(stored, merged)
                git("add", "--", "state.json", cwd=worktree)
                if not git("diff", "--cached", "--name-only", cwd=worktree):
                    return
                git("-c", "user.name=github-actions[bot]", "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit", "-m", "Update flaky test state", cwd=worktree)
                try:
                    git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=worktree)
                    return
                except subprocess.CalledProcessError:
                    if attempt == 2:
                        raise
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        print(f"::warning::State {mode} skipped (repository unavailable or no permission; {type(exc).__name__}).")
    finally:
        if worktree:
            try:
                git("worktree", "remove", "--force", worktree)
            except subprocess.CalledProcessError:
                pass
        for orphan in orphan_branches:
            try:
                git("branch", "-D", orphan)
            except subprocess.CalledProcessError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["load", "save"])
    parser.add_argument("--branch", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--window-size", type=int, default=200)
    args = parser.parse_args()
    persist(args.mode, args.branch, args.state_file, args.window_size)
