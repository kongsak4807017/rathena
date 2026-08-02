"""Tests for A3 artifact generation, Markdown reports, and Grafana validation."""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.performance.a3.io import read_json, sha256_file
from tools.performance.a3.models import CapacityVerdict, MetricVerdict
from tools.performance.a3.reporting import (
    ArtifactError,
    CycleReportResult,
    RunArtifactResult,
    render_dashboard_runtime,
    validate_dashboard_thresholds,
    write_cycle_reports,
    write_run_artifacts,
)
from tools.performance.a3.scaling import (
    CapacityResult,
    LevelAggregation,
    RegressionResult,
    ScalingResult,
)
from tools.performance.a3.slo import _METRIC_SPECS, evaluate_run_slos
from tools.performance.a3.tests.test_slo import thresholds as slo_thresholds
from tools.performance.a3.tests.test_slo import valid_bundle

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
MANIFEST_PATH = FIXTURE_DIR / "valid_manifest.json"
DASHBOARD_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "grafana-dashboard.json"
)
THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "slo-thresholds.json"
)
DOC_PATH = (
    Path(__file__).resolve().parents[4]
    / "docs"
    / "observability"
    / "A3_ARTIFACT_FORMAT.md"
)

CYCLE = "cycle-2026-08"
RUN_ID = "run-l500-n1"
MANIFEST_ID = "a3-20260802-f82d9b0-ubuntu2404-8c16t-32g-001"

REQUIRED_RUN_FILES = (
    "run.json",
    "summary.json",
    "timeseries.csv",
    "workload.csv",
    "slo-verdict.json",
    "anomalies.json",
    "prometheus-queries.json",
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
    "checksums.json",
)

REQUIRED_CYCLE_FILES = (
    "manifest.json",
    "technical-report.md",
    "executive-summary.md",
    "comparison.csv",
    "capacity.json",
    "scaling.json",
    "regression.json",
    "anomalies.json",
    "artifact-index.json",
    "retention.json",
    "grafana-dashboard.json",
    "checksums.json",
)

SOURCE_KEYS = tuple(REQUIRED_RUN_FILES[7:20])


def run_payload() -> dict:
    return {
        "version": 1,
        "baseline_cycle_id": CYCLE,
        "run_id": RUN_ID,
        "manifest_id": MANIFEST_ID,
        "load_level": 500,
        "run_number": 1,
        "validity": {"valid": True, "reasons": []},
        "final_phase": "REPORTING",
        "artifact_status": "complete",
        "created_utc": "2026-08-02T20:00:00Z",
    }


def summary_payload() -> dict:
    return {
        "version": 1,
        "run_id": RUN_ID,
        "manifest_id": MANIFEST_ID,
        "load_level": 500,
        "verdict": "PASS",
        "valid": True,
        "median_metrics": {"latency_p95_ms": 10.0},
        "worst_metrics": {"latency_p95_ms": 11.0},
        "warnings": [],
        "failures": [],
        "primary_bottleneck": None,
    }


def timeseries_rows() -> list:
    rows = []
    for index in range(4):
        rows.append(
            {
                "timestamp": 1000 + index * 5,
                "phase": "STEADY_STATE",
                "active_users": 500,
                "cpu_percent": 50.0 + index,
                "memory_rss_bytes": 20_000_000_000,
                "tick_latency_ms": 5.0,
                "packet_processing_ms": 2.0,
                "sql_latency_ms": 10.0,
                "script_latency_ms": 2.0,
                "storage_utilization_percent": 45.0,
                "storage_await_ms": 2.0,
                "network_utilization_percent": 40.0,
            }
        )
    return rows


def workload_rows() -> list:
    return [
        {
            "timestamp": 1000 + index * 5,
            "phase": "STEADY_STATE",
            "active_users": 500,
            "category": "combat",
            "event_count": 100,
            "error_count": 0,
        }
        for index in range(2)
    ]


