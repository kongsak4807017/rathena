"""Tests for the A3 lifecycle state machine and command execution boundary."""

import copy
import dataclasses
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.performance.a3.config import load_config
from tools.performance.a3.lifecycle import (
    CatastrophicRunError,
    CommandExecutionError,
    InvalidTransitionError,
    LifecycleError,
    RunCommand,
    RunController,
    RunEvent,
)
from tools.performance.a3.models import RunPhase

FIXTURE_MANIFEST = (
    Path(__file__).resolve().parent / "fixtures" / "valid_manifest.json"
)
EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "a3.example.json"

SQL_ENV = "RATHENA_SQL_OBSERVABILITY_SLOW_MS"
SCRIPT_ENV = "RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS"

# Approved reference topology for the A3 baseline host.
REFERENCE_TOPOLOGY = {
    "physical_cores": 8,
    "logical_threads": 16,
    "ram_bytes": 34359738368,
    "link_speed_mbps": 1000,
}


def load_manifest() -> dict:
    with open(FIXTURE_MANIFEST, "r", encoding="utf-8") as handle:
        return json.load(handle)


def reference_manifest() -> dict:
    """Fixture manifest adjusted to the approved reference topology."""
    manifest = load_manifest()
    manifest["hardware"].update(REFERENCE_TOPOLOGY)
    manifest["operating_system"]["distribution"] = "ubuntu"
    manifest["operating_system"]["distribution_version"] = "24.04.4"
    return manifest


class FakeClock:
    def __init__(self):
        self.ns = 1_000_000_000
        self.sleeps = []

    def now(self) -> int:
        return self.ns

    def sleep(self, seconds) -> None:
        self.sleeps.append(seconds)
        self.ns += int(seconds * 1_000_000_000)


