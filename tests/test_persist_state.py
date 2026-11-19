import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import detective
import persist_state


class TestPersistence(unittest.TestCase):
    def test_local_bare_origin_roundtrip_merge_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin.git"
            repo = root / "checkout"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
            real_git = persist_state.git

            def git(*args, cwd=None):
                return real_git(*args, cwd=cwd or repo)

            with patch.object(persist_state, "git", side_effect=git), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}):
                (repo / "keep.txt").write_text("untouched")
                git("add", "keep.txt")
                git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
                git("push", "origin", "HEAD")
                head = git("rev-parse", "HEAD")
                state_file = root / "state.json"
                persist_state.persist("load", "state", str(state_file))
                self.assertFalse(state_file.exists())
                state = {"tests": {}}
                detective.update_state(state, [detective.TestObservation("first", "failed", run_id="1")])
                detective.save_state(state_file, state)
                persist_state.persist("save", "state", str(state_file))
                state_file.unlink()
                persist_state.persist("load", "state", str(state_file))
                self.assertEqual(len(detective.load_state(state_file)["tests"]["first"]["history"]), 1)
                stale = root / "stale.json"
                detective.save_state(stale, {"tests": {}})
                stale_state = detective.load_state(stale)
                detective.update_state(stale_state, [detective.TestObservation("second", "passed", run_id="2")])
                detective.save_state(stale, stale_state)
                pushes = 0

                def reject_once(*args, cwd=None):
                    nonlocal pushes
                    if args[0] == "push":
                        pushes += 1
                        if pushes == 1:
                            raise subprocess.CalledProcessError(1, "git push")
                    return git(*args, cwd=cwd)

                with patch.object(persist_state, "git", side_effect=reject_once):
                    persist_state.persist("save", "state", str(stale))
                self.assertEqual(pushes, 2)
                persist_state.persist("load", "state", str(state_file))
                self.assertEqual(set(detective.load_state(state_file)["tests"]), {"first", "second"})
                self.assertEqual(git("rev-parse", "HEAD"), head)
                self.assertEqual(git("status", "--porcelain"), "")
                self.assertEqual((repo / "keep.txt").read_text(), "untouched")
                denied_state = detective.load_state(state_file)
                detective.update_state(denied_state, [detective.TestObservation("denied", "failed")])
                detective.save_state(state_file, denied_state)

                def deny_push(*args, cwd=None):
                    if args[0] == "push":
                        raise subprocess.CalledProcessError(1, "git push")
                    return git(*args, cwd=cwd)

                with patch.object(persist_state, "git", side_effect=deny_push), contextlib.redirect_stdout(io.StringIO()) as output:
                    persist_state.persist("save", "state", str(state_file))
                self.assertIn("::warning::", output.getvalue())
                print("Persistence local test: bare origin create/load/update, stale-state merge, push retry, checkout unchanged: OK")

    def test_disabled_no_repo_and_no_permission(self):
        with patch.object(persist_state, "git") as git:
            persist_state.persist("save", "", "unused")
            git.assert_not_called()
        with patch.object(persist_state, "git", side_effect=subprocess.CalledProcessError(1, "git")), contextlib.redirect_stdout(io.StringIO()) as output:
            persist_state.persist("save", "state", "unused")
        self.assertIn("::warning::", output.getvalue())

    def test_fork_never_pushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": "fork/repo"}}, "base": {"repo": {"full_name": "owner/repo"}}}}))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event)}), patch.object(persist_state, "git") as git, contextlib.redirect_stdout(io.StringIO()) as output:
                persist_state.persist("save", "state", "unused")
            self.assertIn("fork pull request", output.getvalue())
            self.assertFalse(any(call.args[0] == "push" for call in git.call_args_list))
