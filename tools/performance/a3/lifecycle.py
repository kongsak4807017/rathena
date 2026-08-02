"""A3 run lifecycle state machine and command execution boundary.

Approved successful path:
ENVIRONMENT_CHECK -> SERVICE_START -> PRECONDITIONING -> RAMP_UP ->
STEADY_STATE -> COOL_DOWN -> VALIDATION -> REPORTING

Catastrophic path:
any non-terminal active phase -> ABORTED -> ARTIFACT_CAPTURE ->
ROOT_CAUSE_ANALYSIS

All timing uses an injected monotonic clock and sleep callable. subprocess is
used only inside the default command runner, always with argument arrays,
``shell=False``, explicit timeout, and UTF-8 replacement decoding.
"""

import copy
import dataclasses
import re
import subprocess
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from tools.performance.a3.io import write_json_atomic
from tools.performance.a3.models import A3Config, RunPhase

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LifecycleError(Exception):
    """Base class for all A3 lifecycle errors."""


class InvalidTransitionError(LifecycleError):
    """Raised when a phase transition violates the approved lifecycle."""


class CommandExecutionError(LifecycleError):
    """Raised when a command fails without catastrophic policy."""


class CatastrophicRunError(LifecycleError):
    """Raised when a failure forces the catastrophic abort path."""


# ---------------------------------------------------------------------------
# Reference topology and thresholds (approved A3 baseline constants)
# ---------------------------------------------------------------------------

REFERENCE_PHYSICAL_CORES = 8
REFERENCE_LOGICAL_THREADS = 16
REFERENCE_RAM_BYTES = 34359738368
REFERENCE_LINK_SPEED_MBPS = 1000
REFERENCE_DISTRIBUTION = "ubuntu"
REFERENCE_DISTRIBUTION_VERSION = "24.04.4"
REFERENCE_SNAPSHOT_INTERVAL_SECONDS = 5

SQL_THRESHOLD_ENV = "RATHENA_SQL_OBSERVABILITY_SLOW_MS"
SCRIPT_THRESHOLD_ENV = "RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS"

SUMMARY_LIMIT = 500

_MANIFEST_ID_RE = re.compile(
    r"^a3-\d{8}-[0-9a-f]{7}-ubuntu2404-8c16t-32g-\d{3}$"
)
_MANIFEST_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_FORWARD = {
    RunPhase.ENVIRONMENT_CHECK: RunPhase.SERVICE_START,
    RunPhase.SERVICE_START: RunPhase.PRECONDITIONING,
    RunPhase.PRECONDITIONING: RunPhase.RAMP_UP,
    RunPhase.RAMP_UP: RunPhase.STEADY_STATE,
    RunPhase.STEADY_STATE: RunPhase.COOL_DOWN,
    RunPhase.COOL_DOWN: RunPhase.VALIDATION,
    RunPhase.VALIDATION: RunPhase.REPORTING,
    RunPhase.ABORTED: RunPhase.ARTIFACT_CAPTURE,
    RunPhase.ARTIFACT_CAPTURE: RunPhase.ROOT_CAUSE_ANALYSIS,
}

_TERMINAL = frozenset({RunPhase.REPORTING, RunPhase.ROOT_CAUSE_ANALYSIS})

# abort() may only leave an active successful-path phase. Recovery phases
# (ABORTED, ARTIFACT_CAPTURE) and terminal phases are one-way.
_ABORTABLE = frozenset(
    {
        RunPhase.ENVIRONMENT_CHECK,
        RunPhase.SERVICE_START,
        RunPhase.PRECONDITIONING,
        RunPhase.RAMP_UP,
        RunPhase.STEADY_STATE,
        RunPhase.COOL_DOWN,
        RunPhase.VALIDATION,
    }
)


# ---------------------------------------------------------------------------
# Command and event records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RunCommand:
    """Immutable command description for the execution boundary."""

    name: str
    argv: Tuple[str, ...]
    timeout_seconds: int
    phase: RunPhase
    catastrophic_on_failure: bool
    environment: Mapping[str, str]
    cwd: Optional[Path]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"command name must be a non-empty string, got {self.name!r}")
        argv = tuple(self.argv)
        if not argv:
            raise ValueError("argv must be a non-empty argument array")
        for argument in argv:
            if not isinstance(argument, str):
                raise ValueError(f"argv entries must be strings, got {argument!r}")
            if "\x00" in argument:
                raise ValueError("argv entries must not contain NUL bytes")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                f"timeout_seconds must be a positive integer, got {self.timeout_seconds!r}"
            )
        object.__setattr__(self, "argv", argv)
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )
        if self.cwd is not None:
            object.__setattr__(self, "cwd", Path(self.cwd))