def slo_result_dict() -> dict:
    return {
        "status": "PASS",
        "evaluations": [],
        "catastrophic_signals": [],
        "evaluated_metrics": [],
        "blocked_metrics": [],
    }


def prometheus_queries() -> dict:
    return {
        "start": 1000,
        "end": 2000,
        "step": 5,
        "queries": [
            {
                "name": "cpu",
                "expr": "rate(cpu_seconds_total[5m])",
                "url": "http://127.0.0.1:9090/api/v1/query_range?query=up",
            }
        ],
    }


def build_source_files(raw_dir: Path) -> dict:
    mapping = {}
    for key in SOURCE_KEYS:
        path = raw_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic {key}\n", encoding="utf-8", newline="\n")
        mapping[key] = path
    return mapping


def level_agg(level: int, verdict: MetricVerdict = MetricVerdict.PASS) -> LevelAggregation:
    return LevelAggregation(
        load_level=level,
        manifest_id=MANIFEST_ID,
        valid_run_count=3,
        required_valid_run_count=3,
        run_ids=(f"run-l{level}-n1", f"run-l{level}-n2", f"run-l{level}-n3"),
        run_verdicts=(verdict, verdict, verdict),
        verdict=verdict,
        median_metrics={
            "cpu_p95_percent": 60.0,
            "memory_per_user_bytes": 1_000_000.0,
            "latency_p95_ms": 10.0,
            "latency_p99_ms": 20.0,
            "throughput_per_second": float(level),
            "error_rate": 0.001,
        },
        worst_metrics={"cpu_p95_percent": 62.0},
        stability_metrics={},
        warnings=(),
        failures=(),
    )


def cycle_inputs():
    levels = [level_agg(level) for level in (500, 1000, 2500, 5000)]
    scaling = ScalingResult(passed=True, checks=(), first_degradation_level=None)
    regression = RegressionResult(
        passed=True, checks=(), compared_levels=(500, 1000, 2500, 5000)
    )
    capacity = CapacityResult(
        safe_capacity=5000,
        conditional_capacity=None,
        tested_ceiling=5000,
        verdict=CapacityVerdict.PASS,
        first_degradation_level=None,
        notes=(),
    )
    controls = {
        "idle": {"verdict": "PASS", "notes": "idle control clean"},
        "webgl": {"verdict": "PASS", "notes": "20 WebGL clients clean"},
        "primary_bottleneck": "sql.p95_ms",
        "run_checksums": {},
        "run_numbers": {},
        "dashboard_run_id": RUN_ID,
    }
    dataset = {
        "seed": 20260802,
        "row_counts": {
            "accounts": 6000,
            "characters": 12000,
            "guilds": 200,
            "parties": 500,
        },
    }
    recommendations = {
        "remediation": ["keep sql p95 below warning zone"],
        "a5": ["prepared-statement review"],
    }
    return levels, scaling, regression, capacity, controls, dataset, recommendations


class ReportingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.artifact_root = Path(self._tmp.name) / "root"
        self.artifact_root.mkdir()
        self.raw_dir = Path(self._tmp.name) / "raw"

    def run_dir(self) -> Path:
        return (
            self.artifact_root
            / "artifacts"
            / "performance"
            / "a3"
            / CYCLE
            / "runs"
            / RUN_ID
        )

    def cycle_dir(self) -> Path:
        return self.artifact_root / "artifacts" / "performance" / "a3" / CYCLE

    def source_dir(self) -> Path:
        return self.artifact_root / "raw-run-src"

    def write_run(self, **overrides):
        source_files = overrides.pop("source_files", None) or build_source_files(
            self.source_dir()
        )
        kwargs = dict(
            artifact_root=self.artifact_root,
            baseline_cycle_id=CYCLE,
            run_payload=run_payload(),
            summary_payload=summary_payload(),
            timeseries_rows=timeseries_rows(),
            workload_rows=workload_rows(),
            slo_result=slo_result_dict(),
            anomalies=[],
            prometheus_queries=prometheus_queries(),
            source_files=source_files,
        )
        kwargs.update(overrides)
        return write_run_artifacts(**kwargs)

    def write_cycle(self, **overrides):
        inputs = overrides.pop("inputs", cycle_inputs())
        levels, scaling, regression, capacity, controls, dataset, recommendations = inputs
        kwargs = dict(
            artifact_root=self.artifact_root,
            baseline_cycle_id=CYCLE,
            manifest=json.loads(MANIFEST_PATH.read_text(encoding="utf-8")),
            level_results=levels,
            scaling_result=scaling,
            regression_result=regression,
            capacity_result=capacity,
            controls=controls,
            dataset_summary=dataset,
            anomalies=[],
            recommendations=recommendations,
        )
        kwargs.update(overrides)
        return write_cycle_reports(**kwargs)


