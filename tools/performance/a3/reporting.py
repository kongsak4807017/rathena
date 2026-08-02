"""A3 artifact generation, Markdown reporting, and Grafana validation.

Writes deterministic per-run and per-cycle artifact trees under
``artifacts/performance/a3/<baseline_cycle_id>/`` with atomic writes,
byte-for-byte streamed source copies, and SHA-256 checksum manifests written
last. Structured artifacts are UTF-8 JSON with sorted keys, no NaN/Infinity,
and no secret markers. Raw copied logs are never scanned, redacted, or
embedded into reports.
"""

import dataclasses
import json
import math
import os
import shutil
import tempfile
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from tools.performance.a3.io import sha256_file
from tools.performance.a3.models import CapacityVerdict, MetricVerdict, RunPhase
from tools.performance.a3.scaling import (
    CapacityResult,
    LevelAggregation,
    RegressionResult,
    ScalingResult,
)
from tools.performance.a3.slo import _METRIC_SPECS, RunSLOResult

MAX_SOURCE_FILE_BYTES = 256 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024

SECRET_MARKERS = (
    "password",
    "token",
    "secret",
    "api_key",
    "private_key",
    "authorization",
    "bearer",
)

FORMULA_PREFIXES = ("=", "+", "-", "@")

TIMESERIES_COLUMNS = (
    "timestamp",
    "phase",
    "active_users",
    "cpu_percent",
    "memory_rss_bytes",
    "tick_latency_ms",
    "packet_processing_ms",
    "sql_latency_ms",
    "script_latency_ms",
    "storage_utilization_percent",
    "storage_await_ms",
    "network_utilization_percent",
)

WORKLOAD_COLUMNS = (
    "timestamp",
    "phase",
    "active_users",
    "category",
    "event_count",
    "error_count",
)

WORKLOAD_CATEGORIES = (
    "movement_direction_changes",
    "idle_heartbeat",
    "combat",
    "npc_interaction",
    "item_inventory",
    "map_change_warp",
    "chat",
    "login_logout_character_select",
)

SOURCE_FILE_KEYS = (
    "collectors/collectors.json",
    "collectors/pidstat.log",
    "collectors/pidstat.stderr.log",
    "collectors/sar.log",
    "collectors/sar.stderr.log",
    "collectors/vmstat.log",
    "collectors/vmstat.stderr.log",
    "collectors/iostat.log",
    "collectors/iostat.stderr.log",
    "service-logs/login-server.log",
    "service-logs/char-server.log",
    "service-logs/map-server.log",
    "event-log.json",
)

REQUIRED_RUN_PAYLOAD_FIELDS = (
    "version",
    "baseline_cycle_id",
    "run_id",
    "manifest_id",
    "load_level",
    "run_number",
    "validity",
    "final_phase",
    "artifact_status",
    "created_utc",
)

REQUIRED_SUMMARY_PAYLOAD_FIELDS = (
    "version",
    "run_id",
    "manifest_id",
    "load_level",
    "verdict",
    "valid",
)

REQUIRED_PANEL_TITLES = (
    "A3 Overview",
    "CPU",
    "Memory",
    "Tick Latency",
    "Packet Processing",
    "SQL",
    "Script",
    "Storage",
    "Network",
    "Scaling",
)

REQUIRED_VARIABLES = ("baseline_cycle_id", "manifest_id", "run_id", "load_level")

PLACEHOLDERS = {
    "${A3_BASELINE_CYCLE_ID}": "baseline_cycle_id",
    "${A3_MANIFEST_ID}": "manifest_id",
    "${A3_RUN_ID}": "run_id",
}

_PHASE_VALUES = {phase.value for phase in RunPhase}
_LOAD_LEVELS = (500, 1000, 2500, 5000)


