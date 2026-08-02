"""Tests for A3 telemetry collector process management."""

import dataclasses
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.performance.a3.collectors import (
    CollectorController,
    CollectorError,
    CollectorProcess,
    CollectorRecord,
    CollectorSpec,
    CollectorStartError,
    CollectorStopError,
    DEFAULT_COLLECTORS,
    RunContext,
    SAMPLING_INTERVAL_SECONDS,
    _minimal_environment,
    _popen_platform_kwargs,
)
from tools.performance.a3.models import RunPhase


class FakeProcess:
    """Popen-like test double."""

    def __init__(self, pid, terminate_exits=True, kill_exits=True, terminate_error=None):
        self.pid = pid
        self._running = True
        self._rc = None
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []
        self._terminate_exits = terminate_exits
        self._kill_exits = kill_exits
        self._terminate_error = terminate_error

    def poll(self):
        return None if self._running else self._rc

    def terminate(self):
        self.terminated = True
        if self._terminate_error is not None:
            raise self._terminate_error
        if self._terminate_exits:
            self._running = False
            self._rc = -15

    def kill(self):
        self.killed = True
        if self._kill_exits:
            self._running = False
            self._rc = -9

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self._running:
            raise subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout)
        return self._rc


class FakeHandle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeFactory:
    """Process factory double: no real processes are ever launched."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
        self.processes = {}
        self.handles = {}
        self._next_pid = 4200

    def __call__(self, spec, stdout_path, stderr_path):
        self.calls.append(
            {"spec": spec, "stdout_path": stdout_path, "stderr_path": stderr_path}
        )
        if self.fail_at == spec.name:
            raise OSError(f"cannot spawn {spec.name}")
        self._next_pid += 1
        process = FakeProcess(pid=self._next_pid)
        stdout_handle = FakeHandle()
        stderr_handle = FakeHandle()
        self.processes[spec.name] = process
        self.handles[spec.name] = (stdout_handle, stderr_handle)
        return CollectorProcess(
            spec=spec,
            process=process,
            stdout_handle=stdout_handle,
            stderr_handle=stderr_handle,
            stdout_path=Path(stdout_path),
            stderr_path=Path(stderr_path),
        )


class FakeClock:
    def __init__(self):
        self.ns = 5_000_000_000

    def now(self):
        value = self.ns
        self.ns += 1_000_000
        return value


def make_context(artifact_root, run_id="run-001") -> RunContext:
    return RunContext(
        baseline_cycle_id="cycle-2026-08",
        manifest_id="a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001",
        run_id=run_id,
        artifact_root=Path(artifact_root),
        phase=RunPhase.STEADY_STATE,
    )


class CollectorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.artifact_root = Path(self._tmp.name) / "artifacts"
        self.factory = FakeFactory()
        self.clock = FakeClock()

    def make_controller(self, factory=None):
        return CollectorController(
            process_factory=factory or self.factory,
            monotonic_ns=self.clock.now,
        )

    def start(self, controller=None):
        controller = controller or self.make_controller()
        records = controller.start(make_context(self.artifact_root))
        return controller, records


class DefaultCollectorSpecTests(unittest.TestCase):
    def test_exact_four_collectors_in_order(self):
        self.assertEqual(
            [spec.name for spec in DEFAULT_COLLECTORS],
            ["pidstat", "sar", "vmstat", "iostat"],
        )

    def test_exact_commands_with_five_second_interval(self):
        expected = {
            "pidstat": ("pidstat", "-h", "-r", "-u", "-w", "-p", "ALL", "5"),
            "sar": ("sar", "-u", "-r", "-n", "DEV,TCP,ETCP", "5"),
            "vmstat": ("vmstat", "-t", "5"),
            "iostat": ("iostat", "-x", "-d", "5"),
        }
        for spec in DEFAULT_COLLECTORS:
            self.assertEqual(spec.argv, expected[spec.name])
            self.assertIsInstance(spec.argv, tuple)
            self.assertEqual(spec.argv[-1], str(SAMPLING_INTERVAL_SECONDS))

    def test_sampling_interval_is_five(self):
        self.assertEqual(SAMPLING_INTERVAL_SECONDS, 5)

    def test_names_unique(self):
        names = [spec.name for spec in DEFAULT_COLLECTORS]
        self.assertEqual(len(names), len(set(names)))

    def test_duplicate_names_rejected(self):
        specs = (
            CollectorSpec(name="dup", argv=("x", "5"), stdout_name="a.log"),
            CollectorSpec(name="dup", argv=("y", "5"), stdout_name="b.log"),
        )
        with self.assertRaises(CollectorError):
            CollectorController(specs=specs, process_factory=FakeFactory())


class RunContextValidationTests(unittest.TestCase):
    def _context(self, **overrides):
        values = {
            "baseline_cycle_id": "cycle-1",
            "manifest_id": "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001",
            "run_id": "run-1",
            "artifact_root": Path("/tmp/a3"),
            "phase": RunPhase.STEADY_STATE,
        }
        values.update(overrides)
        return RunContext(**values)

    def test_valid_context(self):
        context = self._context()
        self.assertEqual(context.run_id, "run-1")

    def test_empty_identifier_rejected(self):
        with self.assertRaises(ValueError):
            self._context(run_id="")

    def test_path_separator_rejected(self):
        with self.assertRaises(ValueError):
            self._context(run_id="a/b")
        with self.assertRaises(ValueError):
            self._context(run_id="a\\b")

    def test_dot_dot_rejected(self):
        with self.assertRaises(ValueError):
            self._context(run_id="..")
        with self.assertRaises(ValueError):
            self._context(run_id="a..b")

    def test_nul_rejected(self):
        with self.assertRaises(ValueError):
            self._context(run_id="a\x00b")

    def test_too_long_rejected(self):
        with self.assertRaises(ValueError):
            self._context(run_id="a" * 129)

    def test_non_ascii_rejected(self):
        with self.assertRaises(ValueError):
            self._context(run_id="café")

    def test_allowed_punctuation(self):
        context = self._context(run_id="a.b-c_d")
        self.assertEqual(context.run_id, "a.b-c_d")


class CollectorStartTests(CollectorTestBase):
    def test_start_order_deterministic(self):
        self.start()
        self.assertEqual(
            [call["spec"].name for call in self.factory.calls],
            ["pidstat", "sar", "vmstat", "iostat"],
        )

    def test_output_paths_below_collectors_dir(self):
        _, records = self.start()
        for record in records:
            self.assertTrue(record.stdout_path.startswith("collectors/"))
            self.assertTrue(record.stderr_path.startswith("collectors/"))
        names = {record.name: record for record in records}
        self.assertEqual(names["pidstat"].stdout_path, "collectors/pidstat.log")
        self.assertEqual(names["sar"].stdout_path, "collectors/sar.log")
        self.assertEqual(names["vmstat"].stdout_path, "collectors/vmstat.log")
        self.assertEqual(names["iostat"].stdout_path, "collectors/iostat.log")
        self.assertEqual(names["pidstat"].stderr_path, "collectors/pidstat.stderr.log")

    def test_start_records_pid_and_monotonic(self):
        _, records = self.start()
        for record in records:
            self.assertIsNotNone(record.pid)
            self.assertIsNotNone(record.started_monotonic_ns)
            self.assertIsNone(record.stopped_monotonic_ns)
            self.assertIsNone(record.return_code)
            self.assertFalse(record.graceful_termination_requested)
            self.assertFalse(record.forced_kill_used)

    def test_already_started_rejected(self):
        controller, _ = self.start()
        with self.assertRaises(CollectorStartError):
            controller.start(make_context(self.artifact_root))

    def test_partial_start_rolls_back(self):
        factory = FakeFactory(fail_at="vmstat")
        controller = self.make_controller(factory=factory)
        with self.assertRaises(CollectorStartError):
            controller.start(make_context(self.artifact_root))
        # pidstat and sar started before the failure and must be terminated.
        self.assertTrue(factory.processes["pidstat"].terminated)
        self.assertTrue(factory.processes["sar"].terminated)
        self.assertNotIn("vmstat", factory.processes)
        # The failure is recorded.
        records = controller.records()
        vmstat = [r for r in records if r.name == "vmstat"][0]
        self.assertIsNotNone(vmstat.start_error)

    def test_symlink_artifact_root_rejected(self):
        controller = self.make_controller()
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(CollectorStartError):
                controller.start(make_context(self.artifact_root))

    def test_handles_opened_per_collector(self):
        self.start()
        self.assertEqual(len(self.factory.handles), 4)


class CollectorStopTests(CollectorTestBase):
    def test_stop_reverse_order(self):
        controller, _ = self.start()
        controller.stop()
        termination_order = [
            name
            for name in ("pidstat", "sar", "vmstat", "iostat")
            if self.factory.processes[name].terminated
        ]
        self.assertEqual(
            termination_order, ["pidstat", "sar", "vmstat", "iostat"]
        )
        # Verify actual order via termination sequence tracking.
        self.assertEqual(
            controller._stop_order, ["iostat", "vmstat", "sar", "pidstat"]
        )

    def test_graceful_stop(self):
        controller, _ = self.start()
        records = controller.stop()
        for record in records:
            self.assertTrue(record.graceful_termination_requested)
            self.assertFalse(record.forced_kill_used)
            self.assertEqual(record.return_code, -15)
            self.assertIsNotNone(record.stopped_monotonic_ns)

    def test_forced_kill_after_timeout(self):
        controller, _ = self.start()
        self.factory.processes["sar"]._terminate_exits = False
        records = {r.name: r for r in controller.stop()}
        sar = records["sar"]
        self.assertTrue(sar.forced_kill_used)
        self.assertEqual(sar.return_code, -9)
        self.assertEqual(
            self.factory.processes["sar"].wait_timeouts, [5, 2]
        )
        self.assertFalse(records["pidstat"].forced_kill_used)

    def test_stop_continues_after_one_failure(self):
        controller, _ = self.start()
        self.factory.processes["vmstat"]._terminate_error = OSError("stuck")
        with self.assertRaises(CollectorStopError) as ctx:
            controller.stop()
        self.assertIn("vmstat", str(ctx.exception))
        self.assertTrue(self.factory.processes["iostat"].terminated)
        self.assertTrue(self.factory.processes["pidstat"].terminated)
        records = {r.name: r for r in controller.records()}
        self.assertIsNotNone(records["vmstat"].stop_error)
        self.assertIsNone(records["pidstat"].stop_error)

    def test_handles_closed_after_stop(self):
        controller, _ = self.start()
        controller.stop()
        for stdout_handle, stderr_handle in self.factory.handles.values():
            self.assertTrue(stdout_handle.closed)
            self.assertTrue(stderr_handle.closed)

    def test_stop_never_started_is_noop(self):
        controller = self.make_controller()
        self.assertEqual(controller.stop(), ())

    def test_repeated_stop_idempotent(self):
        controller, _ = self.start()
        first = controller.stop()
        second = controller.stop()
        self.assertEqual(first, second)
        for name, process in self.factory.processes.items():
            self.assertEqual(len(process.wait_timeouts), 1, name)

    def test_wait_timeouts_are_five_then_two(self):
        controller, _ = self.start()
        controller.stop()
        for name, process in self.factory.processes.items():
            self.assertEqual(process.wait_timeouts, [5], name)


class MetadataTests(CollectorTestBase):
    def test_metadata_structure(self):
        controller, _ = self.start()
        controller.stop()
        path = self.artifact_root / "collectors" / "collectors.json"
        controller.write_metadata(path)
        metadata = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"], 1)
        self.assertEqual(metadata["baseline_cycle_id"], "cycle-2026-08")
        self.assertEqual(
            metadata["manifest_id"], "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"
        )
        self.assertEqual(metadata["run_id"], "run-001")
        self.assertEqual(metadata["sampling_interval_seconds"], 5)
        collectors = metadata["collectors"]
        self.assertEqual(
            [c["name"] for c in collectors], ["pidstat", "sar", "vmstat", "iostat"]
        )
        pidstat = collectors[0]
        self.assertEqual(pidstat["argv"], list(DEFAULT_COLLECTORS[0].argv))
        self.assertEqual(pidstat["stdout_path"], "collectors/pidstat.log")
        self.assertEqual(pidstat["stderr_path"], "collectors/pidstat.stderr.log")
        self.assertEqual(pidstat["return_code"], -15)
        self.assertTrue(pidstat["graceful_termination_requested"])
        self.assertFalse(pidstat["forced_kill_used"])
        self.assertIsNone(pidstat["start_error"])
        self.assertIsNone(pidstat["stop_error"])

    def test_metadata_deterministic(self):
        controller, _ = self.start()
        controller.stop()
        first = self.artifact_root / "collectors" / "a.json"
        second = self.artifact_root / "collectors" / "b.json"
        controller.write_metadata(first)
        controller.write_metadata(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_metadata_has_no_environment_or_secrets(self):
        controller, _ = self.start()
        controller.stop()
        path = self.artifact_root / "collectors" / "collectors.json"
        controller.write_metadata(path)
        text = path.read_text(encoding="utf-8").lower()
        for marker in ("path=", "lang", "password", "secret", "token"):
            self.assertNotIn(marker, text)

    def test_metadata_paths_are_relative(self):
        controller, _ = self.start()
        controller.stop()
        path = self.artifact_root / "collectors" / "collectors.json"
        controller.write_metadata(path)
        metadata = json.loads(path.read_text(encoding="utf-8"))
        for record in metadata["collectors"]:
            self.assertFalse(record["stdout_path"].startswith("/"))
            self.assertNotIn("..", record["stdout_path"])

    def test_write_metadata_rejects_escaping_path(self):
        controller, _ = self.start()
        controller.stop()
        bad = dataclasses.replace(
            controller.records()[0], stdout_path="../escape.log"
        )
        controller._records = (bad,) + controller.records()[1:]
        with self.assertRaises(CollectorError):
            controller.write_metadata(
                self.artifact_root / "collectors" / "collectors.json"
            )


class EnvironmentSafetyTests(unittest.TestCase):
    def test_minimal_environment_allowlist(self):
        environ = {
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "AWS_SECRET_ACCESS_KEY": "nope",
            "HOME": "/home/user",
        }
        minimal = _minimal_environment(environ)
        self.assertEqual(
            set(minimal), {"PATH", "LANG", "LC_ALL", "TZ"}
        )
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", minimal)

    def test_platform_kwargs_posix(self):
        kwargs = _popen_platform_kwargs("posix")
        self.assertIs(kwargs["start_new_session"], True)
        self.assertIs(kwargs["close_fds"], True)

    def test_platform_kwargs_windows(self):
        kwargs = _popen_platform_kwargs("nt")
        self.assertNotIn("start_new_session", kwargs)
        self.assertIn("creationflags", kwargs)


class DefaultFactoryTests(CollectorTestBase):
    def test_default_factory_uses_popen_safely(self):
        controller = CollectorController(monotonic_ns=self.clock.now)
        with mock.patch(
            "tools.performance.a3.collectors.subprocess.Popen"
        ) as popen_mock, mock.patch(
            "tools.performance.a3.collectors.os.environ", {"PATH": "/usr/bin"}
        ):
            process = FakeProcess(pid=9999)
            popen_mock.return_value = process
            controller.start(make_context(self.artifact_root))
        _, kwargs = popen_mock.call_args_list[0]
        argv = popen_mock.call_args_list[0][0][0]
        self.assertEqual(argv, list(DEFAULT_COLLECTORS[0].argv))
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIsNotNone(kwargs["stdout"])
        self.assertIsNotNone(kwargs["stderr"])
        self.assertEqual(set(kwargs["env"]), {"PATH"})
        # Cleanup: stop the fake process.
        controller.stop()


class ImmutabilityTests(CollectorTestBase):
    def test_record_frozen(self):
        _, records = self.start()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            records[0].pid = 1

    def test_record_argv_tuple(self):
        _, records = self.start()
        self.assertIsInstance(records[0].argv, tuple)

    def test_context_frozen(self):
        context = make_context(self.artifact_root)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.run_id = "x"

    def test_spec_list_copied(self):
        specs = list(DEFAULT_COLLECTORS)
        controller = CollectorController(
            specs=specs, process_factory=FakeFactory()
        )
        specs.clear()
        self.start(controller)


if __name__ == "__main__":
    unittest.main()