def completed(argv, rc=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(argv, rc, stdout, stderr)


def make_command(
    name="svc",
    argv=("run", "--flag"),
    timeout_seconds=5,
    phase=RunPhase.SERVICE_START,
    catastrophic_on_failure=False,
    environment=None,
    cwd=None,
) -> RunCommand:
    return RunCommand(
        name=name,
        argv=tuple(argv),
        timeout_seconds=timeout_seconds,
        phase=phase,
        catastrophic_on_failure=catastrophic_on_failure,
        environment={} if environment is None else environment,
        cwd=cwd,
    )


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.artifact_root = Path(self._tmp.name)
        self.config = load_config(EXAMPLE_CONFIG)

    def make_controller(
        self,
        runner=None,
        manifest=None,
        verifier=None,
        clock=None,
    ):
        clock = clock or FakeClock()
        controller = RunController(
            config=self.config,
            manifest=reference_manifest() if manifest is None else manifest,
            artifact_root=self.artifact_root,
            command_runner=runner,
            monotonic_ns=clock.now,
            sleep=clock.sleep,
            manifest_verifier=verifier,
        )
        return controller, clock

    def succeed_runner(self, calls=None):
        def runner(argv, timeout_seconds, cwd, environment):
            if calls is not None:
                calls.append(
                    {
                        "argv": argv,
                        "timeout_seconds": timeout_seconds,
                        "cwd": cwd,
                        "environment": environment,
                    }
                )
            return completed(argv, 0, stdout=b"ok\n", stderr=b"")

        return runner


class ErrorHierarchyTests(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(InvalidTransitionError, LifecycleError))
        self.assertTrue(issubclass(CommandExecutionError, LifecycleError))
        self.assertTrue(issubclass(CatastrophicRunError, LifecycleError))
        self.assertTrue(issubclass(LifecycleError, Exception))


class StateMachineTests(ControllerTestBase):
    def test_initial_phase_is_environment_check(self):
        controller, _ = self.make_controller()
        self.assertIs(controller.current_phase(), RunPhase.ENVIRONMENT_CHECK)

    def test_every_valid_forward_transition(self):
        controller, _ = self.make_controller()
        path = [
            RunPhase.SERVICE_START,
            RunPhase.PRECONDITIONING,
            RunPhase.RAMP_UP,
            RunPhase.STEADY_STATE,
            RunPhase.COOL_DOWN,
            RunPhase.VALIDATION,
            RunPhase.REPORTING,
        ]
        for phase in path:
            event = controller.transition(phase)
            self.assertIs(controller.current_phase(), phase)
            self.assertIs(event.phase, phase)
        self.assertIs(controller.current_phase(), RunPhase.REPORTING)

    def test_skip_rejected(self):
        controller, _ = self.make_controller()
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.PRECONDITIONING)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.REPORTING)

    def test_repeat_rejected(self):
        controller, _ = self.make_controller()
        controller.transition(RunPhase.SERVICE_START)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.SERVICE_START)

    def test_backward_rejected(self):
        controller, _ = self.make_controller()
        controller.transition(RunPhase.SERVICE_START)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.ENVIRONMENT_CHECK)

    def test_reporting_is_terminal(self):
        controller, _ = self.make_controller()
        for phase in (
            RunPhase.SERVICE_START,
            RunPhase.PRECONDITIONING,
            RunPhase.RAMP_UP,
            RunPhase.STEADY_STATE,
            RunPhase.COOL_DOWN,
            RunPhase.VALIDATION,
            RunPhase.REPORTING,
        ):
            controller.transition(phase)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.VALIDATION)
        with self.assertRaises(InvalidTransitionError):
            controller.abort("late", catastrophic=True)

    def test_catastrophic_chain(self):
        controller, _ = self.make_controller()
        controller.transition(RunPhase.SERVICE_START)
        event = controller.abort("boom", catastrophic=True)
        self.assertIs(controller.current_phase(), RunPhase.ABORTED)
        self.assertTrue(event.catastrophic)
        controller.transition(RunPhase.ARTIFACT_CAPTURE)
        controller.transition(RunPhase.ROOT_CAUSE_ANALYSIS)
        self.assertIs(controller.current_phase(), RunPhase.ROOT_CAUSE_ANALYSIS)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.ARTIFACT_CAPTURE)

    def test_aborted_only_moves_to_artifact_capture(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.PRECONDITIONING)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.ROOT_CAUSE_ANALYSIS)

    def test_artifact_capture_only_moves_to_root_cause(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        controller.transition(RunPhase.ARTIFACT_CAPTURE)
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.ABORTED)

    def test_direct_aborted_transition_rejected(self):
        controller, _ = self.make_controller()
        with self.assertRaises(InvalidTransitionError):
            controller.transition(RunPhase.ABORTED)

    def test_events_have_increasing_sequence_starting_at_one(self):
        controller, _ = self.make_controller()
        controller.transition(RunPhase.SERVICE_START)
        controller.transition(RunPhase.PRECONDITIONING)
        sequences = [event.sequence for event in controller.events()]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))


