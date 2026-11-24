"""ci/tick_mode.sh: tick.yml captures for real only when VM_CAPTURE is exactly "tick" and no run of capture.yml is
queued or in progress; if the runs cannot be listed it captures. Driven through a stub `gh` on PATH."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci" / "tick_mode.sh"

FAKE_GH = """#!/bin/bash
echo "$@" >> "$FAKE_GH_LOG"
[ -n "${FAKE_GH_OUT:-}" ] && echo "$FAKE_GH_OUT"
exit "${FAKE_GH_RC:-0}"
"""


class TickMode(unittest.TestCase):
    def decide(self, capture_var, gh_out="", gh_rc=0):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            gh = os.path.join(bin_dir, "gh")
            with open(gh, "w") as fh:
                fh.write(FAKE_GH)
            os.chmod(gh, 0o755)
            output, gh_log = os.path.join(tmp, "output"), os.path.join(tmp, "gh.log")
            env = {"PATH": bin_dir + os.pathsep + "/usr/bin:/bin", "GITHUB_REPOSITORY": "owner/repo",
                   "GITHUB_OUTPUT": output, "FAKE_GH_LOG": gh_log, "FAKE_GH_OUT": gh_out, "FAKE_GH_RC": str(gh_rc)}
            if capture_var is not None:
                env["VM_CAPTURE"] = capture_var
            proc = subprocess.run(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  universal_newlines=True)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            with open(output) as fh:
                shadow = fh.read().strip()
            calls = []
            if os.path.exists(gh_log):
                with open(gh_log) as fh:
                    calls = fh.read().splitlines()
        return shadow, calls, proc.stdout

    def test_shadow_unless_the_variable_is_exactly_tick_and_gh_is_not_asked(self):
        for value in (None, "", "Tick", "tick ", "loop", "on"):
            shadow, calls, _ = self.decide(value, gh_out="0")
            self.assertEqual((shadow, calls), ("shadow=1", []), repr(value))

    def test_real_when_no_capture_run_is_active(self):
        shadow, calls, out = self.decide("tick", gh_out="0")
        self.assertEqual(shadow, "shadow=0")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("api repos/owner/repo/actions/workflows/capture.yml/runs?per_page=40 --jq "),
                        calls[0])
        self.assertIn("capturing", out)

    def test_shadow_while_the_loop_is_queued_or_running(self):
        shadow, _, out = self.decide("tick", gh_out="2")
        self.assertEqual(shadow, "shadow=1")
        self.assertIn("2 run(s) of capture.yml are queued or in progress: shadow run", out)

    def test_real_when_the_runs_cannot_be_listed(self):
        for out, rc in (("", 1), ('{"message":"Bad credentials"}', 1), ("", 0)):
            shadow, _, text = self.decide("tick", gh_out=out, gh_rc=rc)
            self.assertEqual(shadow, "shadow=0", (out, rc))
            self.assertIn("could not list the runs of capture.yml: capturing", text)


if __name__ == "__main__":
    unittest.main()