@dataclasses.dataclass(frozen=True)
class RunEvent:
    """Immutable record of one lifecycle event."""

    sequence: int
    phase: RunPhase
    event_type: str
    command_name: Optional[str]
    started_monotonic_ns: int
    finished_monotonic_ns: Optional[int]
    return_code: Optional[int]
    timed_out: bool
    stdout_summary: str
    stderr_summary: str
    catastrophic: bool
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summarize(data: Optional[bytes]) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    return " ".join(text.split())[:SUMMARY_LIMIT]


def _default_command_runner(argv, timeout_seconds, cwd, environment):
    """Default runner: the only place subprocess is invoked."""
    return subprocess.run(
        list(argv),
        shell=False,
        timeout=timeout_seconds,
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _plain(value: Any) -> Any:
    """Convert mappings (including MappingProxyType) to plain JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, RunPhase):
        return value.value
    return value


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class RunController:
    """Lifecycle state machine with manifest preflight and command boundary."""

    def __init__(
        self,
        config: A3Config,
        manifest: dict,
        artifact_root: Path,
        command_runner: Optional[Callable] = None,
        monotonic_ns: Optional[Callable[[], int]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        manifest_verifier: Optional[Callable[[dict], List[str]]] = None,
    ) -> None:
        self._config = config
        self._manifest = copy.deepcopy(manifest) if isinstance(manifest, dict) else manifest
        self._artifact_root = Path(artifact_root)
        self._command_runner = command_runner or _default_command_runner
        self._monotonic_ns = monotonic_ns or time.monotonic_ns
        self._sleep = sleep or time.sleep
        self._manifest_verifier = manifest_verifier
        self._phase = RunPhase.ENVIRONMENT_CHECK
        self._events: List[RunEvent] = []
        self._preflight_passed = False
        self._append_event(
            phase=RunPhase.ENVIRONMENT_CHECK,
            event_type="run_initialized",
            details={"from": None},
        )

    # -- event plumbing ----------------------------------------------------

    def _append_event(
        self,
        phase: RunPhase,
        event_type: str,
        command_name: Optional[str] = None,
        started_monotonic_ns: Optional[int] = None,
        finished_monotonic_ns: Optional[int] = None,
        return_code: Optional[int] = None,
        timed_out: bool = False,
        stdout_summary: str = "",
        stderr_summary: str = "",
        catastrophic: bool = False,
        details: Optional[Mapping[str, Any]] = None,
    ) -> RunEvent:
        event = RunEvent(
            sequence=len(self._events) + 1,
            phase=phase,
            event_type=event_type,
            command_name=command_name,
            started_monotonic_ns=(
                self._monotonic_ns() if started_monotonic_ns is None else started_monotonic_ns
            ),
            finished_monotonic_ns=finished_monotonic_ns,
            return_code=return_code,
            timed_out=timed_out,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            catastrophic=catastrophic,
            details=dict(details or {}),
        )
        self._events.append(event)
        return event

    # -- inspection ----------------------------------------------------------

    def current_phase(self) -> RunPhase:
        return self._phase

    def events(self) -> Tuple[RunEvent, ...]:
        return tuple(self._events)

    # -- transitions ---------------------------------------------------------

    def transition(self, next_phase: RunPhase) -> RunEvent:
        if self._phase in _TERMINAL:
            raise InvalidTransitionError(
                f"{self._phase.value} is terminal; cannot move to {next_phase.value}"
            )
        expected = _FORWARD.get(self._phase)
        if next_phase is not expected:
            raise InvalidTransitionError(
                f"invalid transition {self._phase.value} -> {next_phase.value}"
            )
        event = self._append_event(
            phase=next_phase,
            event_type="phase_transition",
            details={"from": self._phase.value},
        )
        self._phase = next_phase
        return event

    def abort(self, reason: str, catastrophic: bool) -> RunEvent:
        if self._phase not in _ABORTABLE:
            raise InvalidTransitionError(
                f"cannot abort from {self._phase.value}; abort is only "
                "allowed from an active successful-path phase"
            )
        event = self._append_event(
            phase=RunPhase.ABORTED,
            event_type="run_aborted",
            catastrophic=catastrophic,
            details={"from": self._phase.value, "reason": reason},
        )
        self._phase = RunPhase.ABORTED
        return event

    # -- preflight -------------------------------------------------------------

    def _preflight_failures(self) -> List[str]:
        manifest = self._manifest
        if not isinstance(manifest, dict):
            return ["manifest is not a JSON object"]

        failures: List[str] = []
        if manifest.get("eligible_for_execution") is not True:
            failures.append("eligible_for_execution is not true")
        if "capture_errors" not in manifest:
            failures.append("capture_errors missing")
        elif manifest["capture_errors"]:
            failures.append("capture_errors is not empty")
        manifest_id = manifest.get("manifest_id")
        if not isinstance(manifest_id, str) or not _MANIFEST_ID_RE.match(manifest_id):
            failures.append("manifest_id missing or invalid")
        manifest_sha = manifest.get("manifest_sha256")
        if not isinstance(manifest_sha, str) or not _MANIFEST_SHA_RE.match(manifest_sha):
            failures.append("manifest_sha256 missing or invalid")

        source = manifest.get("source") or {}
        if source.get("working_tree_clean") is not True:
            failures.append("source.working_tree_clean is not true")

        if self._manifest_verifier is not None:
            drift = list(self._manifest_verifier(manifest))
            if drift:
                failures.append(f"runtime manifest drift: {'; '.join(drift)}")

        hardware = manifest.get("hardware") or {}
        topology = (
            ("physical_cores", REFERENCE_PHYSICAL_CORES),
            ("logical_threads", REFERENCE_LOGICAL_THREADS),
            ("ram_bytes", REFERENCE_RAM_BYTES),
            ("link_speed_mbps", REFERENCE_LINK_SPEED_MBPS),
        )
        for field, expected in topology:
            if hardware.get(field) != expected:
                failures.append(
                    f"hardware.{field} expected {expected} got {hardware.get(field)!r}"
                )

        operating_system = manifest.get("operating_system") or {}
        if operating_system.get("distribution") != REFERENCE_DISTRIBUTION:
            failures.append(
                "operating_system.distribution expected "
                f"{REFERENCE_DISTRIBUTION} got {operating_system.get('distribution')!r}"
            )
        if operating_system.get("distribution_version") != REFERENCE_DISTRIBUTION_VERSION:
            failures.append(
                "operating_system.distribution_version expected "
                f"{REFERENCE_DISTRIBUTION_VERSION} got "
                f"{operating_system.get('distribution_version')!r}"
            )

        rathena_configuration = manifest.get("rathena_configuration") or {}
        interval = rathena_configuration.get("snapshot_interval_seconds")
        if interval != REFERENCE_SNAPSHOT_INTERVAL_SECONDS:
            failures.append(
                "rathena_configuration.snapshot_interval_seconds expected "
                f"{REFERENCE_SNAPSHOT_INTERVAL_SECONDS} got {interval!r}"
            )
        for field in ("slow_sql_threshold", "slow_script_threshold"):
            value = rathena_configuration.get(field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                failures.append(
                    f"rathena_configuration.{field} missing or invalid: {value!r}"
                )
        return failures

    def run_preflight(self) -> Tuple[RunEvent, ...]:
        if self._phase is not RunPhase.ENVIRONMENT_CHECK:
            raise InvalidTransitionError(
                f"preflight requires ENVIRONMENT_CHECK, got {self._phase.value}"
            )
        if self._preflight_passed:
            raise LifecycleError("preflight already passed")
        failures = self._preflight_failures()
        if failures:
            self._append_event(
                phase=RunPhase.ENVIRONMENT_CHECK,
                event_type="preflight_failed",
                catastrophic=True,
                details={"failures": tuple(failures)},
            )
            self.abort("preflight failed", catastrophic=True)
            raise CatastrophicRunError(
                "preflight failed: " + "; ".join(failures)
            )
        self._preflight_passed = True
        event = self._append_event(
            phase=RunPhase.ENVIRONMENT_CHECK,
            event_type="preflight_passed",
        )
        return (event,)

    # -- service start ---------------------------------------------------------

    def _threshold_environment(self) -> Dict[str, str]:
        rathena_configuration = self._manifest["rathena_configuration"]
        return {
            SQL_THRESHOLD_ENV: str(rathena_configuration["slow_sql_threshold"]),
            SCRIPT_THRESHOLD_ENV: str(rathena_configuration["slow_script_threshold"]),
        }

    def run_service_start(
        self, commands: Tuple[RunCommand, ...]
    ) -> Tuple[RunEvent, ...]:
        if not self._preflight_passed:
            raise LifecycleError("service start requires a passed preflight")
        self.transition(RunPhase.SERVICE_START)
        thresholds = self._threshold_environment()
        events: List[RunEvent] = []
        for command in commands:
            environment = dict(command.environment)
            environment.update(thresholds)  # approved overrides win
            effective = dataclasses.replace(
                command, environment=MappingProxyType(environment)
            )
            events.append(
                self._execute(
                    effective,
                    extra_details={
                        "environment": {
                            SQL_THRESHOLD_ENV: int(thresholds[SQL_THRESHOLD_ENV]),
                            SCRIPT_THRESHOLD_ENV: int(thresholds[SCRIPT_THRESHOLD_ENV]),
                        }
                    },
                )
            )
        return tuple(events)

    # -- command execution -------------------------------------------------------

    def execute(self, command: RunCommand) -> RunEvent:
        return self._execute(command, extra_details=None)

    def _execute(
        self, command: RunCommand, extra_details: Optional[Mapping[str, Any]]
    ) -> RunEvent:
        started = self._monotonic_ns()
        return_code: Optional[int] = None
        timed_out = False
        stdout_summary = ""
        stderr_summary = ""
        failure: Optional[str] = None
        try:
            completed = self._command_runner(
                argv=command.argv,
                timeout_seconds=command.timeout_seconds,
                cwd=command.cwd,
                environment=command.environment,
            )
            return_code = completed.returncode
            stdout_summary = _summarize(completed.stdout)
            stderr_summary = _summarize(completed.stderr)
            if return_code != 0:
                failure = f"exit code {return_code}"
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            stderr_summary = _summarize(stderr)
            failure = f"timeout after {command.timeout_seconds}s"
        except OSError as exc:
            stderr_summary = _summarize(str(exc).encode("utf-8", errors="replace"))
            failure = f"OS error: {exc}"
        finished = self._monotonic_ns()

        details: Dict[str, Any] = dict(extra_details or {})
        if failure is None:
            return self._append_event(
                phase=self._phase,
                event_type="command_succeeded",
                command_name=command.name,
                started_monotonic_ns=started,
                finished_monotonic_ns=finished,
                return_code=return_code,
                stdout_summary=stdout_summary,
                stderr_summary=stderr_summary,
                details=details,
            )

        event = self._append_event(
            phase=self._phase,
            event_type="command_failed",
            command_name=command.name,
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
            return_code=return_code,
            timed_out=timed_out,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            catastrophic=command.catastrophic_on_failure,
            details=details,
        )
        reason = f"command {command.name} failed: {failure}"
        if command.catastrophic_on_failure:
            self.abort(reason, catastrophic=True)
            raise CatastrophicRunError(reason)
        raise CommandExecutionError(reason)

    # -- duration phases -----------------------------------------------------------

    def _run_duration_phase(self, phase: RunPhase, seconds: int) -> RunEvent:
        self.transition(phase)
        started = self._monotonic_ns()
        self._sleep(seconds)
        finished = self._monotonic_ns()
        return self._append_event(
            phase=phase,
            event_type="duration_phase_completed",
            started_monotonic_ns=started,
            finished_monotonic_ns=finished,
            details={
                "planned_duration_seconds": seconds,
                "actual_duration_ns": finished - started,
            },
        )

    def run_preconditioning(self) -> RunEvent:
        return self._run_duration_phase(
            RunPhase.PRECONDITIONING, self._config.preconditioning_seconds
        )

    def run_ramp_up(self) -> RunEvent:
        return self._run_duration_phase(
            RunPhase.RAMP_UP, self._config.ramp_seconds
        )

    def run_steady_state(self) -> RunEvent:
        return self._run_duration_phase(
            RunPhase.STEADY_STATE, self._config.steady_state_seconds
        )

    def run_cooldown(self) -> RunEvent:
        return self._run_duration_phase(
            RunPhase.COOL_DOWN, self._config.cooldown_seconds
        )

    def run_validation(self) -> RunEvent:
        return self.transition(RunPhase.VALIDATION)

    def run_reporting(self) -> RunEvent:
        return self.transition(RunPhase.REPORTING)

    # -- persistence -----------------------------------------------------------------

    def write_event_log(self, path: Path) -> None:
        """Write the deterministic JSON event log via write_json_atomic."""
        manifest_id = (
            self._manifest.get("manifest_id")
            if isinstance(self._manifest, dict)
            else None
        )
        events = [
            {
                "sequence": event.sequence,
                "phase": event.phase.value,
                "event_type": event.event_type,
                "command_name": event.command_name,
                "started_monotonic_ns": event.started_monotonic_ns,
                "finished_monotonic_ns": event.finished_monotonic_ns,
                "return_code": event.return_code,
                "timed_out": event.timed_out,
                "stdout_summary": event.stdout_summary,
                "stderr_summary": event.stderr_summary,
                "catastrophic": event.catastrophic,
                "details": _plain(event.details),
            }
            for event in self._events
        ]
        payload = {
            "version": 1,
            "manifest_id": manifest_id,
            "final_phase": self._phase.value,
            "events": events,
        }
        write_json_atomic(Path(path), payload)