class PreflightTests(ControllerTestBase):
    def _run_preflight(self, manifest=None, verifier=None, runner=None):
        controller, clock = self.make_controller(
            runner=runner or self.succeed_runner(), manifest=manifest, verifier=verifier
        )
        return controller, controller.run_preflight()

    def _assert_preflight_failure(self, manifest=None, verifier=None):
        runner_calls = []
        controller, _ = self.make_controller(
            runner=self.succeed_runner(calls=runner_calls),
            manifest=manifest,
            verifier=verifier,
        )
        with self.assertRaises(CatastrophicRunError):
            controller.run_preflight()
        self.assertIs(controller.current_phase(), RunPhase.ABORTED)
        events = controller.events()
        self.assertTrue(events[-1].catastrophic)
        self.assertEqual(events[-1].phase, RunPhase.ABORTED)
        with self.assertRaises(LifecycleError):
            controller.run_service_start((make_command(),))
        self.assertEqual(runner_calls, [])
        return controller

    def test_valid_manifest_passes(self):
        controller, events = self._run_preflight()
        self.assertIs(controller.current_phase(), RunPhase.ENVIRONMENT_CHECK)
        self.assertTrue(any(e.event_type == "preflight_passed" for e in events))

    def test_non_dict_manifest_fails(self):
        self._assert_preflight_failure(manifest=["not", "a", "dict"])

    def test_ineligible_manifest_fails(self):
        manifest = reference_manifest()
        manifest["eligible_for_execution"] = False
        self._assert_preflight_failure(manifest=manifest)

    def test_missing_capture_errors_fails(self):
        manifest = reference_manifest()
        del manifest["capture_errors"]
        self._assert_preflight_failure(manifest=manifest)

    def test_non_empty_capture_errors_fails(self):
        manifest = reference_manifest()
        manifest["capture_errors"] = [{"field": "x", "command": ["y"], "return_code": 1, "stderr": "z"}]
        self._assert_preflight_failure(manifest=manifest)

    def test_invalid_manifest_id_fails(self):
        manifest = reference_manifest()
        manifest["manifest_id"] = "bogus"
        self._assert_preflight_failure(manifest=manifest)

    def test_invalid_manifest_sha_fails(self):
        manifest = reference_manifest()
        manifest["manifest_sha256"] = "not-hex"
        self._assert_preflight_failure(manifest=manifest)

    def test_dirty_working_tree_fails(self):
        manifest = reference_manifest()
        manifest["source"]["working_tree_clean"] = False
        self._assert_preflight_failure(manifest=manifest)

    def test_manifest_drift_fails(self):
        verifier = lambda manifest: ["build.map_server_sha256 changed"]
        self._assert_preflight_failure(verifier=verifier)

    def test_wrong_cpu_cores_fails(self):
        manifest = reference_manifest()
        manifest["hardware"]["physical_cores"] = 4
        self._assert_preflight_failure(manifest=manifest)

    def test_wrong_logical_threads_fails(self):
        manifest = reference_manifest()
        manifest["hardware"]["logical_threads"] = 8
        self._assert_preflight_failure(manifest=manifest)

    def test_wrong_ram_fails(self):
        manifest = reference_manifest()
        manifest["hardware"]["ram_bytes"] = 1000
        self._assert_preflight_failure(manifest=manifest)

    def test_wrong_link_speed_fails(self):
        manifest = reference_manifest()
        manifest["hardware"]["link_speed_mbps"] = 100
        self._assert_preflight_failure(manifest=manifest)

    def test_wrong_ubuntu_version_fails(self):
        manifest = reference_manifest()
        manifest["operating_system"]["distribution_version"] = "22.04"
        self._assert_preflight_failure(manifest=manifest)

    def test_wrong_distribution_fails(self):
        manifest = reference_manifest()
        manifest["operating_system"]["distribution"] = "debian"
        self._assert_preflight_failure(manifest=manifest)

    def test_wrong_sampling_interval_fails(self):
        manifest = reference_manifest()
        manifest["rathena_configuration"]["snapshot_interval_seconds"] = 10
        self._assert_preflight_failure(manifest=manifest)

    def test_missing_sql_threshold_fails(self):
        manifest = reference_manifest()
        manifest["rathena_configuration"]["slow_sql_threshold"] = None
        self._assert_preflight_failure(manifest=manifest)

    def test_invalid_sql_threshold_fails(self):
        manifest = reference_manifest()
        manifest["rathena_configuration"]["slow_sql_threshold"] = "fast"
        self._assert_preflight_failure(manifest=manifest)

    def test_missing_script_threshold_fails(self):
        manifest = reference_manifest()
        manifest["rathena_configuration"]["slow_script_threshold"] = None
        self._assert_preflight_failure(manifest=manifest)