class RunArtifactStructureTests(ReportingTestBase):
    def test_exact_run_directory_and_files(self):
        result = self.write_run()
        self.assertIsInstance(result, RunArtifactResult)
        expected_dir = self.run_dir()
        self.assertTrue(expected_dir.is_dir())
        for relative in REQUIRED_RUN_FILES:
            self.assertTrue((expected_dir / relative).is_file(), relative)
        self.assertTrue(result.complete)
        self.assertEqual(result.baseline_cycle_id, CYCLE)
        self.assertEqual(result.run_id, RUN_ID)

    def test_overwrite_refused_by_default(self):
        self.write_run()
        with self.assertRaises(ArtifactError):
            self.write_run()

    def test_missing_source_key_rejected(self):
        sources = build_source_files(self.source_dir())
        del sources["collectors/sar.log"]
        with self.assertRaises(ArtifactError):
            self.write_run(source_files=sources)

    def test_unknown_source_key_rejected(self):
        sources = build_source_files(self.source_dir())
        extra = self.source_dir() / "extra.log"
        extra.write_text("x", encoding="utf-8")
        sources["extra.log"] = extra
        with self.assertRaises(ArtifactError):
            self.write_run(source_files=sources)

    def test_missing_source_file_rejected(self):
        sources = build_source_files(self.source_dir())
        sources["collectors/sar.log"].unlink()
        with self.assertRaises(ArtifactError):
            self.write_run(source_files=sources)

    def test_source_symlink_rejected(self):
        sources = build_source_files(self.source_dir())
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ArtifactError):
                self.write_run(source_files=sources)

    def test_artifact_root_symlink_rejected(self):
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ArtifactError):
                self.write_run()

    def test_source_outside_artifact_root_rejected(self):
        outside = Path(self._tmp.name) / "outside"
        mapping = {}
        for key in SOURCE_KEYS:
            path = outside / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
            mapping[key] = path
        with self.assertRaises(ArtifactError):
            self.write_run(source_files=mapping)

    def test_identifier_traversal_rejected(self):
        payload = run_payload()
        payload["run_id"] = "../escape"
        with self.assertRaises(ValueError):
            self.write_run(run_payload=payload)
        with self.assertRaises(ValueError):
            self.write_run(baseline_cycle_id="a/b")

    def test_checksums_correct_and_sorted(self):
        self.write_run()
        checksums = read_json(self.run_dir() / "checksums.json")
        self.assertEqual(checksums["version"], 1)
        self.assertEqual(checksums["baseline_cycle_id"], CYCLE)
        self.assertEqual(checksums["run_id"], RUN_ID)
        files = checksums["files"]
        paths = [entry["path"] for entry in files]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertNotIn("checksums.json", paths)
        self.assertEqual(len(paths), len(REQUIRED_RUN_FILES) - 1)
        for entry in files:
            target = self.run_dir() / entry["path"]
            self.assertEqual(entry["sha256"], sha256_file(target))
            self.assertEqual(entry["size_bytes"], target.stat().st_size)
            self.assertFalse(entry["path"].startswith("/"))

    def test_no_temp_files_after_success(self):
        self.write_run()
        self.assertEqual(list(self.run_dir().rglob("*.tmp")), [])

    def test_failed_run_has_no_checksums(self):
        sources = build_source_files(self.source_dir())
        sources["collectors/sar.log"].unlink()
        with self.assertRaises(ArtifactError):
            self.write_run(source_files=sources)
        self.assertFalse((self.run_dir() / "checksums.json").exists())

    def test_sources_copied_byte_for_byte(self):
        self.write_run()
        for key in SOURCE_KEYS:
            source = self.source_dir() / key
            copied = self.run_dir() / key
            self.assertEqual(source.read_bytes(), copied.read_bytes(), key)


