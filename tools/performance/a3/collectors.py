"""A3 telemetry collector process management.

Manages the four approved 5-second system collectors (pidstat, sar, vmstat,
iostat) as child processes whose output is redirected into the run artifact
directory. subprocess.Popen is used only inside the default process factory,
always with argument arrays, ``shell=False``, ``stdin=DEVNULL``, and a
minimal allowlisted environment whose values are never logged.
"""

import dataclasses
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from tools.performance.a3.io import write_json_atomic
from tools.performance.a3.models import RunPhase

SAMPLING_INTERVAL_SECONDS = 5
TERMINATE_WAIT_SECONDS = 5
KILL_WAIT_SECONDS = 2

ALLOWED_ENVIRONMENT_NAMES = ("PATH", "LANG", "LC_ALL", "TZ")

COLLECTORS_DIR_NAME = "collectors"

_IDENTIFIER_MAX_LENGTH = 128


class CollectorError(Exception):
    """Base class for collector management errors."""


class CollectorStartError(CollectorError):
    """Raised when collector startup fails."""


class CollectorStopError(CollectorError):
    """Raised after stop attempts when at least one collector failed."""


@dataclasses.dataclass(frozen=True)
class CollectorSpec:
    name: str
    argv: Tuple[str, ...]
    stdout_name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))


@dataclasses.dataclass(frozen=True)
class CollectorProcess:
    """Live collector process state (handles owned by the controller)."""

    spec: CollectorSpec
    process: Any
    stdout_handle: Any
    stderr_handle: Any
    stdout_path: Path
    stderr_path: Path


@dataclasses.dataclass(frozen=True)
class CollectorRecord:
    """Immutable lifecycle record of one collector."""

    name: str
    argv: Tuple[str, ...]
    stdout_path: str
    stderr_path: str
    pid: Optional[int]
    started_monotonic_ns: Optional[int]
    stopped_monotonic_ns: Optional[int]
    return_code: Optional[int]
    graceful_termination_requested: bool
    forced_kill_used: bool
    start_error: Optional[str]
    stop_error: Optional[str]


DEFAULT_COLLECTORS: Tuple[CollectorSpec, ...] = (
    CollectorSpec(
        name="pidstat",
        argv=("pidstat", "-h", "-r", "-u", "-w", "-p", "ALL", "5"),
        stdout_name="pidstat.log",
    ),
    CollectorSpec(
        name="sar",
        argv=("sar", "-u", "-r", "-n", "DEV,TCP,ETCP", "5"),
        stdout_name="sar.log",
    ),
    CollectorSpec(
        name="vmstat",
        argv=("vmstat", "-t", "5"),
        stdout_name="vmstat.log",
    ),
    CollectorSpec(
        name="iostat",
        argv=("iostat", "-x", "-d", "5"),
        stdout_name="iostat.log",
    ),
)


def _validate_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > _IDENTIFIER_MAX_LENGTH:
        raise ValueError(f"{field} exceeds {_IDENTIFIER_MAX_LENGTH} characters")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL bytes")
    if ".." in value:
        raise ValueError(f"{field} must not contain '..'")
    for char in value:
        if not (char.isascii() and (char.isalnum() or char in ".-_")):
            raise ValueError(f"{field} contains invalid character {char!r}")
    return value


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Identifiers and artifact location for one A3 run."""

    baseline_cycle_id: str
    manifest_id: str
    run_id: str
    artifact_root: Path
    phase: RunPhase

    def __post_init__(self) -> None:
        _validate_identifier(self.baseline_cycle_id, "baseline_cycle_id")
        _validate_identifier(self.manifest_id, "manifest_id")
        _validate_identifier(self.run_id, "run_id")
        object.__setattr__(self, "artifact_root", Path(self.artifact_root))


def _minimal_environment(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Copy only allowlisted names; values are never logged."""
    source = os.environ if environ is None else environ
    return {
        name: source[name] for name in ALLOWED_ENVIRONMENT_NAMES if name in source
    }