class ThresholdEnvironmentTests(ControllerTestBase):
    def _start_services(self, command_env=None):
        calls = []
        controller, _ = self.make_controller(runner=self.succeed_runner(calls=calls))
        controller.run_preflight()
        command = make_command(environment=command_env or {})
        events = controller.run_service_start((command,))
        return controller, calls, events

    def test_sql_threshold_injected_exactly(self):
        _, calls, _ = self._start_services()
        self.assertEqual(calls[0]["environment"][SQL_ENV], "50")

    def test_script_threshold_injected_exactly(self):
        _, calls, _ = self._start_services()
        self.assertEqual(calls[0]["environment"][SCRIPT_ENV], "25")

    def test_conflicting_inherited_values_overridden(self):
        _, calls, _ = self._start_services(
            command_env={SQL_ENV: "999", SCRIPT_ENV: "999", "A3_SAFE_FLAG": "1"}
        )
        self.assertEqual(calls[0]["environment"][SQL_ENV], "50")
        self.assertEqual(calls[0]["environment"][SCRIPT_ENV], "25")
        self.assertEqual(calls[0]["environment"]["A3_SAFE_FLAG"], "1")

    def test_arbitrary_environment_not_in_event_details(self):
        _, _, events = self._start_services(
            command_env={"A3_SAFE_FLAG": "1", "OTHER_SETTING": "x"}
        )
        command_events = [e for e in events if e.event_type == "command_succeeded"]
        self.assertEqual(len(command_events), 1)
        details = dict(command_events[0].details)
        env_details = details.get("environment", {})
        self.assertEqual(set(env_details), {SQL_ENV, SCRIPT_ENV})
        self.assertEqual(env_details[SQL_ENV], 50)
        self.assertEqual(env_details[SCRIPT_ENV], 25)

    def test_command_receives_explicit_immutable_environment(self):
        _, calls, _ = self._start_services()
        environment = calls[0]["environment"]
        with self.assertRaises(TypeError):
            environment["NEW"] = "1"

    def test_full_process_environment_never_logged(self):
        _, _, events = self._start_services(command_env={"A3_SAFE_FLAG": "1"})
        for event in events:
            self.assertNotIn("A3_SAFE_FLAG", json.dumps(dict(event.details)))


class CommandValidationTests(unittest.TestCase):
    def test_empty_argv_rejected(self):
        with self.assertRaises(ValueError):
            make_command(argv=())

    def test_non_string_argument_rejected(self):
        with self.assertRaises(ValueError):
            make_command(argv=("run", 5))

    def test_nul_argument_rejected(self):
        with self.assertRaises(ValueError):
            make_command(argv=("run", "bad\x00arg"))

    def test_zero_timeout_rejected(self):
        with self.assertRaises(ValueError):
            make_command(timeout_seconds=0)

    def test_negative_timeout_rejected(self):
        with self.assertRaises(ValueError):
            make_command(timeout_seconds=-3)

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            make_command(name="")