class JsonArtifactTests(ReportingTestBase):
    def test_stable_bytes(self):
        self.write_run()
        first = (self.run_dir() / "run.json").read_bytes()
        second_root = Path(self._tmp.name) / "root2"
        second_root.mkdir()
        original = self.artifact_root
        self.artifact_root = second_root
        self.write_run()
        second = (self.run_dir() / "run.json").read_bytes()
        self.artifact_root = original
        self.assertEqual(first, second)

    def test_nan_rejected(self):
        payload = summary_payload()
        payload["median_metrics"] = {"latency_p95_ms": float("nan")}
        with self.assertRaises(ValueError):
            self.write_run(summary_payload=payload)

    def test_missing_identity_field_rejected(self):
        payload = run_payload()
        del payload["manifest_id"]
        with self.assertRaises(ArtifactError):
            self.write_run(run_payload=payload)

    def test_absolute_path_rejected(self):
        payload = run_payload()
        payload["note"] = "/etc/passwd"
        with self.assertRaises(ArtifactError):
            self.write_run(run_payload=payload)

    def test_enum_serialization_in_slo_verdict(self):
        result = evaluate_run_slos(valid_bundle(), slo_thresholds())
        self.write_run(slo_result=result)
        verdict = read_json(self.run_dir() / "slo-verdict.json")
        self.assertEqual(verdict["status"], "PASS")
        self.assertTrue(verdict["evaluations"])
        first = verdict["evaluations"][0]
        self.assertIn(
            first["verdict"], ("PASS", "PASS_WITH_WARNING", "FAIL", "BLOCKED")
        )
        self.assertEqual(verdict["evaluations"][0]["metric"], "cpu.median_percent")

    def test_secret_marker_rejected(self):
        payload = summary_payload()
        payload["primary_bottleneck"] = "db_password leak"
        with self.assertRaises(ValueError):
            self.write_run(summary_payload=payload)


