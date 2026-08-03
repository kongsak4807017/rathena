"""A3 orchestration CLI: prepare, control, run, evaluate, report, approve.

argparse-only, standard library only. Every operational dependency is
injected through :class:`CLIDependencies` so unit tests run entirely on
fakes. Exit codes: 0 success, 2 usage/input error, 3 governance/state
refusal, 4 operational dependency failure, 5 catastrophic abort, 10
unexpected internal error. No timestamps, approvals, or supersessions are
ever generated automatically.
"""

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from tools.performance.a3.approval import ApprovalState, is_transition_allowed
from tools.performance.a3.models import CapacityVerdict, MetricVerdict
from tools.performance.a3.scaling import (
    CapacityResult,
    LevelAggregation,
    RegressionResult,
    ScalingResult,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSAL = 3
EXIT_OPERATIONAL = 4
EXIT_CATASTROPHIC = 5
EXIT_INTERNAL = 10

LOAD_LEVELS = (500, 1000, 2500, 5000)
STATE_VERSION = 1
DATASET_SEED = 20260802

SECRET_MARKERS = (
    "password",
    "token",
    "secret",
    "api_key",
    "private_key",
    "authorization",
    "bearer",
)

STATE_FIELDS = (
    "version",
    "state",
    "baseline_cycle_id",
    "manifest_id",
    "config_path",
    "artifact_root",
    "controls",
    "runs",
    "evaluated",
    "reported",
    "approved",
    "catastrophic",
    "last_error",
)


class CLIError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# State model and store
# ---------------------------------------------------------------------------


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    if isinstance(value, (ApprovalState, MetricVerdict, CapacityVerdict)):
        return value.value
    return value


@dataclasses.dataclass(frozen=True)
class CycleState:
    version: int
    state: ApprovalState
    baseline_cycle_id: str
    manifest_id: Optional[str]
    config_path: str
    artifact_root: str
    controls: Mapping[str, Any]
    runs: Tuple[Mapping[str, Any], ...]
    evaluated: bool
    reported: bool
    approved: bool
    catastrophic: bool
    last_error: Optional[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ApprovalState(self.state))
        object.__setattr__(self, "controls", _freeze(self.controls))
        object.__setattr__(self, "runs", tuple(_freeze(r) for r in self.runs))


def _state_to_dict(state: CycleState) -> Dict[str, Any]:
    return {
        "version": state.version,
        "state": state.state.value,
        "baseline_cycle_id": state.baseline_cycle_id,
        "manifest_id": state.manifest_id,
        "config_path": state.config_path,
        "artifact_root": state.artifact_root,
        "controls": _thaw(state.controls),
        "runs": [_thaw(r) for r in state.runs],
        "evaluated": state.evaluated,
        "reported": state.reported,
        "approved": state.approved,
        "catastrophic": state.catastrophic,
        "last_error": state.last_error,
    }


def _check_secret_markers(value: Any, context: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_secret_markers(str(key), context)
            _check_secret_markers(item, context)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_secret_markers(item, context)
    elif isinstance(value, str):
        lowered = value.lower()
        for marker in SECRET_MARKERS:
            if marker in lowered:
                raise CLIError(EXIT_REFUSAL, f"secret marker in {context}")


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


class CycleStateStore:
    """Atomic, deterministic persistence for cycle-state.json."""

    def __init__(self, artifact_root: Path) -> None:
        self._root = Path(artifact_root)

    def path_for(self, baseline_cycle_id: str) -> Path:
        return (
            self._root
            / "artifacts"
            / "performance"
            / "a3"
            / baseline_cycle_id
            / "cycle-state.json"
        )

    def exists(self, baseline_cycle_id: str) -> bool:
        return self.path_for(baseline_cycle_id).is_file()

    def read(self, baseline_cycle_id: str) -> CycleState:
        path = self.path_for(baseline_cycle_id)
        if path.is_symlink():
            raise CLIError(EXIT_REFUSAL, "cycle state file must not be a symlink")
        if not path.is_file():
            raise CLIError(EXIT_REFUSAL, f"cycle not found: {baseline_cycle_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CLIError(EXIT_REFUSAL, f"cycle state is malformed: {exc}") from None
        if not isinstance(payload, dict):
            raise CLIError(EXIT_REFUSAL, "cycle state must be an object")
        if payload.get("version") != STATE_VERSION:
            raise CLIError(EXIT_REFUSAL, "cycle state version must be 1")
        unknown = sorted(set(payload) - set(STATE_FIELDS))
        if unknown:
            raise CLIError(EXIT_REFUSAL, f"cycle state has unknown fields: {', '.join(unknown)}")
        try:
            return CycleState(
                version=payload["version"],
                state=ApprovalState(payload["state"]),
                baseline_cycle_id=payload["baseline_cycle_id"],
                manifest_id=payload.get("manifest_id"),
                config_path=payload["config_path"],
                artifact_root=payload["artifact_root"],
                controls=payload.get("controls", {}),
                runs=tuple(payload.get("runs", [])),
                evaluated=payload["evaluated"],
                reported=payload["reported"],
                approved=payload["approved"],
                catastrophic=payload["catastrophic"],
                last_error=payload.get("last_error"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CLIError(EXIT_REFUSAL, f"cycle state is malformed: {exc}") from None

    def write(self, state: CycleState) -> None:
        if self._root.is_symlink():
            raise CLIError(EXIT_REFUSAL, "artifact root must not be a symlink")
        path = self.path_for(state.baseline_cycle_id)
        if path.parent.is_symlink() or path.is_symlink():
            raise CLIError(EXIT_REFUSAL, "cycle state path must not be a symlink")
        payload = _state_to_dict(state)
        _check_secret_markers(payload, "cycle state")
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        _atomic_write_bytes(path, (text + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CLIDependencies:
    load_config: Optional[Callable] = None
    capture_manifest: Optional[Callable] = None
    capture_runtime_manifest: Optional[Callable] = None
    verify_manifest: Optional[Callable] = None
    load_slo_thresholds: Optional[Callable] = None
    build_dataset_plan: Optional[Callable] = None
    dataset_counts: Optional[Callable] = None
    emit_dataset_sql: Optional[Callable] = None
    create_run_controller: Optional[Callable] = None
    create_collector_controller: Optional[Callable] = None
    run_harness: Optional[Callable] = None
    run_control: Optional[Callable] = None
    evaluate_validity: Optional[Callable] = None
    validate_metric_integrity: Optional[Callable] = None
    combine_validity_results: Optional[Callable] = None
    evaluate_slos: Optional[Callable] = None
    aggregate_level: Optional[Callable] = None
    evaluate_scaling: Optional[Callable] = None
    evaluate_regression: Optional[Callable] = None
    derive_capacity: Optional[Callable] = None
    load_previous_baseline: Optional[Callable] = None
    load_manifest: Optional[Callable] = None
    write_run_artifacts: Optional[Callable] = None
    write_cycle_reports: Optional[Callable] = None
    approve_baseline: Optional[Callable] = None
    reject_baseline: Optional[Callable] = None
    render_prometheus_config: Optional[Callable] = None
    render_dashboard: Optional[Callable] = None
    state_store: Optional[CycleStateStore] = None
    stdout: Callable = print
    stderr: Callable = lambda text: print(text, file=sys.stderr)


def _require(value: Any, name: str) -> Any:
    if value is None:
        raise CLIError(EXIT_OPERATIONAL, f"dependency not configured: {name}")
    return value


def default_dependencies(artifact_root: Path) -> CLIDependencies:
    """Wire the real Task 1-10 implementations (lazy imports)."""
    from tools.performance.a3 import approval as approval_mod
    from tools.performance.a3 import collectors as collectors_mod
    from tools.performance.a3 import config as config_mod
    from tools.performance.a3 import dataset as dataset_mod
    from tools.performance.a3 import lifecycle as lifecycle_mod
    from tools.performance.a3 import manifest as manifest_mod
    from tools.performance.a3 import reporting as reporting_mod
    from tools.performance.a3 import scaling as scaling_mod
    from tools.performance.a3 import slo as slo_mod
    from tools.performance.a3 import validity as validity_mod
    from tools.performance.a3.io import read_json, write_json_atomic

    artifact_root = Path(artifact_root)

    def _counts(plan):
        return {
            "accounts": len(plan.accounts),
            "characters": len(plan.characters),
            "guilds": len(plan.guilds),
            "parties": len(plan.parties),
        }

    def _controller(request):
        return lifecycle_mod.RunController(
            config=request["config"],
            manifest=request["manifest"],
            artifact_root=Path(request["artifact_root"]),
        )

    def _not_configured(name):
        def _missing(*args, **kwargs):
            raise CLIError(
                EXIT_OPERATIONAL,
                f"operational adapter not configured in this environment: {name}",
            )

        return _missing

    def _render_prometheus(cycle, manifest_id):
        template_path = (
            Path(__file__).resolve().parent / "config" / "prometheus.yml"
        )
        text = template_path.read_text(encoding="utf-8")
        return (
            text.replace("${A3_BASELINE_CYCLE_ID}", cycle).replace(
                "${A3_MANIFEST_ID}", manifest_id
            )
        )

    def _render_dashboard(template, cycle, manifest_id, run_id):
        return reporting_mod.render_dashboard_runtime(template, cycle, manifest_id, run_id)

    def _load_previous_baseline():
        base = artifact_root / "artifacts" / "performance" / "a3"
        candidates = []
        if base.is_dir():
            for approved_path in base.glob("*/approval/approved-baseline.json"):
                try:
                    approved = read_json(approved_path)
                    state_payload = read_json(approved_path.parents[1] / "cycle-state.json")
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                evaluation = state_payload.get("controls", {}).get("evaluation", {})
                levels = evaluation.get("levels")
                if approved.get("state") != "APPROVED" or not isinstance(levels, list):
                    continue
                candidates.append((approved.get("approved_utc", ""), approved, levels))
        if not candidates:
            return {"status": "NO_PREVIOUS_BASELINE"}
        _, approved, levels = max(candidates, key=lambda item: (item[0], item[1]["baseline_cycle_id"]))
        return {
            "version": 1,
            "approval_state": "APPROVED",
            "manifest_id": approved["manifest_id"],
            "levels": {
                str(item["load_level"]): {"median_metrics": item["median_metrics"]}
                for item in levels
            },
        }

    return CLIDependencies(
        load_config=config_mod.load_config,
        capture_manifest=manifest_mod.capture_manifest,
        capture_runtime_manifest=manifest_mod.capture_manifest,
        verify_manifest=manifest_mod.verify_manifest,
        load_slo_thresholds=lambda: read_json(
            Path(__file__).resolve().parent / "config" / "slo-thresholds.json"
        ),
        build_dataset_plan=dataset_mod.build_dataset_plan,
        dataset_counts=_counts,
        emit_dataset_sql=dataset_mod.emit_dataset_sql,
        create_run_controller=_controller,
        create_collector_controller=lambda request: collectors_mod.CollectorController(),
        run_harness=_not_configured("run_harness"),
        run_control=_not_configured("run_control"),
        evaluate_validity=validity_mod.validate_run,
        validate_metric_integrity=validity_mod.validate_metric_integrity,
        combine_validity_results=validity_mod.combine_validity_results,
        evaluate_slos=slo_mod.evaluate_valid_run_slos,
        aggregate_level=scaling_mod.aggregate_level,
        evaluate_scaling=scaling_mod.evaluate_scaling,
        evaluate_regression=scaling_mod.evaluate_regression,
        derive_capacity=scaling_mod.derive_capacity,
        load_previous_baseline=_load_previous_baseline,
        load_manifest=lambda manifest_id: _missing_manifest(artifact_root, manifest_id),
        write_run_artifacts=reporting_mod.write_run_artifacts,
        write_cycle_reports=reporting_mod.write_cycle_reports,
        approve_baseline=approval_mod.approve_baseline,
        reject_baseline=approval_mod.reject_baseline,
        render_prometheus_config=_render_prometheus,
        render_dashboard=_render_dashboard,
        state_store=CycleStateStore(artifact_root),
    )


def _missing_manifest(artifact_root: Path, manifest_id: str) -> dict:
    from tools.performance.a3.io import read_json

    path = (
        Path(artifact_root)
        / "artifacts"
        / "performance"
        / "a3"
        / manifest_id
        / "manifest.json"
    )
    if not path.is_file():
        raise CLIError(EXIT_OPERATIONAL, f"manifest not found: {manifest_id}")
    return read_json(path)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-root", default=".")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a3", description="A3 baseline orchestration CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--config", required=True)
    _add_common(prepare)

    control = sub.add_parser("control")
    control.add_argument("control_command", choices=["idle", "webgl"])
    control.add_argument("--cycle", required=True)
    _add_common(control)

    run = sub.add_parser("run")
    run.add_argument("--cycle", required=True)
    run.add_argument("--users", type=int, choices=LOAD_LEVELS, required=True)
    run.add_argument("--run", type=int, choices=(1, 2, 3), required=True)
    _add_common(run)

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--cycle", required=True)
    _add_common(evaluate)

    report = sub.add_parser("report")
    report.add_argument("--cycle", required=True)
    _add_common(report)

    approve = sub.add_parser("approve")
    approve.add_argument("--cycle", required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--rationale", required=True)
    approve.add_argument("--approved-utc", required=True)
    _add_common(approve)

    reject = sub.add_parser("reject")
    reject.add_argument("--cycle", required=True)
    reject.add_argument("--approver", required=True)
    reject.add_argument("--rationale", required=True)
    reject.add_argument("--rejected-utc", required=True)
    _add_common(reject)

    return parser


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------


def _validate_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CLIError(EXIT_USAGE, f"{field} must be a non-empty string")
    if len(value) > 128 or "\x00" in value or ".." in value:
        raise CLIError(EXIT_USAGE, f"{field} is not a safe identifier")
    if not all(c.isascii() and (c.isalnum() or c in ".-_") for c in value):
        raise CLIError(EXIT_USAGE, f"{field} is not a safe identifier")
    return value


def _print(deps: CLIDependencies, payload: Mapping) -> None:
    deps.stdout(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _read_state(deps: CLIDependencies, cycle: str) -> CycleState:
    return _require(deps.state_store, "state_store").read(cycle)


def _write_state(deps: CLIDependencies, state: CycleState) -> None:
    _require(deps.state_store, "state_store").write(state)


def _controls_completed(state: CycleState) -> bool:
    return bool(
        state.controls.get("idle", {}).get("completed")
        and state.controls.get("webgl", {}).get("completed")
    )


def _level_to_dict(level: LevelAggregation) -> Dict[str, Any]:
    return {
        "load_level": level.load_level,
        "manifest_id": level.manifest_id,
        "valid_run_count": level.valid_run_count,
        "required_valid_run_count": level.required_valid_run_count,
        "run_ids": list(level.run_ids),
        "run_verdicts": [v.value for v in level.run_verdicts],
        "verdict": level.verdict.value,
        "median_metrics": dict(level.median_metrics),
        "worst_metrics": dict(level.worst_metrics),
        "stability_metrics": dict(level.stability_metrics),
        "warnings": list(level.warnings),
        "failures": list(level.failures),
    }


def _level_from_dict(data: Mapping) -> LevelAggregation:
    return LevelAggregation(
        load_level=data["load_level"],
        manifest_id=data["manifest_id"],
        valid_run_count=data["valid_run_count"],
        required_valid_run_count=data["required_valid_run_count"],
        run_ids=tuple(data["run_ids"]),
        run_verdicts=tuple(MetricVerdict(v) for v in data["run_verdicts"]),
        verdict=MetricVerdict(data["verdict"]),
        median_metrics=data["median_metrics"],
        worst_metrics=data["worst_metrics"],
        stability_metrics=data["stability_metrics"],
        warnings=tuple(data["warnings"]),
        failures=tuple(data["failures"]),
    )


def _scaling_to_dict(result: ScalingResult) -> Dict[str, Any]:
    return {
        "passed": result.passed,
        "first_degradation_level": result.first_degradation_level,
        "checks": [dataclasses.asdict(c) for c in result.checks],
    }


def _scaling_from_dict(data: Mapping) -> ScalingResult:
    from tools.performance.a3.scaling import ScalingCheck

    return ScalingResult(
        passed=data["passed"],
        checks=tuple(ScalingCheck(**c) for c in data["checks"]),
        first_degradation_level=data["first_degradation_level"],
    )


def _regression_to_dict(result: RegressionResult) -> Dict[str, Any]:
    return {
        "passed": result.passed,
        "compared_levels": list(result.compared_levels),
        "checks": [dataclasses.asdict(c) for c in result.checks],
    }


def _regression_from_dict(data: Mapping) -> RegressionResult:
    from tools.performance.a3.scaling import RegressionCheck

    return RegressionResult(
        passed=data["passed"],
        checks=tuple(RegressionCheck(**c) for c in data["checks"]),
        compared_levels=tuple(data["compared_levels"]),
    )


def _capacity_to_dict(result: CapacityResult) -> Dict[str, Any]:
    return {
        "safe_capacity": result.safe_capacity,
        "conditional_capacity": result.conditional_capacity,
        "tested_ceiling": result.tested_ceiling,
        "verdict": result.verdict.value,
        "first_degradation_level": result.first_degradation_level,
        "notes": list(result.notes),
    }


def _capacity_from_dict(data: Mapping) -> CapacityResult:
    return CapacityResult(
        safe_capacity=data["safe_capacity"],
        conditional_capacity=data["conditional_capacity"],
        tested_ceiling=data["tested_ceiling"],
        verdict=CapacityVerdict(data["verdict"]),
        first_degradation_level=data["first_degradation_level"],
        notes=tuple(data["notes"]),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_prepare(args, deps: CLIDependencies) -> int:
    config = _require(deps.load_config, "load_config")(Path(args.config))
    manifest = _require(deps.capture_manifest, "capture_manifest")(Path("."), config)
    if manifest.get("capture_errors") or manifest.get("eligible_for_execution") is not True:
        raise CLIError(EXIT_REFUSAL, "manifest is not eligible for execution")
    cycle = _validate_identifier(manifest.get("manifest_id"), "baseline_cycle_id")
    plan = _require(deps.build_dataset_plan, "build_dataset_plan")(DATASET_SEED)
    counts = _require(deps.dataset_counts, "dataset_counts")(plan)

    planned = {
        "dataset_sql": f"artifacts/performance/a3/{cycle}/dataset/a3-dataset.sql",
        "dataset_metadata": f"artifacts/performance/a3/{cycle}/dataset/a3-dataset.sql.metadata.json",
        "prometheus_config": f"artifacts/performance/a3/{cycle}/prometheus.yml",
        "grafana_dashboard": f"artifacts/performance/a3/{cycle}/grafana-dashboard.json",
        "cycle_state": f"artifacts/performance/a3/{cycle}/cycle-state.json",
    }
    payload = {
        "command": "prepare",
        "baseline_cycle_id": cycle,
        "config": str(args.config),
        "manifest_id": cycle,
        "dataset": {"seed": DATASET_SEED, "row_counts": counts},
        "planned_paths": planned,
        "dry_run": bool(args.dry_run),
        "execution": "not executed (planning only)",
    }
    if args.dry_run:
        _print(deps, payload)
        return EXIT_OK

    store = _require(deps.state_store, "state_store")
    if store.exists(cycle):
        raise CLIError(EXIT_REFUSAL, f"cycle already prepared: {cycle}")

    # Persist every required cycle input; cycle-state.json is written last.
    # Any earlier failure must leave no finalized cycle state.
    from tools.performance.a3.io import write_json_atomic

    cycle_dir = Path(args.artifact_root) / "artifacts" / "performance" / "a3" / cycle
    write_json_atomic(cycle_dir / "manifest.json", manifest)

    dataset_path = cycle_dir / "dataset" / "a3-dataset.sql"
    _require(deps.emit_dataset_sql, "emit_dataset_sql")(plan, dataset_path)

    rendered = _require(deps.render_prometheus_config, "render_prometheus_config")(cycle, cycle)
    _atomic_write_bytes(cycle_dir / "prometheus.yml", rendered.encode("utf-8"))

    template_path = Path(__file__).resolve().parent / "config" / "grafana-dashboard.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    dashboard = _require(deps.render_dashboard, "render_dashboard")(
        template, cycle, cycle, "cycle"
    )
    write_json_atomic(cycle_dir / "grafana-dashboard.json", dashboard)

    state = CycleState(
        version=STATE_VERSION,
        state=ApprovalState.DRAFT,
        baseline_cycle_id=cycle,
        manifest_id=cycle,
        config_path=str(args.config),
        artifact_root=str(args.artifact_root),
        controls={
            "dataset": {"seed": DATASET_SEED, "row_counts": counts},
            "git_sha": manifest.get("source", {}).get("git_commit_sha"),
            "planned_paths": planned,
        },
        runs=(),
        evaluated=False,
        reported=False,
        approved=False,
        catastrophic=False,
        last_error=None,
    )
    store.write(state)
    _print(deps, payload)
    return EXIT_OK


def _cmd_control(args, deps: CLIDependencies) -> int:
    state = _read_state(deps, args.cycle)
    if state.state is not ApprovalState.DRAFT:
        raise CLIError(EXIT_REFUSAL, "controls are only allowed while the cycle is DRAFT")
    name = args.control_command
    if name == "webgl" and not state.controls.get("idle", {}).get("completed"):
        raise CLIError(EXIT_REFUSAL, "webgl control requires a completed idle control")
    if state.controls.get(name, {}).get("completed"):
        raise CLIError(EXIT_REFUSAL, f"control already completed: {name}")
    if args.dry_run:
        _print(
            deps,
            {
                "command": f"control {name}",
                "baseline_cycle_id": state.baseline_cycle_id,
                "duration_seconds": 600,
                "dry_run": True,
            },
        )
        return EXIT_OK
    result = _require(deps.run_control, "run_control")(
        name=name,
        duration_seconds=600,
        artifact_root=str(args.artifact_root),
        cycle=state.baseline_cycle_id,
    )
    if not isinstance(result, Mapping):
        raise CLIError(EXIT_OPERATIONAL, "control adapter returned malformed result")
    if name == "webgl" and result.get("clients") != 20:
        raise CLIError(EXIT_OPERATIONAL, "webgl control must observe exactly 20 clients")
    controls = dict(state.controls)
    controls[name] = {**result, "completed": True}
    _write_state(deps, dataclasses.replace(state, controls=controls))
    _print(
        deps,
        {
            "command": f"control {name}",
            "baseline_cycle_id": state.baseline_cycle_id,
            "completed": True,
            "dry_run": False,
        },
    )
    return EXIT_OK


def _valid_runs_at(state: CycleState, level: int) -> int:
    return sum(
        1
        for entry in state.runs
        if entry["load_level"] == level and entry["valid"]
    )


def _cmd_run(args, deps: CLIDependencies) -> int:
    state = _read_state(deps, args.cycle)
    if state.catastrophic:
        raise CLIError(EXIT_REFUSAL, "cycle is catastrophic; no further runs allowed")
    if state.state is not ApprovalState.DRAFT:
        raise CLIError(EXIT_REFUSAL, "runs are only allowed while the cycle is DRAFT")
    if not _controls_completed(state):
        raise CLIError(EXIT_REFUSAL, "both controls must complete before benchmark runs")
    for level in LOAD_LEVELS:
        if level >= args.users:
            break
        if _valid_runs_at(state, level) != 3:
            raise CLIError(
                EXIT_REFUSAL,
                f"level {level} requires exactly three valid runs before level {args.users}",
            )

    base_id = f"run-l{args.users}-n{args.run}"
    same_base = [e for e in state.runs if e["run_id"] == base_id or e["run_id"].startswith(base_id + "-r")]
    if any(e["run_id"] == base_id and e["valid"] for e in same_base):
        raise CLIError(EXIT_REFUSAL, f"run identity already finalized: {base_id}")
    run_id = base_id
    if same_base:
        run_id = f"{base_id}-r{len(same_base) + 1}"

    if args.dry_run:
        _print(
            deps,
            {
                "command": "run",
                "baseline_cycle_id": state.baseline_cycle_id,
                "users": args.users,
                "run_number": args.run,
                "run_id": run_id,
                "phases": [
                    "ENVIRONMENT_CHECK",
                    "SERVICE_START",
                    "PRECONDITIONING",
                    "RAMP_UP",
                    "STEADY_STATE",
                    "COOL_DOWN",
                    "VALIDATION",
                    "REPORTING",
                ],
                "planned_artifacts": f"artifacts/performance/a3/{state.baseline_cycle_id}/runs/{run_id}/",
                "dry_run": True,
            },
        )
        return EXIT_OK

    config = _require(deps.load_config, "load_config")(Path(state.config_path))
    frozen_manifest = _require(deps.load_manifest, "load_manifest")(
        state.baseline_cycle_id
    )
    if (
        isinstance(frozen_manifest, Mapping)
        and frozen_manifest.get("manifest_id") != state.manifest_id
    ):
        raise CLIError(EXIT_REFUSAL, "frozen manifest identity mismatch")
    runtime_manifest = _require(deps.capture_runtime_manifest, "capture_runtime_manifest")(
        Path("."), config
    )
    drift = _require(deps.verify_manifest, "verify_manifest")(
        frozen_manifest, runtime_manifest
    )
    if drift:
        raise CLIError(EXIT_REFUSAL, "runtime manifest drift detected")

    request = {
        "config": config,
        "manifest": frozen_manifest,
        "artifact_root": str(args.artifact_root),
        "baseline_cycle_id": state.baseline_cycle_id,
        "run_id": run_id,
        "load_level": args.users,
        "run_number": args.run,
    }
    controller = _require(deps.create_run_controller, "create_run_controller")(request)

    from tools.performance.a3.collectors import RunContext
    from tools.performance.a3.models import RunPhase

    controller.run_preflight()
    controller.run_service_start(())
    collectors = _require(deps.create_collector_controller, "create_collector_controller")(request)
    context = RunContext(
        baseline_cycle_id=state.baseline_cycle_id,
        manifest_id=state.manifest_id,
        run_id=run_id,
        artifact_root=Path(args.artifact_root),
        phase=RunPhase.STEADY_STATE,
    )
    try:
        collectors.start(context)
    except Exception as exc:
        raise CLIError(EXIT_OPERATIONAL, f"collector start failed: {exc}") from None

    harness = None
    primary_error = None
    stop_error = None
    catastrophic_result = False
    try:
        harness = _require(deps.run_harness, "run_harness")(request)
        if harness.get("catastrophic"):
            # Confirmed catastrophic result: invoke the abort path before
            # the finally-guaranteed collector stop.
            catastrophic_result = True
            controller.abort("harness reported a catastrophic result", catastrophic=True)
        else:
            controller.run_preconditioning()
            controller.run_ramp_up()
            controller.run_steady_state()
            controller.run_cooldown()
    except Exception as exc:
        primary_error = exc
        if _is_confirmed_catastrophic(exc):
            try:
                controller.abort(str(exc), catastrophic=True)
            except Exception:  # noqa: BLE001 - abort is best-effort here
                pass
    finally:
        try:
            collectors.stop()
        except Exception as exc:  # noqa: BLE001 - recorded deterministically
            stop_error = exc

    if primary_error is not None and not _is_confirmed_catastrophic(primary_error):
        # Operational failure: no automatic catastrophic claim.
        message = f"run execution failed: {primary_error}"
        if stop_error is not None:
            message += f"; collector stop also failed: {stop_error}"
        raise CLIError(EXIT_OPERATIONAL, message) from None

    if primary_error is not None:
        # Confirmed catastrophic exception before offline evaluation; the
        # harness contract inputs may be unavailable.
        return _preserve_catastrophic_run(deps, args, state, run_id, harness, None, None)

    if stop_error is not None:
        raise CLIError(EXIT_OPERATIONAL, f"collector stop failed: {stop_error}") from None

    if catastrophic_result:
        # Harness already reported the catastrophic signal: preserve
        # immediately without offline evaluation.
        return _preserve_catastrophic_run(deps, args, state, run_id, harness, None, None)

    try:
        controller.run_validation()
        controller.run_reporting()
        run_validity = _require(deps.evaluate_validity, "evaluate_validity")(harness["run_data"])
        query_window = harness["prometheus_queries"]
        metric_validity = _require(
            deps.validate_metric_integrity, "validate_metric_integrity"
        )(
            tuple(harness.get("prometheus_series", ())),
            query_window["start"],
            query_window["end"],
            step=query_window["step"],
        )
        validity = _require(
            deps.combine_validity_results, "combine_validity_results"
        )(run_validity, metric_validity)
        thresholds = _require(deps.load_slo_thresholds, "load_slo_thresholds")()
        slo_result = _require(deps.evaluate_slos, "evaluate_slos")(
            validity, harness["metric_bundle"], thresholds
        )
    except CLIError:
        raise
    except Exception as exc:
        raise CLIError(EXIT_OPERATIONAL, f"offline evaluation failed: {exc}") from None

    if slo_result.catastrophic_signals:
        controller.abort("catastrophic SLO signal", catastrophic=True)
        return _preserve_catastrophic_run(
            deps, args, state, run_id, harness, validity, slo_result
        )

    artifacts = _write_checked_artifacts(
        deps,
        args,
        state,
        run_id,
        harness,
        validity,
        slo_result,
        final_phase="REPORTING",
        verdict=slo_result.status.value,
        valid_flag=bool(validity.valid),
    )
    entry = {
        "run_id": run_id,
        "load_level": args.users,
        "run_number": args.run,
        "valid": bool(validity.valid),
        "verdict": slo_result.status.value,
        "manifest_id": state.manifest_id,
        "catastrophic": False,
        "metrics": harness.get("metrics", {}),
    }
    runs = tuple(state.runs) + (entry,)
    _write_state(deps, dataclasses.replace(state, runs=runs, last_error=None))
    _print(
        deps,
        {
            "command": "run",
            "baseline_cycle_id": state.baseline_cycle_id,
            "run_id": run_id,
            "valid": bool(validity.valid),
            "verdict": slo_result.status.value,
            "catastrophic": False,
            "artifacts": artifacts if isinstance(artifacts, Mapping) else {"complete": True},
            "dry_run": False,
        },
    )
    return EXIT_OK


def _is_confirmed_catastrophic(exc: BaseException) -> bool:
    """Catastrophic only when an adapter explicitly says so."""
    if getattr(exc, "catastrophic", None) is True:
        return True
    from tools.performance.a3.lifecycle import CatastrophicRunError

    return isinstance(exc, CatastrophicRunError)


def _write_checked_artifacts(
    deps,
    args,
    state: CycleState,
    run_id: str,
    harness: Mapping,
    validity,
    slo_result,
    final_phase: str,
    verdict: str,
    valid_flag: bool,
):
    run_payload = {
        "version": 1,
        "baseline_cycle_id": state.baseline_cycle_id,
        "run_id": run_id,
        "manifest_id": state.manifest_id,
        "load_level": args.users,
        "run_number": args.run,
        "validity": {
            "valid": valid_flag,
            "reasons": (
                [getattr(reason, "message", str(reason)) for reason in validity.reasons]
                if validity is not None
                else []
            ),
        },
        "final_phase": final_phase,
        "artifact_status": "complete",
        "created_utc": harness.get("created_utc"),
    }
    summary_payload = {
        "version": 1,
        "run_id": run_id,
        "manifest_id": state.manifest_id,
        "load_level": args.users,
        "verdict": verdict,
        "valid": valid_flag,
        "median_metrics": harness.get("metrics", {}),
        "worst_metrics": harness.get("worst_metrics", {}),
        "warnings": [],
        "failures": [],
        "primary_bottleneck": None,
    }
    if slo_result is None:
        slo_result = {
            "status": "BLOCKED",
            "evaluations": [],
            "catastrophic_signals": [],
            "evaluated_metrics": [],
            "blocked_metrics": [],
        }
    try:
        return _require(deps.write_run_artifacts, "write_run_artifacts")(
            artifact_root=Path(args.artifact_root),
            baseline_cycle_id=state.baseline_cycle_id,
            run_payload=run_payload,
            summary_payload=summary_payload,
            timeseries_rows=harness["timeseries_rows"],
            workload_rows=harness["workload_rows"],
            slo_result=slo_result,
            anomalies=harness.get("anomalies", []),
            prometheus_queries=harness["prometheus_queries"],
            source_files=harness["source_files"],
        )
    except CLIError:
        raise
    except Exception as exc:
        raise CLIError(EXIT_OPERATIONAL, f"run artifact write failed: {exc}") from None


def _preserve_catastrophic_run(
    deps,
    args,
    state: CycleState,
    run_id: str,
    harness,
    validity,
    slo_result,
) -> int:
    """Preserve evidence for a confirmed catastrophic run, then mark state.

    The cycle becomes catastrophic only after artifact checksums complete;
    a preservation failure stays operational and changes nothing.
    """
    from tools.performance.a3.reporting import SOURCE_FILE_KEYS

    if harness is None or set(harness.get("source_files", {})) != set(SOURCE_FILE_KEYS):
        raise CLIError(
            EXIT_OPERATIONAL,
            "catastrophic artifact preservation failed: required source set unavailable",
        )
    artifacts = _write_checked_artifacts(
        deps,
        args,
        state,
        run_id,
        harness,
        validity,
        slo_result,
        final_phase="ARTIFACT_CAPTURE",
        verdict="BLOCKED",
        valid_flag=bool(validity.valid) if validity is not None else False,
    )
    entry = {
        "run_id": run_id,
        "load_level": args.users,
        "run_number": args.run,
        "valid": bool(validity.valid) if validity is not None else False,
        "verdict": "BLOCKED",
        "manifest_id": state.manifest_id,
        "catastrophic": True,
        "metrics": harness.get("metrics", {}),
    }
    runs = tuple(state.runs) + (entry,)
    _write_state(deps, dataclasses.replace(state, runs=runs, catastrophic=True))
    _print(
        deps,
        {
            "command": "run",
            "baseline_cycle_id": state.baseline_cycle_id,
            "run_id": run_id,
            "valid": entry["valid"],
            "verdict": "BLOCKED",
            "catastrophic": True,
            "artifacts": artifacts if isinstance(artifacts, Mapping) else {"complete": True},
            "dry_run": False,
        },
    )
    return EXIT_CATASTROPHIC


def _cmd_evaluate(args, deps: CLIDependencies) -> int:
    state = _read_state(deps, args.cycle)
    if state.catastrophic:
        raise CLIError(EXIT_REFUSAL, "catastrophic cycle cannot be evaluated as normal")
    if state.state is not ApprovalState.DRAFT:
        raise CLIError(EXIT_REFUSAL, "evaluation requires DRAFT state")
    if not _controls_completed(state):
        raise CLIError(EXIT_REFUSAL, "controls must complete before evaluation")

    manifests = {entry["manifest_id"] for entry in state.runs}
    if len(manifests) > 1:
        raise CLIError(EXIT_REFUSAL, "mixed manifest IDs across runs")
    levels_present = sorted({entry["load_level"] for entry in state.runs})
    if not levels_present:
        raise CLIError(EXIT_REFUSAL, "no runs recorded for evaluation")
    for level in levels_present:
        if _valid_runs_at(state, level) != 3:
            raise CLIError(
                EXIT_REFUSAL,
                f"level {level} has {_valid_runs_at(state, level)} valid runs; exactly three required",
            )

    if args.dry_run:
        _print(
            deps,
            {
                "command": "evaluate",
                "baseline_cycle_id": state.baseline_cycle_id,
                "planned_levels": levels_present,
                "operations": [
                    "aggregate_level",
                    "evaluate_scaling",
                    "evaluate_regression",
                    "derive_capacity",
                    "transition CI_EVALUATED",
                ],
                "dry_run": True,
            },
        )
        return EXIT_OK

    from tools.performance.a3.scaling import RunSummary

    aggregations = []
    for level in levels_present:
        summaries = [
            RunSummary(
                run_id=entry["run_id"],
                manifest_id=entry["manifest_id"],
                load_level=entry["load_level"],
                run_number=entry["run_number"],
                valid=entry["valid"],
                verdict=MetricVerdict(entry["verdict"]),
                metrics=entry["metrics"],
                catastrophic=entry.get("catastrophic", False),
            )
            for entry in state.runs
            if entry["load_level"] == level and entry["valid"]
        ]
        aggregations.append(_require(deps.aggregate_level, "aggregate_level")(summaries))

    scaling = _require(deps.evaluate_scaling, "evaluate_scaling")(aggregations)
    previous = _require(deps.load_previous_baseline, "load_previous_baseline")()
    regression = _require(deps.evaluate_regression, "evaluate_regression")(aggregations, previous)
    capacity = _require(deps.derive_capacity, "derive_capacity")(aggregations)

    regression_payload = _regression_to_dict(regression)
    regression_payload["status"] = (
        "NOT_APPLICABLE"
        if previous == {"status": "NO_PREVIOUS_BASELINE"}
        else ("PASS" if regression.passed else "FAIL")
    )
    regression_payload["reason"] = (
        "no previous approved baseline"
        if previous == {"status": "NO_PREVIOUS_BASELINE"}
        else None
    )
    evaluation = {
        "levels": [_level_to_dict(level) for level in aggregations],
        "scaling": _scaling_to_dict(scaling),
        "regression": regression_payload,
        "capacity": _capacity_to_dict(capacity),
    }
    controls = dict(state.controls)
    controls["evaluation"] = evaluation
    controls["ci"] = {"evaluated": True, "status": "success"}
    if not is_transition_allowed(state.state, ApprovalState.CI_EVALUATED):
        raise CLIError(EXIT_REFUSAL, "illegal state transition to CI_EVALUATED")
    new_state = dataclasses.replace(
        state,
        state=ApprovalState.CI_EVALUATED,
        controls=controls,
        evaluated=True,
    )
    _write_state(deps, new_state)
    _print(
        deps,
        {
            "command": "evaluate",
            "baseline_cycle_id": state.baseline_cycle_id,
            "state": ApprovalState.CI_EVALUATED.value,
            "levels": [level["load_level"] for level in evaluation["levels"]],
            "capacity": evaluation["capacity"],
            "dry_run": bool(args.dry_run),
        },
    )
    return EXIT_OK


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_run_checksums(
    artifact_root: Path, state: CycleState
) -> Tuple[Dict[str, str], Dict[str, int]]:
    cycle_dir = artifact_root / "artifacts" / "performance" / "a3" / state.baseline_cycle_id
    hashes: Dict[str, str] = {}
    numbers: Dict[str, int] = {}
    for entry in sorted(state.runs, key=lambda item: item["run_id"]):
        run_id = entry["run_id"]
        checksum_path = cycle_dir / "runs" / run_id / "checksums.json"
        if not checksum_path.is_file():
            raise CLIError(EXIT_OPERATIONAL, f"run checksums missing: {run_id}")
        try:
            payload = json.loads(checksum_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIError(EXIT_OPERATIONAL, f"run checksums invalid for {run_id}: {exc}") from None
        if payload.get("run_id") != run_id or not isinstance(payload.get("files"), list):
            raise CLIError(EXIT_OPERATIONAL, f"run checksums identity mismatch: {run_id}")
        run_dir = checksum_path.parent
        for file_entry in payload["files"]:
            if not isinstance(file_entry, Mapping):
                raise CLIError(EXIT_OPERATIONAL, f"run checksums malformed: {run_id}")
            relative = file_entry.get("path")
            expected = file_entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise CLIError(EXIT_OPERATIONAL, f"run checksums malformed: {run_id}")
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise CLIError(EXIT_OPERATIONAL, f"run checksums unsafe path: {run_id}")
            target = run_dir / relative_path
            if not target.is_file() or _sha256_path(target) != expected:
                raise CLIError(EXIT_OPERATIONAL, f"run checksum mismatch: {run_id}/{relative}")
        hashes[run_id] = _sha256_path(checksum_path)
        numbers[run_id] = int(entry["run_number"])
    return hashes, numbers


def _cmd_report(args, deps: CLIDependencies) -> int:
    state = _read_state(deps, args.cycle)
    if not state.evaluated or state.state is not ApprovalState.CI_EVALUATED:
        raise CLIError(EXIT_REFUSAL, "report requires a completed evaluation")
    if state.reported:
        raise CLIError(EXIT_REFUSAL, "report already finalized for this cycle")
    evaluation = state.controls.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise CLIError(EXIT_REFUSAL, "evaluation results missing from cycle state")

    if args.dry_run:
        _print(
            deps,
            {
                "command": "report",
                "baseline_cycle_id": state.baseline_cycle_id,
                "planned_paths": {
                    "technical_report": f"artifacts/performance/a3/{state.baseline_cycle_id}/technical-report.md",
                    "executive_summary": f"artifacts/performance/a3/{state.baseline_cycle_id}/executive-summary.md",
                    "comparison_csv": f"artifacts/performance/a3/{state.baseline_cycle_id}/comparison.csv",
                    "artifact_index": f"artifacts/performance/a3/{state.baseline_cycle_id}/artifact-index.json",
                },
                "dry_run": True,
            },
        )
        return EXIT_OK

    level_results = [_level_from_dict(item) for item in evaluation["levels"]]
    scaling = _scaling_from_dict(evaluation["scaling"])
    regression = _regression_from_dict(evaluation["regression"])
    capacity = _capacity_from_dict(evaluation["capacity"])
    manifest = _require(deps.load_manifest, "load_manifest")(state.manifest_id)

    bottleneck = None
    for check in scaling.checks:
        if not check.passed and check.metric:
            bottleneck = check.metric
            break
    run_checksums, run_numbers = _validated_run_checksums(Path(args.artifact_root), state)
    report_controls = {
        "idle": dict(state.controls.get("idle", {})),
        "webgl": dict(state.controls.get("webgl", {})),
        "primary_bottleneck": bottleneck,
        "run_checksums": run_checksums,
        "run_numbers": run_numbers,
        "dashboard_run_id": state.runs[0]["run_id"] if state.runs else "cycle",
    }
    result = _require(deps.write_cycle_reports, "write_cycle_reports")(
        artifact_root=Path(args.artifact_root),
        baseline_cycle_id=state.baseline_cycle_id,
        manifest=manifest,
        level_results=level_results,
        scaling_result=scaling,
        regression_result=regression,
        capacity_result=capacity,
        controls=report_controls,
        dataset_summary=state.controls.get("dataset", {}),
        anomalies=[],
        recommendations={"remediation": [], "a5": []},
    )
    cycle_dir = (
        Path(args.artifact_root) / "artifacts" / "performance" / "a3" / state.baseline_cycle_id
    )
    checksums = cycle_dir / "checksums.json"
    if not checksums.is_file() or not getattr(result, "files", None):
        raise CLIError(EXIT_OPERATIONAL, "cycle report checksum verification failed")

    if not is_transition_allowed(state.state, ApprovalState.AWAITING_APPROVAL):
        raise CLIError(EXIT_REFUSAL, "illegal state transition to AWAITING_APPROVAL")
    controls = dict(state.controls)
    controls["report"] = {
        "technical_report": str(result.technical_report_path),
        "executive_summary": str(result.executive_summary_path),
        "comparison_csv": str(result.comparison_csv_path),
        "artifact_index": str(result.artifact_index_path),
    }
    new_state = dataclasses.replace(
        state,
        state=ApprovalState.AWAITING_APPROVAL,
        controls=controls,
        reported=True,
    )
    _write_state(deps, new_state)
    _print(
        deps,
        {
            "command": "report",
            "baseline_cycle_id": state.baseline_cycle_id,
            "state": ApprovalState.AWAITING_APPROVAL.value,
            "technical_report": str(result.technical_report_path),
            "executive_summary": str(result.executive_summary_path),
            "comparison_csv": str(result.comparison_csv_path),
            "artifact_index": str(result.artifact_index_path),
            "approval": "not approved (report only)",
            "dry_run": bool(args.dry_run),
        },
    )
    return EXIT_OK


def _approval_summary(args, deps: CLIDependencies, state: CycleState) -> Dict[str, Any]:
    controls = state.controls
    if "cycle_summary" in controls:
        summary = dict(controls["cycle_summary"])
    else:
        evaluation = controls.get("evaluation", {})
        capacity = evaluation.get("capacity", {})
        warning_candidates: List[str] = []
        for item in evaluation.get("levels", []):
            warning_candidates.extend(str(value) for value in item.get("warnings", []))
        warning_candidates.extend(
            str(check.get("message"))
            for check in evaluation.get("scaling", {}).get("checks", [])
            if check.get("message") and not check.get("passed", True)
        )
        warning_candidates.extend(
            str(check.get("message"))
            for check in evaluation.get("regression", {}).get("checks", [])
            if check.get("message") and not check.get("passed", True)
        )
        warning_candidates.extend(str(value) for value in capacity.get("notes", []))
        warnings = sorted({value for value in warning_candidates if value})
        summary = {
            "capacity": {
                "verdict": capacity.get("verdict"),
                "safe_capacity": capacity.get("safe_capacity"),
                "conditional_capacity": capacity.get("conditional_capacity"),
                "tested_ceiling": capacity.get("tested_ceiling"),
                "first_degradation_level": capacity.get("first_degradation_level"),
                "notes": capacity.get("notes", []),
            },
            "levels": {
                str(item["load_level"]): {"verdict": item["verdict"]}
                for item in evaluation.get("levels", [])
            },
            "warnings": warnings,
            "git_sha": controls.get("git_sha"),
            "report_checksums_sha256": None,
        }
    checksums = (
        Path(args.artifact_root)
        / "artifacts"
        / "performance"
        / "a3"
        / state.baseline_cycle_id
        / "checksums.json"
    )
    if summary.get("report_checksums_sha256") is None and checksums.is_file():
        summary["report_checksums_sha256"] = hashlib.sha256(
            checksums.read_bytes()
        ).hexdigest()
    summary["state"] = ApprovalState.AWAITING_APPROVAL.value
    summary["baseline_cycle_id"] = state.baseline_cycle_id
    summary["manifest_id"] = state.manifest_id
    summary["ci"] = {"evaluated": True, "status": "success"}
    return summary


def _cmd_approve(args, deps: CLIDependencies) -> int:
    state = _read_state(deps, args.cycle)
    if state.approved or state.state is ApprovalState.APPROVED:
        raise CLIError(EXIT_REFUSAL, "cycle already approved")
    if state.state is not ApprovalState.AWAITING_APPROVAL or not state.reported or not state.evaluated:
        raise CLIError(EXIT_REFUSAL, "approval requires AWAITING_APPROVAL with a completed report")
    if args.dry_run:
        _print(
            deps,
            {
                "command": "approve",
                "baseline_cycle_id": state.baseline_cycle_id,
                "approver_provided": bool(args.approver),
                "rationale_provided": bool(args.rationale),
                "approved_utc": args.approved_utc,
                "planned_approval": f"artifacts/performance/a3/{state.baseline_cycle_id}/approval/approved-baseline.json",
                "dry_run": True,
            },
        )
        return EXIT_OK
    summary = _approval_summary(args, deps, state)
    result = _require(deps.approve_baseline, "approve_baseline")(
        artifact_root=Path(args.artifact_root),
        cycle_summary=summary,
        approver=args.approver,
        rationale=args.rationale,
        approved_utc=args.approved_utc,
    )
    if not is_transition_allowed(state.state, ApprovalState.APPROVED):
        raise CLIError(EXIT_REFUSAL, "illegal state transition to APPROVED")
    new_state = dataclasses.replace(
        state, state=ApprovalState.APPROVED, approved=True
    )
    _write_state(deps, new_state)
    _print(
        deps,
        {
            "command": "approve",
            "baseline_cycle_id": state.baseline_cycle_id,
            "state": ApprovalState.APPROVED.value,
            "approval_path": str(result.approval_path),
            "approval_record_sha256": result.record.approval_record_sha256,
            "dry_run": bool(args.dry_run),
        },
    )
    return EXIT_OK


def _cmd_reject(args, deps: CLIDependencies) -> int:
    state = _read_state(deps, args.cycle)
    if state.state is not ApprovalState.AWAITING_APPROVAL or not state.reported:
        raise CLIError(EXIT_REFUSAL, "rejection requires AWAITING_APPROVAL with a completed report")
    if args.dry_run:
        _print(
            deps,
            {
                "command": "reject",
                "baseline_cycle_id": state.baseline_cycle_id,
                "approver_provided": bool(args.approver),
                "rationale_provided": bool(args.rationale),
                "rejected_utc": args.rejected_utc,
                "planned_rejection": f"artifacts/performance/a3/{state.baseline_cycle_id}/approval/rejected-baseline.json",
                "dry_run": True,
            },
        )
        return EXIT_OK
    summary = _approval_summary(args, deps, state)
    result = _require(deps.reject_baseline, "reject_baseline")(
        artifact_root=Path(args.artifact_root),
        cycle_summary=summary,
        approver=args.approver,
        rationale=args.rationale,
        rejected_utc=args.rejected_utc,
    )
    if not is_transition_allowed(state.state, ApprovalState.REJECTED):
        raise CLIError(EXIT_REFUSAL, "illegal state transition to REJECTED")
    new_state = dataclasses.replace(state, state=ApprovalState.REJECTED)
    _write_state(deps, new_state)
    _print(
        deps,
        {
            "command": "reject",
            "baseline_cycle_id": state.baseline_cycle_id,
            "state": ApprovalState.REJECTED.value,
            "approval_path": str(result.approval_path),
            "dry_run": bool(args.dry_run),
        },
    )
    return EXIT_OK


_HANDLERS = {
    "prepare": _cmd_prepare,
    "control": _cmd_control,
    "run": _cmd_run,
    "evaluate": _cmd_evaluate,
    "report": _cmd_report,
    "approve": _cmd_approve,
    "reject": _cmd_reject,
}


def dispatch(args: argparse.Namespace, dependencies: Optional[CLIDependencies] = None) -> int:
    deps = dependencies or default_dependencies(Path(args.artifact_root))
    handler = _HANDLERS.get(args.command)
    if handler is None:
        raise CLIError(EXIT_USAGE, f"unknown command: {args.command}")
    return handler(args, deps)


def main(argv: Optional[Sequence[str]] = None, dependencies: Optional[CLIDependencies] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    stderr = dependencies.stderr if dependencies is not None else (
        lambda text: print(text, file=sys.stderr)
    )
    try:
        return dispatch(args, dependencies)
    except CLIError as exc:
        stderr(exc.message)
        return exc.code
    except ValueError as exc:
        stderr(str(exc))
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - mapped exit code, no stack trace
        stderr(f"internal error: {type(exc).__name__}: {exc}")
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