class CommandExecutionTests(ControllerTestBase):
    def _controller(self, runner):
        return self.make_controller(runner=runner)[0]

    def test_success(self):
        controller = self._controller(self.succeed_runner())
        event = controller.execute(make_command())
        self.assertEqual(event.event_type, "command_succeeded")
        self.assertEqual(event.return_code, 0)
        self.assertFalse(event.timed_out)
        self.assertFalse(event.catastrophic)

    def test_non_catastrophic_nonzero_raises_command_error(self):
        controller = self._controller(lambda **kwargs: completed((), 3, stderr=b"boom"))
        with self.assertRaises(CommandExecutionError):
            controller.execute(make_command())
        self.assertIs(controller.current_phase(), RunPhase.ENVIRONMENT_CHECK)
        event = controller.events()[-1]
        self.assertEqual(event.event_type, "command_failed")
        self.assertEqual(event.return_code, 3)
        self.assertFalse(event.catastrophic)

    def test_catastrophic_nonzero_aborts(self):
        controller = self._controller(lambda **kwargs: completed((), 1, stderr=b"boom"))
        with self.assertRaises(CatastrophicRunError):
            controller.execute(make_command(catastrophic_on_failure=True))
        self.assertIs(controller.current_phase(), RunPhase.ABORTED)
        event = controller.events()[-2]
        self.assertEqual(event.event_type, "command_failed")
        self.assertTrue(event.catastrophic)

    def test_non_catastrophic_timeout(self):
        def runner(**kwargs):
            raise subprocess.TimeoutExpired(cmd=["x"], timeout=5)

        controller = self._controller(runner)
        with self.assertRaises(CommandExecutionError):
            controller.execute(make_command())
        event = controller.events()[-1]
        self.assertTrue(event.timed_out)
        self.assertIsNone(event.return_code)

    def test_catastrophic_timeout_aborts(self):
        def runner(**kwargs):
            raise subprocess.TimeoutExpired(cmd=["x"], timeout=5)

        controller = self._controller(runner)
        with self.assertRaises(CatastrophicRunError):
            controller.execute(make_command(catastrophic_on_failure=True))
        self.assertIs(controller.current_phase(), RunPhase.ABORTED)

    def test_oserror(self):
        def runner(**kwargs):
            raise OSError("No such file or directory")

        controller = self._controller(runner)
        with self.assertRaises(CommandExecutionError):
            controller.execute(make_command())
        event = controller.events()[-1]
        self.assertIn("No such file or directory", event.stderr_summary)

    def test_invalid_utf8_replaced(self):
        runner = lambda **kwargs: completed((), 0, stdout=b"bad \xff\xfe bytes")
        controller = self._controller(runner)
        event = controller.execute(make_command())
        self.assertIn("�", event.stdout_summary)

    def test_summary_truncated_to_500(self):
        runner = lambda **kwargs: completed((), 0, stdout=b"x" * 5000)
        controller = self._controller(runner)
        event = controller.execute(make_command())
        self.assertEqual(len(event.stdout_summary), 500)

    def test_whitespace_collapsed(self):
        runner = lambda **kwargs: completed((), 0, stdout=b"a\n b\t\tc   d")
        controller = self._controller(runner)
        event = controller.execute(make_command())
        self.assertEqual(event.stdout_summary, "a b c d")

    def test_default_runner_uses_shell_false_argv_cwd_env(self):
        controller, _ = self.make_controller()
        command = make_command(cwd=self.artifact_root, environment={"A3_SAFE_FLAG": "1"})
        with mock.patch(
            "tools.performance.a3.lifecycle.subprocess.run"
        ) as run_mock:
            run_mock.return_value = completed((), 0, stdout=b"ok")
            controller.execute(command)
        _, kwargs = run_mock.call_args
        argv = run_mock.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv, ["run", "--flag"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["cwd"], self.artifact_root)
        self.assertEqual(kwargs["env"], {"A3_SAFE_FLAG": "1"})
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.PIPE)