class CsvArtifactTests(ReportingTestBase):
    TIMESERIES_HEADER = "timestamp,phase,active_users,cpu_percent,memory_rss_bytes,tick_latency_ms,packet_processing_ms,sql_latency_ms,script_latency_ms,storage_utilization_percent,storage_await_ms,network_utilization_percent"
    WORKLOAD_HEADER = "timestamp,phase,active_users,category,event_count,error_count"

    def _read_csv_text(self, name):
        return (self.run_dir() / name).read_text(encoding="utf-8")

    def test_exact_headers(self):
        self.write_run()
        self.assertEqual(
            self._read_csv_text("timeseries.csv").splitlines()[0],
            self.TIMESERIES_HEADER,
        )
        self.assertEqual(
            self._read_csv_text("workload.csv").splitlines()[0],
            self.WORKLOAD_HEADER,
        )

    def test_rows_sorted(self):
        rows = timeseries_rows()
        rows[0], rows[2] = rows[2], rows[0]
        self.write_run(timeseries_rows=rows)
        lines = self._read_csv_text("timeseries.csv").splitlines()[1:]
        timestamps = [int(line.split(",")[0]) for line in lines]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_invalid_phase_rejected(self):
        rows = timeseries_rows()
        rows[0]["phase"] = "NOT_A_PHASE"
        with self.assertRaises(ArtifactError):
            self.write_run(timeseries_rows=rows)

    def test_negative_users_rejected(self):
        rows = timeseries_rows()
        rows[0]["active_users"] = -1
        with self.assertRaises(ArtifactError):
            self.write_run(timeseries_rows=rows)

    def test_bool_numeric_rejected(self):
        rows = timeseries_rows()
        rows[0]["cpu_percent"] = True
        with self.assertRaises(ArtifactError):
            self.write_run(timeseries_rows=rows)

    def test_non_finite_rejected(self):
        rows = timeseries_rows()
        rows[0]["cpu_percent"] = float("inf")
        with self.assertRaises(ArtifactError):
            self.write_run(timeseries_rows=rows)

    def test_formula_injection_rejected(self):
        rows = timeseries_rows()
        rows[0]["phase"] = "=STEADY_STATE"
        with self.assertRaises(ArtifactError):
            self.write_run(timeseries_rows=rows)

    def test_workload_category_and_counts(self):
        rows = workload_rows()
        rows[0]["category"] = "farming"
        with self.assertRaises(ArtifactError):
            self.write_run(workload_rows=rows)
        rows = workload_rows()
        rows[0]["event_count"] = -1
        with self.assertRaises(ArtifactError):
            self.write_run(workload_rows=rows)

    def test_deterministic_newlines(self):
        self.write_run()
        data = (self.run_dir() / "timeseries.csv").read_bytes()
        self.assertNotIn(b"\r", data)


