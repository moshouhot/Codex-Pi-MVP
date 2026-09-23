from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pi_delegate import core


class ResolveTaskTests(unittest.TestCase):
    def test_resolve_task_inside_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task = root / ".ai" / "pi" / "runs" / "abc-001" / "TASK.md"
            task.parent.mkdir(parents=True)
            task.write_text("task", encoding="utf-8")
            resolved, task_id, run_dir = core.resolve_task(root, task)
            self.assertEqual(task_id, "abc-001")
            self.assertEqual(resolved, task.resolve())
            self.assertEqual(run_dir, task.parent.resolve())

    def test_rejects_task_outside_project(self):
        with tempfile.TemporaryDirectory() as project_td, tempfile.TemporaryDirectory() as other_td:
            root = Path(project_td)
            task = Path(other_td) / "abc" / "TASK.md"
            task.parent.mkdir()
            task.write_text("task", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside project root"):
                core.resolve_task(root, task)


class RunnerContractTests(unittest.TestCase):
    def test_stdin_is_devnull_and_completed_contract_is_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "runs" / "case-001"
            run_dir.mkdir(parents=True)
            task = run_dir / "TASK.md"
            task.write_text("task", encoding="utf-8")
            fake_pi = root / "cli.js"
            fake_pi.write_text("// fake", encoding="utf-8")

            def fake_run(*args, **kwargs):
                self.assertIs(kwargs["stdin"], core.subprocess.DEVNULL)
                (run_dir / "RESULT.json").write_text(
                    json.dumps({"task_id": "case-001", "status": "completed"}),
                    encoding="utf-8",
                )
                (run_dir / "REPORT.md").write_text("ok", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="done", stderr="")

            with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
                core.shutil, "which", return_value="node"
            ), mock.patch.object(core.subprocess, "run", side_effect=fake_run):
                code, result = core.run_task(project_root=root, task_file=task)

            self.assertEqual(code, 0)
            self.assertEqual(result["runner_status"], "COMPLETED")
            self.assertEqual(core.read_json(run_dir / "RUN_STATE.json")["status"], "COMPLETED")

    def test_timeout_is_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "runs" / "case-002"
            run_dir.mkdir(parents=True)
            task = run_dir / "TASK.md"
            task.write_text("task", encoding="utf-8")
            fake_pi = root / "cli.js"
            fake_pi.write_text("// fake", encoding="utf-8")

            timeout = core.subprocess.TimeoutExpired(cmd="pi", timeout=1, output="partial", stderr="")
            with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
                core.shutil, "which", return_value="node"
            ), mock.patch.object(core.subprocess, "run", side_effect=timeout):
                code, result = core.run_task(project_root=root, task_file=task, timeout_seconds=1)

            self.assertEqual(code, 124)
            self.assertEqual(result["runner_status"], "TIMEOUT")
            self.assertEqual(core.read_json(run_dir / "RUN_STATE.json")["status"], "TIMEOUT")


class DoctorTests(unittest.TestCase):
    def test_ready_reason_code(self):
        fake_pi = Path("C:/fake/pi/cli.js")
        proc = mock.Mock(returncode=0, stdout="0.86.1\n", stderr="")
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(core.shutil, "which", return_value="node"), mock.patch.object(
            core.subprocess, "run", return_value=proc
        ):
            code, result = core.doctor()
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason_code"], "READY")
        self.assertFalse(result["reinstall_pi_delegate_recommended"])

    def test_node_missing_reason_code(self):
        with mock.patch.object(core.shutil, "which", return_value=None):
            code, result = core.doctor()
        self.assertEqual(code, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "NODE_NOT_FOUND")
        self.assertFalse(result["reinstall_pi_delegate_recommended"])

    def test_pi_cli_missing_reason_code(self):
        fake_pi = Path("C:/missing/pi/cli.js")
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=False
        ), mock.patch.object(core.shutil, "which", return_value="node"):
            code, result = core.doctor()
        self.assertEqual(code, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "PI_CLI_NOT_FOUND")

    def test_pi_version_failure_reason_code(self):
        fake_pi = Path("C:/fake/pi/cli.js")
        proc = mock.Mock(returncode=7, stdout="", stderr="boom")
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(core.shutil, "which", return_value="node"), mock.patch.object(
            core.subprocess, "run", return_value=proc
        ):
            code, result = core.doctor()
        self.assertEqual(code, 4)
        self.assertEqual(result["reason_code"], "PI_VERSION_FAILED")

    def test_pi_version_timeout_reason_code(self):
        fake_pi = Path("C:/fake/pi/cli.js")
        timeout = core.subprocess.TimeoutExpired(cmd="pi", timeout=10)
        with mock.patch.object(core, "discover_pi_cli", return_value=fake_pi), mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(core.shutil, "which", return_value="node"), mock.patch.object(
            core.subprocess, "run", side_effect=timeout
        ):
            code, result = core.doctor()
        self.assertEqual(code, 124)
        self.assertEqual(result["reason_code"], "PI_VERSION_TIMEOUT")


if __name__ == "__main__":
    unittest.main()