def _popen_platform_kwargs(os_name: str) -> Dict[str, Any]:
    if os_name == "posix":
        return {"start_new_session": True, "close_fds": True}
    kwargs: Dict[str, Any] = {"close_fds": True}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", None)
    if flags is not None:
        kwargs["creationflags"] = flags
    return kwargs


def _default_process_factory(
    spec: CollectorSpec, stdout_path: Path, stderr_path: Path
) -> CollectorProcess:
    """Default factory: the only place subprocess.Popen is invoked."""
    stdout_handle = open(stdout_path, "wb")
    stderr_handle = open(stderr_path, "wb")
    try:
        process = subprocess.Popen(
            list(spec.argv),
            shell=False,
            cwd=None,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=_minimal_environment(),
            **_popen_platform_kwargs(os.name),
        )
    except BaseException:
        stdout_handle.close()
        stderr_handle.close()
        raise
    return CollectorProcess(
        spec=spec,
        process=process,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        stdout_path=Path(stdout_path),
        stderr_path=Path(stderr_path),
    )


class CollectorController:
    """Starts, stops, and records the A3 telemetry collectors."""

    def __init__(
        self,
        specs: Tuple[CollectorSpec, ...] = DEFAULT_COLLECTORS,
        process_factory: Optional[Callable] = None,
        monotonic_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        self._specs = tuple(specs)
        names = [spec.name for spec in self._specs]
        if len(names) != len(set(names)):
            raise CollectorError("collector names must be unique")
        self._factory = process_factory or _default_process_factory
        self._monotonic_ns = monotonic_ns or time.monotonic_ns
        self._context: Optional[RunContext] = None
        self._processes: List[CollectorProcess] = []
        self._records: Tuple[CollectorRecord, ...] = ()
        self._started = False
        self._stopped = False
        self._stop_order: List[str] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self, run_context: RunContext) -> Tuple[CollectorRecord, ...]:
        if self._started:
            raise CollectorStartError("collectors already started")
        artifact_root = Path(run_context.artifact_root)
        collector_dir = artifact_root / COLLECTORS_DIR_NAME
        if artifact_root.is_symlink() or collector_dir.is_symlink():
            raise CollectorStartError(
                "artifact root or collector directory must not be a symlink"
            )
        collector_dir.mkdir(parents=True, exist_ok=True)

        self._context = run_context
        self._started = True
        records: List[CollectorRecord] = []
        try:
            for spec in self._specs:
                stdout_path = collector_dir / spec.stdout_name
                stderr_path = collector_dir / f"{spec.name}.stderr.log"
                process = self._factory(
                    spec=spec,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
                self._processes.append(process)
                records.append(
                    CollectorRecord(
                        name=spec.name,
                        argv=spec.argv,
                        stdout_path=self._relative(stdout_path),
                        stderr_path=self._relative(stderr_path),
                        pid=process.process.pid,
                        started_monotonic_ns=self._monotonic_ns(),
                        stopped_monotonic_ns=None,
                        return_code=None,
                        graceful_termination_requested=False,
                        forced_kill_used=False,
                        start_error=None,
                        stop_error=None,
                    )
                )
        except Exception as exc:
            records.append(
                CollectorRecord(
                    name=spec.name,
                    argv=spec.argv,
                    stdout_path=self._relative(stdout_path),
                    stderr_path=self._relative(stderr_path),
                    pid=None,
                    started_monotonic_ns=None,
                    stopped_monotonic_ns=None,
                    return_code=None,
                    graceful_termination_requested=False,
                    forced_kill_used=False,
                    start_error=str(exc),
                    stop_error=None,
                )
            )
            self._records = tuple(records)
            self._stop_all(record_failures=False)
            raise CollectorStartError(
                f"collector {spec.name} failed to start: {exc}"
            ) from exc
        self._records = tuple(records)
        return self._records

    def stop(self) -> Tuple[CollectorRecord, ...]:
        """Stop all collectors; idempotent after the first successful pass."""
        if not self._started or self._stopped:
            return self._records
        self._stop_all(record_failures=True)
        self._stopped = True
        if self._stop_errors:
            raise CollectorStopError("; ".join(self._stop_errors))
        return self._records

    # -- internals ---------------------------------------------------------

    def _relative(self, path: Path) -> str:
        artifact_root = Path(self._context.artifact_root)
        try:
            return Path(path).relative_to(artifact_root).as_posix()
        except ValueError:
            raise CollectorError(f"path escapes artifact root: {path}") from None

    def _stop_all(self, record_failures: bool) -> None:
        self._stop_errors: List[str] = []
        new_records: Dict[str, CollectorRecord] = {
            record.name: record for record in self._records
        }
        for process in reversed(self._processes):
            name = process.spec.name
            self._stop_order.append(name)
            stop_ns = self._monotonic_ns()
            graceful = False
            forced = False
            stop_error: Optional[str] = None
            return_code: Optional[int] = None
            try:
                if process.process.poll() is None:
                    graceful = True
                    process.process.terminate()
                    try:
                        process.process.wait(timeout=TERMINATE_WAIT_SECONDS)
                    except subprocess.TimeoutExpired:
                        forced = True
                        process.process.kill()
                        try:
                            process.process.wait(timeout=KILL_WAIT_SECONDS)
                        except subprocess.TimeoutExpired:
                            raise CollectorError(
                                f"collector {name} did not exit after kill"
                            )
                return_code = process.process.poll()
            except Exception as exc:  # noqa: BLE001 - aggregate and continue
                stop_error = str(exc)
                self._stop_errors.append(f"{name}: {exc}")
            finally:
                for handle in (process.stdout_handle, process.stderr_handle):
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001 - best effort close
                        pass
            previous = new_records.get(name)
            new_records[name] = dataclasses.replace(
                previous,
                stopped_monotonic_ns=stop_ns,
                return_code=return_code,
                graceful_termination_requested=graceful,
                forced_kill_used=forced,
                stop_error=stop_error,
            )
        order = {spec.name: index for index, spec in enumerate(self._specs)}
        self._records = tuple(
            sorted(new_records.values(), key=lambda record: order[record.name])
        )

    # -- reporting ---------------------------------------------------------

    def records(self) -> Tuple[CollectorRecord, ...]:
        return self._records

    def write_metadata(self, path: Path) -> None:
        if self._context is None:
            raise CollectorError("collectors were never started")
        collectors = []
        for record in self._records:
            self._validate_relative(record.stdout_path)
            self._validate_relative(record.stderr_path)
            collectors.append(
                {
                    "name": record.name,
                    "argv": list(record.argv),
                    "stdout_path": record.stdout_path,
                    "stderr_path": record.stderr_path,
                    "pid": record.pid,
                    "started_monotonic_ns": record.started_monotonic_ns,
                    "stopped_monotonic_ns": record.stopped_monotonic_ns,
                    "return_code": record.return_code,
                    "graceful_termination_requested": record.graceful_termination_requested,
                    "forced_kill_used": record.forced_kill_used,
                    "start_error": record.start_error,
                    "stop_error": record.stop_error,
                }
            )
        payload = {
            "version": 1,
            "baseline_cycle_id": self._context.baseline_cycle_id,
            "manifest_id": self._context.manifest_id,
            "run_id": self._context.run_id,
            "sampling_interval_seconds": SAMPLING_INTERVAL_SECONDS,
            "collectors": collectors,
        }
        write_json_atomic(Path(path), payload)

    @staticmethod
    def _validate_relative(path: str) -> None:
        if path.startswith("/") or "\\" in path or ".." in path.split("/"):
            raise CollectorError(f"path escapes artifact root: {path}")