class CycleReportTests(ReportingTestBase):
    TECHNICAL_HEADINGS = [
        "# A3 Technical Baseline Report",
        "## Executive Result",
        "## Manifest and Reproducibility",
        "## Reference Topology",
        "## Synthetic Dataset",
        "## Control Runs",
        "## Per-Level Results",
        "## SLO Evaluation",
        "## Scaling Analysis",
        "## Regression Analysis",
        "## Anomalies",
        "## Bottleneck Attribution",
        "## Capacity Determination",
        "## A4 Readiness",
        "## A5 Optimization Recommendations",
        "## Artifact Integrity and Retention",
    ]

    EXECUTIVE_HEADINGS = [
        "# A3 Executive Summary",
        "## Capacity",
        "## First Degradation",
        "## Primary Bottleneck",
        "## A4 Readiness",
        "## Required Remediation",
    ]

    def test_all_cycle_files_present(self):
        result = self.write_cycle()
        self.assertIsInstance(result, CycleReportResult)
        for relative in REQUIRED_CYCLE_FILES:
            self.assertTrue((self.cycle_dir() / relative).is_file(), relative)

    def test_technical_report_headings_and_content(self):
        self.write_cycle()
        text = (self.cycle_dir() / "technical-report.md").read_text(encoding="utf-8")
        for heading in self.TECHNICAL_HEADINGS:
            self.assertIn(heading, text)
        self.assertIn(MANIFEST_ID, text)
        self.assertIn("f82d9b00e28d6b8dba6abddce90ed50a433d42a1", text)
        for level in ("500", "1000", "2500", "5000"):
            self.assertIn(f"| {level} |", text)
        self.assertIn("20260802", text)
        self.assertIn("Safe Capacity", text)
        self.assertIn("Conditional Capacity", text)
        self.assertIn("Tested Ceiling", text)
        self.assertIn("sql.p95_ms", text)
        self.assertIn("READY", text)
        self.assertIn("external to Git", text)

    def test_executive_summary(self):
        self.write_cycle()
        text = (self.cycle_dir() / "executive-summary.md").read_text(encoding="utf-8")
        for heading in self.EXECUTIVE_HEADINGS:
            self.assertIn(heading, text)
        self.assertIn("Safe Capacity", text)
        self.assertIn("A4 readiness", text)

    def test_a4_readiness_derivations(self):
        levels, scaling, regression, capacity, controls, dataset, recs = cycle_inputs()
        conditional = dataclasses.replace(
            capacity, verdict=CapacityVerdict.PASS_WITH_WARNING
        )
        self.write_cycle(
            inputs=(levels, scaling, regression, conditional, controls, dataset, recs)
        )
        text = (self.cycle_dir() / "executive-summary.md").read_text(encoding="utf-8")
        self.assertIn("CONDITIONAL", text)

        not_ready = dataclasses.replace(
            capacity, verdict=CapacityVerdict.FAIL, safe_capacity=None
        )
        second = Path(self._tmp.name) / "root2"
        second.mkdir()
        original = self.artifact_root
        self.artifact_root = second
        self.write_cycle(
            inputs=(levels, scaling, regression, not_ready, controls, dataset, recs)
        )
        text = (self.cycle_dir() / "executive-summary.md").read_text(encoding="utf-8")
        self.assertIn("NOT READY", text)
        self.artifact_root = original

    def test_comparison_csv_exact_rows(self):
        self.write_cycle()
        lines = (self.cycle_dir() / "comparison.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines[0],
            "load_level,valid_run_count,verdict,cpu_p95_percent,memory_per_user_bytes,latency_p95_ms,latency_p99_ms,throughput_per_second,error_rate,scaling_passed,regression_passed",
        )
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[1].split(",")[:3], ["500", "3", "PASS"])

    def test_comparison_csv_missing_level_explicit(self):
        levels, scaling, regression, capacity, controls, dataset, recs = cycle_inputs()
        levels = [level for level in levels if level.load_level != 2500]
        blocked_scaling = ScalingResult(
            passed=False, checks=(), first_degradation_level=2500
        )
        self.write_cycle(
            inputs=(levels, blocked_scaling, regression, capacity, controls, dataset, recs)
        )
        lines = (self.cycle_dir() / "comparison.csv").read_text(encoding="utf-8").splitlines()
        row_2500 = lines[3].split(",")
        self.assertEqual(row_2500[0], "2500")
        self.assertEqual(row_2500[1], "0")
        self.assertEqual(row_2500[2], "BLOCKED")
        self.assertEqual(row_2500[3], "")

    def test_capacity_and_result_files(self):
        self.write_cycle()
        capacity = read_json(self.cycle_dir() / "capacity.json")
        self.assertEqual(capacity["version"], 1)
        self.assertEqual(capacity["safe_capacity"], 5000)
        self.assertEqual(capacity["verdict"], "PASS")
        self.assertEqual(read_json(self.cycle_dir() / "scaling.json")["version"], 1)
        self.assertEqual(read_json(self.cycle_dir() / "regression.json")["version"], 1)

    def test_retention_policy(self):
        self.write_cycle()
        retention = read_json(self.cycle_dir() / "retention.json")
        self.assertEqual(retention["summary_json"], "permanent")
        self.assertEqual(retention["manifest_json"], "permanent")
        self.assertEqual(retention["csv"], "permanent")
        self.assertEqual(retention["grafana_dashboard"], "permanent")
        self.assertEqual(retention["raw_prometheus_minimum_days"], 180)
        self.assertEqual(retention["linux_logs_minimum_days"], 180)
        self.assertEqual(retention["service_logs_minimum_days"], 180)
        self.assertIs(retention["external_storage_required"], True)

    def test_artifact_index(self):
        self.write_cycle()
        index = read_json(self.cycle_dir() / "artifact-index.json")
        self.assertEqual(index["version"], 1)
        self.assertEqual(index["baseline_cycle_id"], CYCLE)
        self.assertEqual(index["manifest_id"], MANIFEST_ID)
        self.assertIn("runs", index)
        self.assertIn("cycle_files", index)
        self.assertIn("external_raw_artifact_policy", index)

    def test_cycle_checksums_last_and_correct(self):
        self.write_cycle()
        checksums = read_json(self.cycle_dir() / "checksums.json")
        paths = [entry["path"] for entry in checksums["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertNotIn("checksums.json", paths)
        self.assertFalse(any(path.startswith("runs/") for path in paths))
        for entry in checksums["files"]:
            target = self.cycle_dir() / entry["path"]
            self.assertEqual(entry["sha256"], sha256_file(target))

    def test_reports_do_not_embed_raw_logs(self):
        self.write_cycle()
        for name in ("technical-report.md", "executive-summary.md", "artifact-index.json"):
            text = (self.cycle_dir() / name).read_text(encoding="utf-8")
            self.assertNotIn("synthetic collectors/", text)


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


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        cls.thresholds = read_json(THRESHOLDS_PATH)

    def test_committed_dashboard_valid(self):
        result = validate_dashboard_thresholds(self.dashboard, self.thresholds)
        self.assertEqual(result.errors, ())
        self.assertTrue(result.valid)

    def test_required_panels_and_variables(self):
        result = validate_dashboard_thresholds(self.dashboard, self.thresholds)
        for title in REQUIRED_PANEL_TITLES:
            self.assertIn(title, result.checked_panels)
        variables = {
            variable.get("name")
            for variable in self.dashboard["templating"]["list"]
        }
        for name in REQUIRED_VARIABLES:
            self.assertIn(name, variables)

    def test_every_task7_threshold_ref_represented(self):
        result = validate_dashboard_thresholds(self.dashboard, self.thresholds)
        required = {".".join(spec.threshold_path) for spec in _METRIC_SPECS}
        self.assertEqual(required, required & set(result.checked_thresholds))
        self.assertEqual(len(required), 49)

    def test_committed_dashboard_keeps_placeholders(self):
        text = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("${A3_BASELINE_CYCLE_ID}", text)
        self.assertIn("${A3_MANIFEST_ID}", text)
        self.assertIn("${A3_RUN_ID}", text)

    def test_mismatch_detection(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        dashboard["panels"][1]["thresholds"][0]["value"] += 1
        result = validate_dashboard_thresholds(dashboard, self.thresholds)
        self.assertFalse(result.valid)
        self.assertTrue(any("does not match" in error for error in result.errors))

    def test_unknown_reference_rejected(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        dashboard["panels"][0]["thresholds"] = [
            {"a3_threshold_ref": "bogus.ref", "value": 1}
        ]
        result = validate_dashboard_thresholds(dashboard, self.thresholds)
        self.assertFalse(result.valid)
        self.assertTrue(any("unknown threshold reference" in e for e in result.errors))

    def test_missing_reference_rejected(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        dashboard["panels"][0]["thresholds"] = [{"a3_threshold_ref": "", "value": 1}]
        result = validate_dashboard_thresholds(dashboard, self.thresholds)
        self.assertFalse(result.valid)

    def test_duplicate_conflicting_references_rejected(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        dashboard["panels"][1]["thresholds"].append(
            {
                "a3_threshold_ref": dashboard["panels"][1]["thresholds"][0][
                    "a3_threshold_ref"
                ],
                "value": -12345,
            }
        )
        result = validate_dashboard_thresholds(dashboard, self.thresholds)
        self.assertFalse(result.valid)
        self.assertTrue(any("conflicting" in error for error in result.errors))

    def test_warning_value_validation(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        for panel in dashboard["panels"]:
            for line in panel.get("thresholds", []):
                if line.get("warning") is True:
                    line["value"] = line["value"] + 1
                    result = validate_dashboard_thresholds(dashboard, self.thresholds)
                    self.assertFalse(result.valid)
                    self.assertTrue(any("warning" in e for e in result.errors))
                    return
        self.fail("no warning line found in dashboard")

    def test_zero_tolerance_warning_rejected(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        dashboard["panels"][1]["thresholds"].append(
            {"a3_threshold_ref": "memory.oom_max", "value": 0, "warning": True}
        )
        result = validate_dashboard_thresholds(dashboard, self.thresholds)
        self.assertFalse(result.valid)
        self.assertTrue(any("zero-tolerance" in e for e in result.errors))

    def test_validation_does_not_mutate(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        before = json.dumps(dashboard, sort_keys=True)
        validate_dashboard_thresholds(dashboard, self.thresholds)
        self.assertEqual(json.dumps(dashboard, sort_keys=True), before)

    def test_render_runtime_exact(self):
        template = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        before = json.dumps(template, sort_keys=True)
        rendered = render_dashboard_runtime(template, CYCLE, MANIFEST_ID, RUN_ID)
        text = json.dumps(rendered)
        self.assertIn(CYCLE, text)
        self.assertIn(MANIFEST_ID, text)
        self.assertIn(RUN_ID, text)
        self.assertNotIn("${A3_", text)
        self.assertEqual(json.dumps(template, sort_keys=True), before)

    def test_render_rejects_invalid_identifiers(self):
        template = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            render_dashboard_runtime(template, "../bad", MANIFEST_ID, RUN_ID)
        with self.assertRaises(ValueError):
            render_dashboard_runtime(template, CYCLE, "", RUN_ID)


class SecurityAndDocsTests(ReportingTestBase):
    def test_prometheus_credentials_rejected(self):
        queries = prometheus_queries()
        queries["queries"][0]["url"] = "http://user:pass@127.0.0.1:9090/api/v1/query_range"
        with self.assertRaises(ArtifactError):
            self.write_run(prometheus_queries=queries)

    def test_prometheus_secret_marker_rejected(self):
        queries = prometheus_queries()
        queries["queries"][0]["expr"] = "api_key_metric"
        with self.assertRaises(ValueError):
            self.write_run(prometheus_queries=queries)

    def test_prometheus_step_enforced(self):
        queries = prometheus_queries()
        queries["step"] = 10
        with self.assertRaises(ArtifactError):
            self.write_run(prometheus_queries=queries)

    def test_anomalies_sorted(self):
        anomalies = [
            {"severity": "warning", "code": "B", "timestamp": 2.0, "message": "b"},
            {"severity": "error", "code": "B", "timestamp": 1.0, "message": "a"},
            {"severity": "error", "code": "A", "timestamp": 3.0, "message": "c"},
        ]
        self.write_run(anomalies=anomalies)
        data = read_json(self.run_dir() / "anomalies.json")
        self.assertEqual(data["version"], 1)
        keys = [
            (a["severity"], a["code"], a["timestamp"], a["message"])
            for a in data["anomalies"]
        ]
        self.assertEqual(keys, sorted(keys))

    def test_docs_required_statements(self):
        text = DOC_PATH.read_text(encoding="utf-8")
        self.assertIn("180", text)
        self.assertIn("permanent", text)
        self.assertIn("external", text.lower())
        self.assertIn("production player data", text.lower())


class DeterminismTests(ReportingTestBase):
    def test_cycle_artifacts_byte_identical(self):
        self.write_cycle()
        first_root = self.artifact_root
        second = Path(self._tmp.name) / "root2"
        second.mkdir()
        self.artifact_root = second
        self.write_cycle()
        for name in (
            "technical-report.md",
            "executive-summary.md",
            "comparison.csv",
            "capacity.json",
            "scaling.json",
            "regression.json",
            "artifact-index.json",
            "retention.json",
            "checksums.json",
        ):
            first = (first_root / "artifacts" / "performance" / "a3" / CYCLE / name).read_bytes()
            second_bytes = (self.cycle_dir() / name).read_bytes()
            self.assertEqual(first, second_bytes, name)
        self.artifact_root = first_root

    def test_created_utc_from_caller_only(self):
        self.write_run()
        payload = read_json(self.run_dir() / "run.json")
        self.assertEqual(payload["created_utc"], "2026-08-02T20:00:00Z")


if __name__ == "__main__":
    unittest.main()