class DurationPhaseTests(ControllerTestBase):
    def _run_full_cycle(self):
        controller, clock = self.make_controller(runner=self.succeed_runner())
        controller.run_preflight()
        controller.run_service_start((make_command(),))
        pre = controller.run_preconditioning()
        ramp = controller.run_ramp_up()
        steady = controller.run_steady_state()
        cool = controller.run_cooldown()
        controller.run_validation()
        controller.run_reporting()
        return controller, clock, (pre, ramp, steady, cool)

    def test_exact_transition_order(self):
        controller, _, _ = self._run_full_cycle()
        phases = [
            event.phase
            for event in controller.events()
            if event.event_type == "phase_transition"
        ]
        self.assertEqual(
            phases,
            [
                RunPhase.SERVICE_START,
                RunPhase.PRECONDITIONING,
                RunPhase.RAMP_UP,
                RunPhase.STEADY_STATE,
                RunPhase.COOL_DOWN,
                RunPhase.VALIDATION,
                RunPhase.REPORTING,
            ],
        )
        self.assertIs(controller.current_phase(), RunPhase.REPORTING)

    def test_exact_sleep_values(self):
        _, clock, _ = self._run_full_cycle()
        self.assertEqual(clock.sleeps, [600, 300, 1200, 300])

    def test_duration_event_details(self):
        _, _, events = self._run_full_cycle()
        expected = [600, 300, 1200, 300]
        for event, seconds in zip(events, expected):
            details = dict(event.details)
            self.assertEqual(details["planned_duration_seconds"], seconds)
            self.assertEqual(
                details["actual_duration_ns"], seconds * 1_000_000_000
            )
            self.assertIsNotNone(event.started_monotonic_ns)
            self.assertEqual(
                event.finished_monotonic_ns - event.started_monotonic_ns,
                seconds * 1_000_000_000,
            )

    def test_duration_phases_use_config_values(self):
        controller, clock = self.make_controller()
        controller.transition(RunPhase.SERVICE_START)
        controller.run_preconditioning()
        self.assertEqual(clock.sleeps, [self.config.preconditioning_seconds])