class ArtifactError(Exception):
    """Raised when artifact safety or structure requirements are violated."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ArtifactEntry:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclasses.dataclass(frozen=True)
class RunArtifactResult:
    baseline_cycle_id: str
    run_id: str
    run_directory: str
    files: Tuple[ArtifactEntry, ...]
    checksums_path: str
    complete: bool


@dataclasses.dataclass(frozen=True)
class CycleReportResult:
    baseline_cycle_id: str
    cycle_directory: str
    technical_report_path: str
    executive_summary_path: str
    comparison_csv_path: str
    artifact_index_path: str
    files: Tuple[ArtifactEntry, ...]


@dataclasses.dataclass(frozen=True)
class DashboardValidationResult:
    valid: bool
    errors: Tuple[str, ...]
    checked_panels: Tuple[str, ...]
    checked_thresholds: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Safety and serialization helpers
# ---------------------------------------------------------------------------


def _validate_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > 128 or "\x00" in value or ".." in value:
        raise ValueError(f"{field} is not a safe identifier")
    if not all(c.isascii() and (c.isalnum() or c in ".-_") for c in value):
        raise ValueError(f"{field} is not a safe identifier")
    return value


def _reject_symlink(path: Path, description: str) -> None:
    if path.is_symlink():
        raise ArtifactError(f"{description} must not be a symlink: {path}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite float is not JSON-safe: {value!r}")
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        return value
    raise ValueError(f"value of type {type(value).__name__} is not JSON-safe")


def _assert_no_secret_markers(value: Any, context: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_no_secret_markers(str(key), context)
            _assert_no_secret_markers(item, context)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _assert_no_secret_markers(item, context)
    elif isinstance(value, str):
        lowered = value.lower()
        for marker in SECRET_MARKERS:
            if marker in lowered:
                raise ValueError(
                    f"secret marker {marker!r} found in structured artifact ({context})"
                )


def _assert_no_absolute_paths(value: Any, context: str) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_no_absolute_paths(item, context)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _assert_no_absolute_paths(item, context)
    elif isinstance(value, str):
        if value.startswith("/") or (len(value) > 2 and value[1] == ":" and value[2] in "\\/"):
            raise ArtifactError(f"host-local absolute path in {context}: {value!r}")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_json(path: Path, payload: Any, context: str) -> None:
    safe = _json_safe(payload)
    _assert_no_secret_markers(safe, context)
    text = json.dumps(safe, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    _atomic_write_bytes(path, (text + "\n").encode("utf-8"))


def _stream_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as inp:
            while True:
                chunk = inp.read(_COPY_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, destination)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _cycle_dir(artifact_root: Path, baseline_cycle_id: str) -> Path:
    return artifact_root / "artifacts" / "performance" / "a3" / baseline_cycle_id


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise ArtifactError(f"path escapes artifact root: {path}") from None


def _checksum_entries(directory: Path, skip_prefixes: Tuple[str, ...] = ()) -> Tuple[ArtifactEntry, ...]:
    entries: List[ArtifactEntry] = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if relative == "checksums.json" or any(relative.startswith(p) for p in skip_prefixes):
            continue
        if path.is_symlink():
            raise ArtifactError(f"symlink found in artifact tree: {relative}")
        if not path.is_file():
            continue
        entries.append(
            ArtifactEntry(relative, sha256_file(path), path.stat().st_size)
        )
    entries.sort(key=lambda entry: entry.relative_path)
    return tuple(entries)


def _write_checksums(directory: Path, baseline_cycle_id: str, run_id: Optional[str], skip_prefixes: Tuple[str, ...] = ()) -> Tuple[ArtifactEntry, ...]:
    entries = _checksum_entries(directory, skip_prefixes)
    payload = {
        "version": 1,
        "baseline_cycle_id": baseline_cycle_id,
        "run_id": run_id,
        "files": [
            {
                "path": entry.relative_path,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ],
    }
    _write_json(directory / "checksums.json", payload, "checksums.json")
    return entries


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _csv_cell(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _validate_row_strings(row: Mapping, columns: Sequence[str], context: str) -> None:
    for column in columns:
        value = row.get(column)
        if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
            raise ArtifactError(f"formula-injection cell in {context}.{column}")


def _write_timeseries_csv(path: Path, rows: Sequence[Mapping]) -> None:
    validated: List[Mapping] = []
    for row in rows:
        if not _is_number(row.get("timestamp")):
            raise ArtifactError("timeseries timestamp must be a finite number")
        phase = row.get("phase")
        if phase not in _PHASE_VALUES:
            raise ArtifactError(f"timeseries phase is not an approved RunPhase: {phase!r}")
        if not _is_nonneg_int(row.get("active_users")):
            raise ArtifactError("timeseries active_users must be a non-negative integer")
        for column in TIMESERIES_COLUMNS[3:]:
            if not _is_number(row.get(column)):
                raise ArtifactError(f"timeseries {column} must be a finite number")
        _validate_row_strings(row, TIMESERIES_COLUMNS, "timeseries")
        validated.append(row)
    validated.sort(key=lambda row: row["timestamp"])
    lines = [",".join(TIMESERIES_COLUMNS)]
    for row in validated:
        lines.append(",".join(_csv_cell(row[column]) for column in TIMESERIES_COLUMNS))
    _atomic_write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _write_workload_csv(path: Path, rows: Sequence[Mapping]) -> None:
    validated: List[Mapping] = []
    for row in rows:
        if not _is_number(row.get("timestamp")):
            raise ArtifactError("workload timestamp must be a finite number")
        phase = row.get("phase")
        if phase not in _PHASE_VALUES:
            raise ArtifactError(f"workload phase is not an approved RunPhase: {phase!r}")
        if not _is_nonneg_int(row.get("active_users")):
            raise ArtifactError("workload active_users must be a non-negative integer")
        if row.get("category") not in WORKLOAD_CATEGORIES:
            raise ArtifactError(f"workload category is not approved: {row.get('category')!r}")
        if not _is_nonneg_int(row.get("event_count")) or not _is_nonneg_int(row.get("error_count")):
            raise ArtifactError("workload counts must be non-negative integers")
        _validate_row_strings(row, WORKLOAD_COLUMNS, "workload")
        validated.append(row)
    validated.sort(key=lambda row: (row["timestamp"], row["category"]))
    lines = [",".join(WORKLOAD_COLUMNS)]
    for row in validated:
        lines.append(",".join(_csv_cell(row[column]) for column in WORKLOAD_COLUMNS))
    _atomic_write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Payload serializers
# ---------------------------------------------------------------------------


def _serialize_slo_result(slo_result: Any) -> Dict[str, Any]:
    if isinstance(slo_result, RunSLOResult):
        return {
            "status": slo_result.status.value,
            "evaluations": [
                {
                    "metric": e.metric,
                    "statistic": e.statistic,
                    "observed": e.observed,
                    "threshold": e.threshold,
                    "warning_threshold": e.warning_threshold,
                    "verdict": e.verdict.value,
                    "code": e.code,
                    "message": e.message,
                    "catastrophic": e.catastrophic,
                    "details": dict(e.details),
                }
                for e in slo_result.evaluations
            ],
            "catastrophic_signals": [
                dataclasses.asdict(signal) for signal in slo_result.catastrophic_signals
            ],
            "evaluated_metrics": list(slo_result.evaluated_metrics),
            "blocked_metrics": list(slo_result.blocked_metrics),
        }
    if isinstance(slo_result, Mapping):
        return dict(slo_result)
    raise ArtifactError(f"unsupported slo_result type: {type(slo_result).__name__}")


def _serialize_anomalies(run_id: Optional[str], anomalies: Sequence[Mapping]) -> Dict[str, Any]:
    entries = [dict(anomaly) for anomaly in anomalies]
    entries.sort(
        key=lambda a: (
            str(a.get("severity")),
            str(a.get("code")),
            float(a.get("timestamp", 0)),
            str(a.get("message")),
        )
    )
    payload: Dict[str, Any] = {"version": 1, "anomalies": entries}
    if run_id is not None:
        payload["run_id"] = run_id
    return payload


def _serialize_prometheus_queries(run_id: str, queries_payload: Mapping) -> Dict[str, Any]:
    if queries_payload.get("step") != 5:
        raise ArtifactError("prometheus-queries step must be exactly 5")
    payload = {
        "version": 1,
        "run_id": run_id,
        "start": queries_payload.get("start"),
        "end": queries_payload.get("end"),
        "step": 5,
        "queries": queries_payload.get("queries", []),
    }
    for query in payload["queries"]:
        if not isinstance(query, Mapping):
            raise ArtifactError("prometheus query entries must be objects")
        url = query.get("url")
        if isinstance(url, str) and "://" in url:
            authority = url.split("://", 1)[1].split("/", 1)[0]
            if "@" in authority:
                raise ArtifactError("prometheus query URL must not contain credentials")
    return payload


# ---------------------------------------------------------------------------
# Run artifacts
# ---------------------------------------------------------------------------


def write_run_artifacts(
    artifact_root: Path,
    baseline_cycle_id: str,
    run_payload: dict,
    summary_payload: dict,
    timeseries_rows: Sequence[Mapping],
    workload_rows: Sequence[Mapping],
    slo_result: Any,
    anomalies: Sequence[Mapping],
    prometheus_queries: Mapping,
    source_files: Mapping[str, Path],
    overwrite: bool = False,
) -> RunArtifactResult:
    """Write one deterministic run artifact directory (checksums last)."""
    artifact_root = Path(artifact_root)
    _validate_identifier(baseline_cycle_id, "baseline_cycle_id")
    run_id = run_payload.get("run_id") if isinstance(run_payload, Mapping) else None
    _validate_identifier(run_id, "run_id")
    _reject_symlink(artifact_root, "artifact root")

    cycle_dir = _cycle_dir(artifact_root, baseline_cycle_id)
    run_dir = cycle_dir / "runs" / run_id
    _reject_symlink(cycle_dir, "cycle directory")
    _reject_symlink(run_dir, "run directory")
    if run_dir.exists() and not overwrite:
        raise ArtifactError(f"run directory already exists: {run_dir}")

    missing = [field for field in REQUIRED_RUN_PAYLOAD_FIELDS if field not in run_payload]
    if missing:
        raise ArtifactError(f"run_payload missing required fields: {', '.join(missing)}")
    missing_summary = [field for field in REQUIRED_SUMMARY_PAYLOAD_FIELDS if field not in summary_payload]
    if missing_summary:
        raise ArtifactError(f"summary_payload missing required fields: {', '.join(missing_summary)}")
    _assert_no_absolute_paths(run_payload, "run.json")
    _assert_no_absolute_paths(summary_payload, "summary.json")

    unknown_keys = sorted(set(source_files) - set(SOURCE_FILE_KEYS))
    if unknown_keys:
        raise ArtifactError(f"unknown source file keys: {', '.join(unknown_keys)}")
    missing_keys = [key for key in SOURCE_FILE_KEYS if key not in source_files]
    if missing_keys:
        raise ArtifactError(f"missing source file keys: {', '.join(missing_keys)}")

    resolved_root = artifact_root.resolve()
    sources: Dict[str, Path] = {}
    for key in SOURCE_FILE_KEYS:
        source = Path(source_files[key])
        _reject_symlink(source, f"source file {key}")
        if not source.is_file():
            raise ArtifactError(f"source file missing or not regular: {key}")
        if source.stat().st_size > MAX_SOURCE_FILE_BYTES:
            raise ArtifactError(f"source file exceeds 256 MiB: {key}")
        try:
            source.resolve().relative_to(resolved_root)
        except ValueError:
            raise ArtifactError(f"source file outside permitted source root: {key}") from None
        sources[key] = source

    run_dir.mkdir(parents=True, exist_ok=overwrite)

    _write_json(run_dir / "run.json", run_payload, "run.json")
    _write_json(run_dir / "summary.json", summary_payload, "summary.json")
    _write_timeseries_csv(run_dir / "timeseries.csv", timeseries_rows)
    _write_workload_csv(run_dir / "workload.csv", workload_rows)
    _write_json(run_dir / "slo-verdict.json", _serialize_slo_result(slo_result), "slo-verdict.json")
    _write_json(run_dir / "anomalies.json", _serialize_anomalies(run_id, anomalies), "anomalies.json")
    _write_json(
        run_dir / "prometheus-queries.json",
        _serialize_prometheus_queries(run_id, prometheus_queries),
        "prometheus-queries.json",
    )
    for key in SOURCE_FILE_KEYS:
        _stream_copy(sources[key], run_dir / key)

    entries = _write_checksums(run_dir, baseline_cycle_id, run_id)
    return RunArtifactResult(
        baseline_cycle_id=baseline_cycle_id,
        run_id=run_id,
        run_directory=_relative(artifact_root, run_dir),
        files=entries,
        checksums_path=_relative(artifact_root, run_dir / "checksums.json"),
        complete=True,
    )


# ---------------------------------------------------------------------------
# Cycle reports
# ---------------------------------------------------------------------------


def _a4_readiness(capacity: CapacityResult) -> str:
    if (
        capacity.verdict is CapacityVerdict.PASS
        and capacity.safe_capacity == 5000
        and capacity.tested_ceiling == 5000
    ):
        return "READY"
    if capacity.verdict is CapacityVerdict.PASS_WITH_WARNING or (
        capacity.safe_capacity is not None
    ):
        return "CONDITIONAL"
    return "NOT READY"


def _scaling_failures(scaling: ScalingResult) -> List[str]:
    return [
        f"{check.code} ({check.from_level} -> {check.to_level} {check.metric}): {check.message}"
        for check in scaling.checks
        if not check.passed
    ]


def _regression_failures(regression: RegressionResult) -> List[str]:
    return [
        f"{check.code} (level {check.load_level} {check.metric}): {check.message}"
        for check in regression.checks
        if not check.passed
    ]


def _level_table_rows(levels: Sequence[LevelAggregation]) -> List[str]:
    by_level = {level.load_level: level for level in levels}
    rows = ["| Load Level | Valid Runs | Verdict | Median p95 | Worst p95 | Throughput | Error Rate |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for level in _LOAD_LEVELS:
        agg = by_level.get(level)
        if agg is None:
            rows.append(f"| {level} | 0 | BLOCKED |  |  |  |  |")
            continue
        medians = agg.median_metrics
        worst = agg.worst_metrics
        rows.append(
            f"| {level} | {agg.valid_run_count} | {agg.verdict.value} | "
            f"{medians.get('latency_p95_ms', '')} | {worst.get('latency_p95_ms', '')} | "
            f"{medians.get('throughput_per_second', '')} | {medians.get('error_rate', '')} |"
        )
    return rows


def _render_technical_report(
    baseline_cycle_id: str,
    manifest: Mapping,
    levels: Sequence[LevelAggregation],
    scaling: ScalingResult,
    regression: RegressionResult,
    capacity: CapacityResult,
    controls: Mapping,
    dataset_summary: Mapping,
    anomalies: Sequence[Mapping],
    recommendations: Mapping,
    readiness: str,
) -> str:
    source = manifest.get("source", {})
    hardware = manifest.get("hardware", {})
    operating_system = manifest.get("operating_system", {})
    database = manifest.get("database", {})
    bottleneck = controls.get("primary_bottleneck")
    scaling_failures = _scaling_failures(scaling)
    regression_failures = _regression_failures(regression)
    row_counts = dataset_summary.get("row_counts", {})

    lines = [
        "# A3 Technical Baseline Report",
        "",
        "## Executive Result",
        "",
        f"- Baseline cycle: {baseline_cycle_id}",
        f"- Capacity verdict: {capacity.verdict.value}",
        f"- Safe Capacity: {capacity.safe_capacity}",
        f"- Conditional Capacity: {capacity.conditional_capacity}",
        f"- Tested Ceiling: {capacity.tested_ceiling}",
        f"- First degradation level: {capacity.first_degradation_level}",
        f"- A4 readiness: {readiness}",
        "",
        "## Manifest and Reproducibility",
        "",
        f"- Manifest ID: {manifest.get('manifest_id')}",
        f"- Git SHA: {source.get('git_commit_sha')}",
        f"- Branch: {source.get('branch')}",
        "",
        "## Reference Topology",
        "",
        f"- CPU: {hardware.get('cpu_model')} ({hardware.get('physical_cores')} cores / {hardware.get('logical_threads')} threads)",
        f"- RAM bytes: {hardware.get('ram_bytes')}",
        f"- OS: {operating_system.get('distribution')} {operating_system.get('distribution_version')} (kernel {operating_system.get('kernel_version')})",
        f"- Database: {database.get('mariadb_version')}",
        "",
        "## Synthetic Dataset",
        "",
        f"- Seed: {dataset_summary.get('seed')}",
        f"- Accounts: {row_counts.get('accounts')}, Characters: {row_counts.get('characters')}, Guilds: {row_counts.get('guilds')}, Parties: {row_counts.get('parties')}",
        "- Synthetic data only; no production player data.",
        "",
        "## Control Runs",
        "",
        f"- Idle control: {controls.get('idle', {}).get('verdict')} ({controls.get('idle', {}).get('notes')})",
        f"- WebGL-only control: {controls.get('webgl', {}).get('verdict')} ({controls.get('webgl', {}).get('notes')})",
        "",
        "## Per-Level Results",
        "",
        *_level_table_rows(levels),
        "",
        "## SLO Evaluation",
        "",
        "Per-level verdicts derive from the Task 7 SLO engine; see comparison.csv and per-run slo-verdict.json files.",
        "",
        "## Scaling Analysis",
        "",
        f"- Scaling passed: {scaling.passed}",
        f"- First degradation level: {scaling.first_degradation_level}",
    ]
    if scaling_failures:
        lines.append("- Scaling failures:")
        lines.extend(f"  - {failure}" for failure in scaling_failures)
    else:
        lines.append("- Scaling failures: none")
    lines += [
        "",
        "## Regression Analysis",
        "",
        f"- Regression passed: {regression.passed}",
    ]
    if regression_failures:
        lines.append("- Regression failures:")
        lines.extend(f"  - {failure}" for failure in regression_failures)
    else:
        lines.append("- Regression failures: none")
    lines += [
        "",
        "## Anomalies",
        "",
        f"- Recorded anomalies: {len(anomalies)}",
        "",
        "## Bottleneck Attribution",
        "",
        f"- Primary bottleneck: {bottleneck if bottleneck else 'none identified'}",
        "",
        "## Capacity Determination",
        "",
        f"- Safe Capacity: {capacity.safe_capacity}",
        f"- Conditional Capacity: {capacity.conditional_capacity}",
        f"- Tested Ceiling: {capacity.tested_ceiling}",
        f"- Verdict: {capacity.verdict.value}",
        "",
        "## A4 Readiness",
        "",
        f"- A4 readiness: {readiness}",
        "",
        "## A5 Optimization Recommendations",
        "",
    ]
    for item in recommendations.get("a5", []):
        lines.append(f"- {item}")
    lines += [
        "",
        "## Artifact Integrity and Retention",
        "",
        "- All artifacts carry SHA-256 checksums (see checksums.json files).",
        "- Raw execution artifacts (Prometheus blocks, Linux logs, service logs) are external to Git with a minimum 180-day retention.",
        "- This report is generated from supplied run payloads; it does not itself constitute execution evidence.",
        "",
    ]
    return "\n".join(lines)


def _render_executive_summary(
    capacity: CapacityResult,
    controls: Mapping,
    recommendations: Mapping,
    readiness: str,
) -> str:
    bottleneck = controls.get("primary_bottleneck")
    lines = [
        "# A3 Executive Summary",
        "",
        "## Capacity",
        "",
        f"- Safe Capacity: {capacity.safe_capacity}",
        f"- Conditional Capacity: {capacity.conditional_capacity}",
        f"- Tested Ceiling: {capacity.tested_ceiling}",
        f"- Capacity verdict: {capacity.verdict.value}",
        "",
        "## First Degradation",
        "",
        f"- First degradation level: {capacity.first_degradation_level}",
        "",
        "## Primary Bottleneck",
        "",
        f"- {bottleneck if bottleneck else 'none identified'}",
        "",
        "## A4 Readiness",
        "",
        f"- A4 readiness: {readiness}",
        "",
        "## Required Remediation",
        "",
    ]
    for item in recommendations.get("remediation", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _comparison_csv(
    levels: Sequence[LevelAggregation],
    scaling: ScalingResult,
    regression: RegressionResult,
) -> bytes:
    by_level = {level.load_level: level for level in levels}
    scaling_failed_levels = {check.to_level for check in scaling.checks if not check.passed}
    regression_failed_levels = {check.load_level for check in regression.checks if not check.passed}
    header = (
        "load_level,valid_run_count,verdict,cpu_p95_percent,memory_per_user_bytes,"
        "latency_p95_ms,latency_p99_ms,throughput_per_second,error_rate,"
        "scaling_passed,regression_passed"
    )
    lines = [header]
    for level in _LOAD_LEVELS:
        agg = by_level.get(level)
        scaling_passed = level not in scaling_failed_levels
        regression_passed = level not in regression_failed_levels
        if agg is None:
            lines.append(f"{level},0,BLOCKED,,,,,,{scaling_passed},{regression_passed}")
            continue
        medians = agg.median_metrics
        cells = [
            str(level),
            str(agg.valid_run_count),
            agg.verdict.value,
            _csv_cell(medians.get("cpu_p95_percent", "")) if "cpu_p95_percent" in medians else "",
            _csv_cell(medians.get("memory_per_user_bytes", "")) if "memory_per_user_bytes" in medians else "",
            _csv_cell(medians.get("latency_p95_ms", "")) if "latency_p95_ms" in medians else "",
            _csv_cell(medians.get("latency_p99_ms", "")) if "latency_p99_ms" in medians else "",
            _csv_cell(medians.get("throughput_per_second", "")) if "throughput_per_second" in medians else "",
            _csv_cell(medians.get("error_rate", "")) if "error_rate" in medians else "",
            str(scaling_passed),
            str(regression_passed),
        ]
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_cycle_reports(
    artifact_root: Path,
    baseline_cycle_id: str,
    manifest: dict,
    level_results: Sequence[LevelAggregation],
    scaling_result: ScalingResult,
    regression_result: RegressionResult,
    capacity_result: CapacityResult,
    controls: Mapping,
    dataset_summary: Mapping,
    anomalies: Sequence[Mapping],
    recommendations: Mapping,
    overwrite: bool = False,
) -> CycleReportResult:
    """Write deterministic cycle-level reports and metadata (checksums last)."""
    artifact_root = Path(artifact_root)
    _validate_identifier(baseline_cycle_id, "baseline_cycle_id")
    _reject_symlink(artifact_root, "artifact root")
    cycle_dir = _cycle_dir(artifact_root, baseline_cycle_id)
    _reject_symlink(cycle_dir, "cycle directory")
    index_path = cycle_dir / "artifact-index.json"
    if index_path.exists() and not overwrite:
        raise ArtifactError(f"cycle artifacts already exist: {cycle_dir}")
    cycle_dir.mkdir(parents=True, exist_ok=True)

    manifest_id = manifest.get("manifest_id")
    readiness = _a4_readiness(capacity_result)

    _write_json(cycle_dir / "manifest.json", manifest, "manifest.json")

    technical = _render_technical_report(
        baseline_cycle_id, manifest, level_results, scaling_result,
        regression_result, capacity_result, controls, dataset_summary,
        anomalies, recommendations, readiness,
    )
    _atomic_write_bytes(cycle_dir / "technical-report.md", technical.encode("utf-8"))

    executive = _render_executive_summary(capacity_result, controls, recommendations, readiness)
    _atomic_write_bytes(cycle_dir / "executive-summary.md", executive.encode("utf-8"))

    _atomic_write_bytes(
        cycle_dir / "comparison.csv",
        _comparison_csv(level_results, scaling_result, regression_result),
    )

    _write_json(
        cycle_dir / "capacity.json",
        {
            "version": 1,
            "safe_capacity": capacity_result.safe_capacity,
            "conditional_capacity": capacity_result.conditional_capacity,
            "tested_ceiling": capacity_result.tested_ceiling,
            "verdict": capacity_result.verdict.value,
            "first_degradation_level": capacity_result.first_degradation_level,
            "notes": list(capacity_result.notes),
        },
        "capacity.json",
    )
    _write_json(
        cycle_dir / "scaling.json",
        {
            "version": 1,
            "passed": scaling_result.passed,
            "first_degradation_level": scaling_result.first_degradation_level,
            "checks": [dataclasses.asdict(check) for check in scaling_result.checks],
        },
        "scaling.json",
    )
    _write_json(
        cycle_dir / "regression.json",
        {
            "version": 1,
            "passed": regression_result.passed,
            "compared_levels": list(regression_result.compared_levels),
            "checks": [dataclasses.asdict(check) for check in regression_result.checks],
        },
        "regression.json",
    )
    _write_json(
        cycle_dir / "anomalies.json",
        _serialize_anomalies(None, anomalies),
        "anomalies.json",
    )

    run_checksums = controls.get("run_checksums", {})
    run_numbers = controls.get("run_numbers", {})
    run_entries = []
    for level in sorted(level_results, key=lambda item: item.load_level):
        for position, run_id in enumerate(level.run_ids):
            run_entries.append(
                {
                    "run_id": run_id,
                    "load_level": level.load_level,
                    "run_number": run_numbers.get(run_id, position + 1),
                    "valid": True,
                    "verdict": level.run_verdicts[position].value,
                    "manifest_id": level.manifest_id,
                    "relative_path": f"runs/{run_id}/",
                    "checksums_sha256": run_checksums.get(run_id),
                }
            )
    cycle_files = [
        "manifest.json",
        "technical-report.md",
        "executive-summary.md",
        "comparison.csv",
        "capacity.json",
        "scaling.json",
        "regression.json",
        "anomalies.json",
        "retention.json",
        "grafana-dashboard.json",
        "checksums.json",
    ]
    _write_json(
        index_path,
        {
            "version": 1,
            "baseline_cycle_id": baseline_cycle_id,
            "manifest_id": manifest_id,
            "runs": run_entries,
            "cycle_files": cycle_files,
            "external_raw_artifact_policy": {
                "raw_prometheus_minimum_days": 180,
                "linux_logs_minimum_days": 180,
                "service_logs_minimum_days": 180,
                "external_storage_required": True,
                "note": "Raw execution artifacts are external to Git.",
            },
        },
        "artifact-index.json",
    )

    _write_json(
        cycle_dir / "retention.json",
        {
            "summary_json": "permanent",
            "manifest_json": "permanent",
            "csv": "permanent",
            "grafana_dashboard": "permanent",
            "raw_prometheus_minimum_days": 180,
            "linux_logs_minimum_days": 180,
            "service_logs_minimum_days": 180,
            "external_storage_required": True,
        },
        "retention.json",
    )

    template = json.loads(
        (Path(__file__).resolve().parent / "config" / "grafana-dashboard.json").read_text(encoding="utf-8")
    )
    rendered = render_dashboard_runtime(
        template,
        baseline_cycle_id,
        manifest_id,
        controls.get("dashboard_run_id", "cycle"),
    )
    _write_json(cycle_dir / "grafana-dashboard.json", rendered, "grafana-dashboard.json")

    entries = _write_checksums(cycle_dir, baseline_cycle_id, None, skip_prefixes=("runs/",))
    return CycleReportResult(
        baseline_cycle_id=baseline_cycle_id,
        cycle_directory=_relative(artifact_root, cycle_dir),
        technical_report_path=_relative(artifact_root, cycle_dir / "technical-report.md"),
        executive_summary_path=_relative(artifact_root, cycle_dir / "executive-summary.md"),
        comparison_csv_path=_relative(artifact_root, cycle_dir / "comparison.csv"),
        artifact_index_path=_relative(artifact_root, index_path),
        files=entries,
    )


# ---------------------------------------------------------------------------
# Grafana dashboard validation and rendering
# ---------------------------------------------------------------------------


def _resolve_threshold(thresholds: Mapping, reference: str):
    node: Any = thresholds
    for part in reference.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None, False
        node = node[part]
    return node, True


def validate_dashboard_thresholds(
    dashboard: dict,
    thresholds: dict,
) -> DashboardValidationResult:
    """Validate every a3_threshold_ref against the committed thresholds."""
    errors: List[str] = []
    hard_values: Dict[str, set] = {}
    warning_values: Dict[str, set] = {}
    found_refs: List[str] = []
    panels: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            title = node.get("title")
            if isinstance(title, str):
                panels.add(title)
            if "a3_threshold_ref" in node:
                reference = node["a3_threshold_ref"]
                if not isinstance(reference, str) or not reference:
                    errors.append("threshold line with missing a3_threshold_ref")
                else:
                    found_refs.append(reference)
                    resolved, exists = _resolve_threshold(thresholds, reference)
                    if not exists:
                        errors.append(f"unknown threshold reference {reference}")
                    elif "value" not in node:
                        errors.append(f"threshold line {reference} missing value")
                    elif node.get("warning") is True:
                        warning_values.setdefault(reference, set()).add(node["value"])
                    else:
                        hard_values.setdefault(reference, set()).add(node["value"])
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(dashboard)

    for reference in sorted(hard_values):
        values = hard_values[reference]
        expected, _ = _resolve_threshold(thresholds, reference)
        if len(values) > 1:
            errors.append(f"duplicate conflicting references for {reference}")
        for value in sorted(values, key=str):
            if value != expected:
                errors.append(
                    f"threshold {reference} value {value} does not match config {expected}"
                )

    warning_ratio = thresholds.get("warning_zone_ratio", 0.9)
    for reference in sorted(warning_values):
        expected_hard, _ = _resolve_threshold(thresholds, reference)
        if expected_hard == 0:
            errors.append(
                f"zero-tolerance threshold {reference} must not have warning lines"
            )
            continue
        for value in sorted(warning_values[reference], key=str):
            expected = expected_hard * warning_ratio
            if value != expected:
                errors.append(
                    f"warning {reference} value {value} does not match {expected}"
                )

    required_refs = {".".join(spec.threshold_path) for spec in _METRIC_SPECS}
    for reference in sorted(required_refs - set(found_refs)):
        errors.append(f"required threshold reference {reference} not represented")

    for title in REQUIRED_PANEL_TITLES:
        if title not in panels:
            errors.append(f"required panel {title} missing")
    variables = {
        variable.get("name")
        for variable in (dashboard.get("templating") or {}).get("list", [])
        if isinstance(variable, Mapping)
    }
    for name in REQUIRED_VARIABLES:
        if name not in variables:
            errors.append(f"required variable {name} missing")

    return DashboardValidationResult(
        valid=not errors,
        errors=tuple(sorted(errors)),
        checked_panels=tuple(sorted(panels)),
        checked_thresholds=tuple(sorted(set(found_refs))),
    )


def render_dashboard_runtime(
    template: dict,
    baseline_cycle_id: str,
    manifest_id: str,
    run_id: str,
) -> dict:
    """Replace the three approved placeholders in a deep copy of template."""
    values = {
        "${A3_BASELINE_CYCLE_ID}": _validate_identifier(baseline_cycle_id, "baseline_cycle_id"),
        "${A3_MANIFEST_ID}": _validate_identifier(manifest_id, "manifest_id"),
        "${A3_RUN_ID}": _validate_identifier(run_id, "run_id"),
    }

    def render(node: Any) -> Any:
        if isinstance(node, str):
            for placeholder, value in values.items():
                node = node.replace(placeholder, value)
            return node
        if isinstance(node, Mapping):
            return {key: render(item) for key, item in node.items()}
        if isinstance(node, list):
            return [render(item) for item in node]
        return node

    rendered = render(json.loads(json.dumps(template)))
    text = json.dumps(rendered)
    for placeholder in values:
        if placeholder in text:
            raise ArtifactError(f"unresolved placeholder after rendering: {placeholder}")
    return rendered