class EventLogTests(ControllerTestBase):
    def _run_and_write(self):
        controller, _ = self.make_controller(runner=self.succeed_runner())
        controller.run_preflight()
        controller.run_service_start((make_command(),))
        controller.run_preconditioning()
        controller.run_ramp_up()
        controller.run_steady_state()
        controller.run_cooldown()
        controller.run_validation()
        controller.run_reporting()
        path = self.artifact_root / "events.json"
        controller.write_event_log(path)
        return controller, path

    def test_event_log_round_trip(self):
        controller, path = self._run_and_write()
        log = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(log["version"], 1)
        self.assertEqual(log["manifest_id"], controller._manifest["manifest_id"])
        self.assertEqual(log["final_phase"], "REPORTING")
        sequences = [event["sequence"] for event in log["events"]]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))

    def test_event_log_deterministic(self):
        _, first = self._run_and_write()
        second_path = self.artifact_root / "events2.json"
        controller2, _ = self.make_controller(runner=self.succeed_runner())
        controller2.run_preflight()
        controller2.run_service_start((make_command(),))
        controller2.run_preconditioning()
        controller2.run_ramp_up()
        controller2.run_steady_state()
        controller2.run_cooldown()
        controller2.run_validation()
        controller2.run_reporting()
        controller2.write_event_log(second_path)
        self.assertEqual(first.read_bytes(), second_path.read_bytes())

    def test_event_log_contains_no_secrets(self):
        _, path = self._run_and_write()
        text = path.read_text(encoding="utf-8").lower()
        for marker in ("password", "secret", "token", "api_key", "private_key"):
            self.assertNotIn(marker, text)

    def test_event_log_atomic_write(self):
        _, path = self._run_and_write()
        leftovers = [
            p for p in self.artifact_root.iterdir() if p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_stored_event_details_immune_to_caller_mutation(self):
        environment = {"A3_SAFE_FLAG": "1"}
        command = make_command(environment=environment)
        environment["A3_SAFE_FLAG"] = "mutated"
        environment["LATE"] = "x"
        self.assertEqual(command.environment["A3_SAFE_FLAG"], "1")
        self.assertNotIn("LATE", command.environment)


class ImmutabilityTests(ControllerTestBase):
    def test_run_command_frozen(self):
        command = make_command()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            command.name = "x"

    def test_run_command_environment_immutable(self):
        command = make_command(environment={"A": "1"})
        with self.assertRaises(TypeError):
            command.environment["A"] = "2"

    def test_run_command_argv_is_tuple(self):
        command = RunCommand(
            name="svc",
            argv=["run", "x"],
            timeout_seconds=5,
            phase=RunPhase.SERVICE_START,
            catastrophic_on_failure=False,
            environment={},
            cwd=None,
        )
        self.assertIsInstance(command.argv, tuple)

    def test_run_event_frozen(self):
        controller, _ = self.make_controller()
        event = controller.transition(RunPhase.SERVICE_START)
        self.assertIsInstance(event, RunEvent)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.sequence = 99

    def test_run_event_details_immutable(self):
        controller, _ = self.make_controller()
        event = controller.transition(RunPhase.SERVICE_START)
        with self.assertRaises(TypeError):
            event.details["x"] = 1


class AbortRecoveryTests(ControllerTestBase):
    ACTIVE_PHASES = [
        RunPhase.ENVIRONMENT_CHECK,
        RunPhase.SERVICE_START,
        RunPhase.PRECONDITIONING,
        RunPhase.RAMP_UP,
        RunPhase.STEADY_STATE,
        RunPhase.COOL_DOWN,
        RunPhase.VALIDATION,
    ]

    FORWARD_PATH = [
        RunPhase.SERVICE_START,
        RunPhase.PRECONDITIONING,
        RunPhase.RAMP_UP,
        RunPhase.STEADY_STATE,
        RunPhase.COOL_DOWN,
        RunPhase.VALIDATION,
        RunPhase.REPORTING,
    ]

    def _advance(self, controller, target):
        if target is RunPhase.ENVIRONMENT_CHECK:
            return
        for phase in self.FORWARD_PATH:
            controller.transition(phase)
            if phase is target:
                return

    def test_abort_from_every_active_phase_succeeds(self):
        for target in self.ACTIVE_PHASES:
            with self.subTest(phase=target):
                controller, _ = self.make_controller()
                self._advance(controller, target)
                self.assertIs(controller.current_phase(), target)
                event = controller.abort("reason", catastrophic=True)
                self.assertIs(controller.current_phase(), RunPhase.ABORTED)
                self.assertIs(event.phase, RunPhase.ABORTED)

    def _assert_abort_rejected(self, controller):
        events_before = controller.events()
        phase_before = controller.current_phase()
        with self.assertRaises(InvalidTransitionError):
            controller.abort("again", catastrophic=True)
        self.assertEqual(controller.events(), events_before)
        self.assertIs(controller.current_phase(), phase_before)

    def test_abort_from_aborted_rejected(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        self._assert_abort_rejected(controller)

    def test_abort_from_artifact_capture_rejected(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        controller.transition(RunPhase.ARTIFACT_CAPTURE)
        self._assert_abort_rejected(controller)

    def test_abort_from_root_cause_analysis_rejected(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        controller.transition(RunPhase.ARTIFACT_CAPTURE)
        controller.transition(RunPhase.ROOT_CAUSE_ANALYSIS)
        self._assert_abort_rejected(controller)

    def test_abort_from_reporting_rejected(self):
        controller, _ = self.make_controller()
        self._advance(controller, RunPhase.REPORTING)
        self._assert_abort_rejected(controller)

    def test_aborted_proceeds_only_via_artifact_capture(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        event = controller.transition(RunPhase.ARTIFACT_CAPTURE)
        self.assertIs(controller.current_phase(), RunPhase.ARTIFACT_CAPTURE)
        self.assertIs(event.phase, RunPhase.ARTIFACT_CAPTURE)

    def test_artifact_capture_proceeds_only_via_root_cause_analysis(self):
        controller, _ = self.make_controller()
        controller.abort("boom", catastrophic=True)
        controller.transition(RunPhase.ARTIFACT_CAPTURE)
        event = controller.transition(RunPhase.ROOT_CAUSE_ANALYSIS)
        self.assertIs(controller.current_phase(), RunPhase.ROOT_CAUSE_ANALYSIS)
        self.assertIs(event.phase, RunPhase.ROOT_CAUSE_ANALYSIS)


if __name__ == "__main__":
    unittest.main()
