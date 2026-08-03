# A3 Baseline and SLO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible A3 performance-baseline toolchain that prepares the environment, runs controlled 500/1,000/2,500/5,000-user scenarios, validates run quality, evaluates SLOs, and emits auditable JSON, CSV, Markdown, Prometheus, and Grafana artifacts.

**Architecture:** Implement A3 as a Python 3.11+ orchestration and analysis package under `tools/performance/a3/`, keeping load generation, manifest capture, telemetry collection, validation, SLO evaluation, reporting, and approval records as separate modules with stable JSON interfaces. Reuse the existing rAthena observability outputs as authoritative application telemetry, add Prometheus/Grafana assets as versioned configuration, and keep large runtime artifacts outside Git while committing schemas, templates, tests, and checksums.

**Tech Stack:** Python 3.11/3.12 standard library, `unittest`, YAML/JSON configuration, Prometheus, node_exporter, MariaDB exporter, Grafana dashboard JSON, Linux `pidstat`/`sar`/`vmstat`/`iostat`, existing rAthena observability textfile metrics.

## Global Constraints

- Reference server: bare-metal Ubuntu Server 24.04.4 LTS, 8 physical cores / 16 threads, 32 GB RAM, NVMe SSD, 1 Gbps network.
- Runtime topology: one login-server, one char-server, one map-server, one MariaDB 10.11 LTS instance on the reference server.
- Load generator and 20-client WebGL validation machine must be physically separate from the reference server.
- Load levels: 500, 1,000, 2,500, and 5,000 synthetic concurrent users, plus 20 real WebGL clients not counted in the synthetic total.
- Each load level requires three valid runs.
- Per run: 10-minute preconditioning, 5-minute linear ramp, 20-minute steady state, 5-minute cool-down.
- Prometheus, rAthena snapshot, and Linux cross-check sampling interval: 5 seconds.
- Use a deterministic synthetic dataset with 6,000 accounts, 12,000 characters, 200 guilds, and 500 parties.
- Prometheus is the authoritative metric source; Linux tools are independent cross-checks.
- No result may combine runs from different manifest IDs.
- Large runtime artifacts remain outside Git; Git stores schemas, templates, reports, indexes, and checksums.
- A3 must not implement sharding, multi-node deployment, deep MariaDB tuning, failover, distributed database, or maximum login-burst testing.

---

## Planned File Structure

```text
tools/performance/a3/
├── __init__.py
├── cli.py
├── config.py
├── models.py
├── manifest.py
├── dataset.py
├── lifecycle.py
├── collectors.py
├── prometheus.py
├── validity.py
├── slo.py
├── scaling.py
├── reporting.py
├── approval.py
├── io.py
├── schemas/
│   ├── manifest.schema.json
│   ├── run.schema.json
│   ├── summary.schema.json
│   ├── slo-verdict.schema.json
│   ├── anomalies.schema.json
│   └── approved-baseline.schema.json
├── config/
│   ├── a3.example.json
│   ├── slo-thresholds.json
│   ├── workload-profile.json
│   ├── prometheus.yml
│   └── grafana-dashboard.json
└── tests/
    ├── test_config.py
    ├── test_manifest.py
    ├── test_dataset.py
    ├── test_lifecycle.py
    ├── test_validity.py
    ├── test_slo.py
    ├── test_scaling.py
    ├── test_reporting.py
    ├── test_approval.py
    └── fixtures/
        ├── valid_manifest.json
        ├── valid_run.json
        ├── invalid_run.json
        ├── steady_state_timeseries.csv
        └── previous_baseline.json

docs/observability/A3_BASELINE_RUNBOOK.md
docs/observability/A3_ARTIFACT_FORMAT.md
.github/workflows/a3-baseline-tests.yml
```

Each module owns one responsibility. Cross-module communication uses immutable dataclasses from `models.py` and JSON files conforming to the committed schemas.

---

### Task 1: Establish A3 package, models, configuration, and schema validation

**Files:**
- Create: `tools/performance/a3/__init__.py`
- Create: `tools/performance/a3/models.py`
- Create: `tools/performance/a3/config.py`
- Create: `tools/performance/a3/io.py`
- Create: `tools/performance/a3/config/a3.example.json`
- Create: `tools/performance/a3/config/slo-thresholds.json`
- Create: `tools/performance/a3/config/workload-profile.json`
- Create: `tools/performance/a3/schemas/manifest.schema.json`
- Create: `tools/performance/a3/schemas/run.schema.json`
- Create: `tools/performance/a3/schemas/summary.schema.json`
- Create: `tools/performance/a3/schemas/slo-verdict.schema.json`
- Create: `tools/performance/a3/schemas/anomalies.schema.json`
- Create: `tools/performance/a3/schemas/approved-baseline.schema.json`
- Test: `tools/performance/a3/tests/test_config.py`

**Interfaces:**
- Produces: `A3Config`, `LoadLevel`, `RunPhase`, `RunStatus`, `MetricVerdict`, `CapacityVerdict`, `read_json(path)`, `write_json_atomic(path, value)`, `sha256_file(path)`.
- Consumes: Python standard library only.

- [ ] **Step 1: Write failing configuration tests**

```python
from pathlib import Path
import unittest

from tools.performance.a3.config import load_config


class ConfigTests(unittest.TestCase):
    def test_example_config_has_exact_load_levels_and_sampling(self):
        config = load_config(Path("tools/performance/a3/config/a3.example.json"))
        self.assertEqual(config.load_levels, (500, 1000, 2500, 5000))
        self.assertEqual(config.valid_runs_per_level, 3)
        self.assertEqual(config.scrape_interval_seconds, 5)
        self.assertEqual(config.webgl_clients, 20)

    def test_workload_profile_sums_to_one(self):
        config = load_config(Path("tools/performance/a3/config/a3.example.json"))
        self.assertAlmostEqual(sum(config.workload_mix.values()), 1.0)
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
python -m unittest tools.performance.a3.tests.test_config -v
```

Expected: import failure because `tools.performance.a3.config` does not exist.

- [ ] **Step 3: Implement typed models and strict configuration loading**

Implement frozen dataclasses and enums in `models.py`. `load_config()` must reject missing keys, unknown load levels, workload sums outside `1.0 +/- 1e-9`, non-5-second sampling, non-3 run count, or non-20 WebGL client count. Use explicit `ValueError` messages naming the invalid field.

- [ ] **Step 4: Implement atomic JSON and SHA-256 helpers**

`write_json_atomic()` must write to a sibling temporary file, `fsync`, and `os.replace()` the destination. `sha256_file()` must stream in 1 MiB chunks.

- [ ] **Step 5: Add exact example configuration**

`a3.example.json` must encode:

```json
{
  "load_levels": [500, 1000, 2500, 5000],
  "valid_runs_per_level": 3,
  "webgl_clients": 20,
  "preconditioning_seconds": 600,
  "ramp_seconds": 300,
  "steady_state_seconds": 1200,
  "cooldown_seconds": 300,
  "scrape_interval_seconds": 5,
  "workload_mix_tolerance_percentage_points": 5,
  "prometheus_missing_data_limit_seconds": 15,
  "target_concurrency_floor_ratio": 0.98
}
```

- [ ] **Step 6: Run package tests**

Run:

```bash
python -m unittest tools.performance.a3.tests.test_config -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add tools/performance/a3
git commit -m "feat: add A3 configuration and schemas"
```

---

### Task 2: Build reproducibility manifest capture and freeze verification

**Files:**
- Create: `tools/performance/a3/manifest.py`
- Create: `tools/performance/a3/tests/test_manifest.py`
- Create: `tools/performance/a3/tests/fixtures/valid_manifest.json`

**Interfaces:**
- Consumes: `A3Config`, `sha256_file()`, `write_json_atomic()`.
- Produces: `capture_manifest(repo_root: Path, config: A3Config) -> dict`, `verify_manifest(expected: dict, actual: dict) -> list[str]`, `manifest_id(manifest: dict) -> str`.

- [ ] **Step 1: Write failing manifest tests**

Cover exact equality, binary checksum drift, workload checksum drift, kernel drift, dataset seed drift, and stable manifest ID generation.

```python
def test_verify_manifest_reports_binary_change(self):
    expected = self.load_fixture()
    actual = copy.deepcopy(expected)
    actual["build"]["map_server_sha256"] = "0" * 64
    self.assertEqual(
        verify_manifest(expected, actual),
        ["build.map_server_sha256 changed"],
    )
```

- [ ] **Step 2: Run test and verify failure**

```bash
python -m unittest tools.performance.a3.tests.test_manifest -v
```

Expected: import failure.

- [ ] **Step 3: Implement manifest capture**

Capture these groups exactly: source, build, protocol, rAthena configuration, game content, database, operating system, hardware, observability, and load generation. Shell commands must be invoked with argument arrays and timeout handling. Store command failures under `capture_errors`; a manifest with any capture error is not eligible for execution.

- [ ] **Step 4: Implement canonical manifest hashing**

Serialize with sorted keys, UTF-8, and separators `(',', ':')`. Exclude only `manifest_sha256` from the preimage. Generate IDs in the form:

```text
a3-YYYYMMDD-<git-short-sha>-ubuntu2404-8c16t-32g-<sequence>
```

- [ ] **Step 5: Implement freeze comparison**

`verify_manifest()` must recursively compare all frozen keys and return deterministic dotted-path messages. It must not ignore binary, configuration, PACKETVER, script, database, kernel, exporter, workload, harness, dataset, or power-setting changes.

- [ ] **Step 6: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_manifest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add tools/performance/a3/manifest.py tools/performance/a3/tests/test_manifest.py tools/performance/a3/tests/fixtures/valid_manifest.json
git commit -m "feat: capture and verify A3 manifests"
```

---

### Task 3: Implement deterministic synthetic dataset planning and verification

**Files:**
- Create: `tools/performance/a3/dataset.py`
- Create: `tools/performance/a3/tests/test_dataset.py`

**Interfaces:**
- Consumes: database connection command configured through `A3Config`; no production data.
- Produces: `DatasetPlan`, `build_dataset_plan(seed: int)`, `emit_dataset_sql(plan, output_path)`, `verify_dataset_counts(actual, plan)`.

- [ ] **Step 1: Write failing tests for deterministic planning**

Tests must prove that identical seeds produce byte-identical plans and different seeds alter assignments without changing target row counts.

- [ ] **Step 2: Run tests and verify failure**

```bash
python -m unittest tools.performance.a3.tests.test_dataset -v
```

- [ ] **Step 3: Implement deterministic distributions**

Use `random.Random(seed)`. Produce exactly 6,000 accounts, 12,000 characters, 200 guilds, and 500 parties. Assign inventory/storage/quest profiles from fixed named tiers: `empty`, `light`, `medium`, `heavy`. Emit stable IDs and stable ordering.

- [ ] **Step 4: Emit SQL and verification metadata**

Generate a SQL file and a sidecar JSON containing seed, row counts, profile counts, SHA-256, and expected foreign-key relationships. Never read production player rows.

- [ ] **Step 5: Add consistency checks**

Reject duplicate identifiers, missing account-character links, invalid guild membership, invalid party membership, and row-count mismatches.

- [ ] **Step 6: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_dataset -v
```

- [ ] **Step 7: Commit**

```bash
git add tools/performance/a3/dataset.py tools/performance/a3/tests/test_dataset.py
git commit -m "feat: add deterministic A3 dataset planning"
```

---

### Task 4: Implement lifecycle state machine and command execution boundary

**Files:**
- Create: `tools/performance/a3/lifecycle.py`
- Create: `tools/performance/a3/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `A3Config`, `RunPhase`, manifest verifier, collector controller, harness adapter.
- Produces: `RunController`, `RunCommand`, `RunEvent`, `LifecycleError`, `CatastrophicRunError`.

- [ ] **Step 1: Write failing transition tests**

Allowed path:

```text
ENVIRONMENT_CHECK -> SERVICE_START -> PRECONDITIONING -> RAMP_UP -> STEADY_STATE -> COOL_DOWN -> VALIDATION -> REPORTING
```

Catastrophic path:

```text
ANY ACTIVE STATE -> ABORTED -> ARTIFACT_CAPTURE -> ROOT_CAUSE_ANALYSIS
```

Tests must reject skipping preconditioning, re-entering steady state, or reporting before validation.

- [ ] **Step 2: Run tests and verify failure**

```bash
python -m unittest tools.performance.a3.tests.test_lifecycle -v
```

- [ ] **Step 3: Implement the state machine**

Persist every transition as one JSON line containing UTC timestamp, run ID, previous phase, next phase, reason, and command result. Use monotonic time for durations and wall-clock UTC for correlation.

- [ ] **Step 4: Implement command adapter**

All service, collector, and harness commands must pass through `RunCommand(argv: tuple[str, ...], timeout_seconds: int, cwd: Path | None)`. Capture stdout, stderr, return code, start/end UTC, and elapsed monotonic seconds.

- [ ] **Step 5: Implement catastrophic handling**

Crash, data corruption flag, deadlock flag, OOM flag, database inconsistency flag, or environment mismatch must stop the cycle and schedule artifact capture. Non-catastrophic SLO failure must not stop higher load levels.

- [ ] **Step 6: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_lifecycle -v
```

- [ ] **Step 7: Commit**

```bash
git add tools/performance/a3/lifecycle.py tools/performance/a3/tests/test_lifecycle.py
git commit -m "feat: add A3 run lifecycle controller"
```

---

### Task 5: Implement telemetry collectors and Prometheus query layer

**Files:**
- Create: `tools/performance/a3/collectors.py`
- Create: `tools/performance/a3/prometheus.py`
- Create: `tools/performance/a3/config/prometheus.yml`
- Create: `tools/performance/a3/tests/test_collectors.py`
- Create: `tools/performance/a3/tests/test_prometheus.py`

**Interfaces:**
- Consumes: run ID, manifest ID, phase annotations, Prometheus base URL.
- Produces: `CollectorController.start(run_context)`, `CollectorController.stop()`, `PrometheusClient.query_range(expr, start, end, step)`, `MetricSeries`.

- [ ] **Step 1: Write failing collector command tests**

Assert exact 5-second invocation for:

```text
pidstat -h -r -u -w -p ALL 5
sar -u -r -n DEV,TCP,ETCP 5
vmstat -t 5
iostat -x -d 5
```

- [ ] **Step 2: Write failing Prometheus parsing tests**

Use fixture HTTP payloads to verify ordered timestamp/value pairs, missing samples, counter reset detection, and duplicate-series rejection.

- [ ] **Step 3: Implement safe collector process management**

Start each process in its own process group, redirect output to the run artifact directory, and terminate gracefully before force-killing after a fixed timeout. Record command metadata in `collectors.json`.

- [ ] **Step 4: Implement Prometheus range-query client**

Use `urllib.request` from the standard library. Enforce HTTP timeout, JSON status `success`, unique series identity, numeric sample values, and requested 5-second step.

- [ ] **Step 5: Add versioned Prometheus configuration**

Configure 5-second scrapes for rAthena textfile metrics, node_exporter, and MariaDB exporter. Add external labels `baseline_cycle_id`, `manifest_id`, and `run_id` via generated runtime substitution, not hard-coded values.

- [ ] **Step 6: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_collectors tools.performance.a3.tests.test_prometheus -v
```

- [ ] **Step 7: Commit**

```bash
git add tools/performance/a3/collectors.py tools/performance/a3/prometheus.py tools/performance/a3/config/prometheus.yml tools/performance/a3/tests/test_collectors.py tools/performance/a3/tests/test_prometheus.py
git commit -m "feat: collect A3 system and Prometheus telemetry"
```

---

### Task 6: Implement strict run-validity gates and metric-integrity checks

**Files:**
- Create: `tools/performance/a3/validity.py`
- Create: `tools/performance/a3/tests/test_validity.py`
- Create: `tools/performance/a3/tests/fixtures/valid_run.json`
- Create: `tools/performance/a3/tests/fixtures/invalid_run.json`

**Interfaces:**
- Consumes: run metadata, workload summary, collector summary, manifest comparison, metric-series diagnostics.
- Produces: `ValidityResult(valid: bool, reasons: tuple[str, ...])`, `validate_run(run_data)`, `validate_metric_integrity(series_set)`.

- [ ] **Step 1: Write one failing test per validity gate**

Cover:

- concurrency below 98%;
- unexpected disconnects above 1%;
- Prometheus gap above 15 seconds;
- service restart/crash;
- unrelated CPU above 5%;
- network loss above 0.1%;
- workload mix beyond +/-5 percentage points;
- manifest mismatch;
- counter reset;
- timestamp jump;
- duplicate series;
- partial textfile write;
- metric write-error growth.

- [ ] **Step 2: Run tests and verify failure**

```bash
python -m unittest tools.performance.a3.tests.test_validity -v
```

- [ ] **Step 3: Implement deterministic validity evaluation**

Return all reasons in stable rule order. Validity is separate from SLO verdict: an invalid run is never PASS, WARNING, FAIL, or BLOCKED for capacity aggregation; it must be replaced.

- [ ] **Step 4: Implement time synchronization check**

Compare captured NTP/chrony offsets for reference server, load generator, and WebGL machine. Mark invalid when offsets prevent reliable phase-to-metric correlation; default hard limit is 100 ms unless the config specifies a stricter value.

- [ ] **Step 5: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_validity -v
```

- [ ] **Step 6: Commit**

```bash
git add tools/performance/a3/validity.py tools/performance/a3/tests/test_validity.py tools/performance/a3/tests/fixtures/valid_run.json tools/performance/a3/tests/fixtures/invalid_run.json
git commit -m "feat: enforce A3 run validity gates"
```

---

### Task 7: Implement percentile, rate, SLO, warning, and catastrophic verdict engine

**Files:**
- Create: `tools/performance/a3/slo.py`
- Create: `tools/performance/a3/tests/test_slo.py`
- Create: `tools/performance/a3/tests/fixtures/steady_state_timeseries.csv`

**Interfaces:**
- Consumes: valid steady-state samples and `slo-thresholds.json`.
- Produces: `percentile(values, q)`, `counter_rate(samples)`, `evaluate_run_slos(metric_bundle) -> tuple[MetricVerdict, ...]`, `classify_run(metric_verdicts)`.

- [ ] **Step 1: Write failing statistics tests**

Define nearest-rank or linear-interpolation behavior explicitly and test p50, p95, p99, empty input, one value, and counter delta with reset rejection.

- [ ] **Step 2: Write failing threshold tests**

Cover every approved threshold:

- CPU median 75%, p95 85%, >95% no longer than 30 seconds;
- memory 80%, growth 5%, no swap/OOM/allocation failure;
- tick p50 10 ms, p95 25 ms, p99 50 ms, max 100 ms, >50 ms no longer than 10 seconds;
- packet p95 5 ms, p99 10 ms, max 25 ms, drops/rejects 0.01%;
- SQL p95 25 ms, p99 75 ms, max 500 ms, slow 0.5%, failures 0.01%, connection p95 75%, no acquisition failure/deadlock/lock timeout;
- script p95 5 ms, p99 20 ms, max 100 ms, slow 0.5%, failures 0.01%, unknown <=5%;
- login and character selection failures 0.1%, unexpected disconnect 0.5%;
- storage utilization p95 75%, await p95 5 ms, await p99 10 ms;
- network p95 70%, loss/retransmit 0.1%, no socket/listen errors.

- [ ] **Step 3: Implement warning-zone logic**

For positive upper-bound metrics, warning begins at 90% of the hard limit. Zero-tolerance metrics have no warning state. A zero-tolerance event maps to FAIL or BLOCKED using the catastrophic classification table.

- [ ] **Step 4: Implement sustained-duration checks**

Convert sample runs to elapsed durations using timestamps; do not infer duration from sample count alone. Detect CPU >95% for more than 30 seconds, tick >50 ms for more than 10 seconds, packet backlog growth beyond 10 seconds, and storage queue growth beyond 10 seconds.

- [ ] **Step 5: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_slo -v
```

- [ ] **Step 6: Commit**

```bash
git add tools/performance/a3/slo.py tools/performance/a3/tests/test_slo.py tools/performance/a3/tests/fixtures/steady_state_timeseries.csv
git commit -m "feat: evaluate A3 service level objectives"
```

---

### Task 8: Implement cross-level scaling, three-run aggregation, regression, and capacity verdicts

**Files:**
- Create: `tools/performance/a3/scaling.py`
- Create: `tools/performance/a3/tests/test_scaling.py`
- Create: `tools/performance/a3/tests/fixtures/previous_baseline.json`

**Interfaces:**
- Consumes: three valid run summaries per level and optional approved baseline.
- Produces: `aggregate_level(runs)`, `evaluate_scaling(levels)`, `evaluate_regression(current, previous)`, `derive_capacity(level_verdicts) -> CapacityVerdict`.

- [ ] **Step 1: Write failing three-run aggregation tests**

Prove that one failed valid run makes the level FAIL even when the median passes. Prove that one warning run makes PASS-WITH-WARNING when all hard SLOs pass.

- [ ] **Step 2: Write failing scaling tests**

Check p95 growth <=50%, p99 growth <=75%, memory/user <=20% above 500-user baseline, error growth <=2x, and throughput gain >=80% of proportional user gain.

- [ ] **Step 3: Write failing regression-budget tests**

Check CPU 10%, memory/user 10%, p95 latency 15%, p99 latency 20%, throughput -10%, and error +25%, while retaining absolute SLO enforcement.

- [ ] **Step 4: Implement capacity verdicts**

Return highest PASS as Safe Capacity, highest PASS-WITH-WARNING as Conditional Capacity, and highest completed non-BLOCKED level as Tested Ceiling. Return `Not Established` when no load level passes.

- [ ] **Step 5: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_scaling -v
```

- [ ] **Step 6: Commit**

```bash
git add tools/performance/a3/scaling.py tools/performance/a3/tests/test_scaling.py tools/performance/a3/tests/fixtures/previous_baseline.json
git commit -m "feat: aggregate A3 scaling and capacity verdicts"
```

---

### Task 9: Implement artifact generation, Markdown reporting, and Grafana dashboard validation

**Files:**
- Create: `tools/performance/a3/reporting.py`
- Create: `tools/performance/a3/config/grafana-dashboard.json`
- Create: `tools/performance/a3/tests/test_reporting.py`
- Create: `docs/observability/A3_ARTIFACT_FORMAT.md`

**Interfaces:**
- Consumes: manifest, run summaries, SLO verdicts, anomalies, scaling results, regression results, capacity verdict.
- Produces: `write_run_artifacts()`, `write_cycle_reports()`, `validate_dashboard_thresholds()`.

- [ ] **Step 1: Write failing artifact-structure tests**

Assert exact paths under:

```text
artifacts/performance/a3/<baseline_cycle_id>/
```

and required run files: `run.json`, `summary.json`, `timeseries.csv`, `workload.csv`, `slo-verdict.json`, `anomalies.json`, `prometheus-queries.json`, Linux logs, and service logs.

- [ ] **Step 2: Write failing report-content tests**

Technical report must include manifest, topology, dataset, controls, per-level results, SLO table, scaling, anomalies, bottleneck attribution, regression, capacity, and A4/A5 recommendations. Executive summary must include Safe Capacity, Conditional Capacity, Tested Ceiling, first degradation level, primary bottleneck, A4 readiness, and remediation.

- [ ] **Step 3: Implement JSON and CSV writers**

Use stable field order, UTF-8, RFC 3339 UTC timestamps, and atomic writes. CSV time-series rows must include run phase and active users. Comparison CSV must have one row per load level.

- [ ] **Step 4: Implement Grafana dashboard**

Add panels for overview, CPU, memory, tick, packet, SQL, script, storage, network, and scaling. Every threshold line must be generated from `slo-thresholds.json`; tests must fail if dashboard constants diverge.

- [ ] **Step 5: Document artifact format**

Explain file ownership, external raw-artifact storage, checksums, retention, and how Git indexes off-repository artifacts.

- [ ] **Step 6: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_reporting -v
```

- [ ] **Step 7: Commit**

```bash
git add tools/performance/a3/reporting.py tools/performance/a3/config/grafana-dashboard.json tools/performance/a3/tests/test_reporting.py docs/observability/A3_ARTIFACT_FORMAT.md
git commit -m "feat: generate A3 reports and dashboards"
```

---

### Task 10: Implement approval governance and approved-baseline lifecycle

**Files:**
- Create: `tools/performance/a3/approval.py`
- Create: `tools/performance/a3/tests/test_approval.py`

**Interfaces:**
- Consumes: CI-evaluated cycle summary, approver identity, rationale.
- Produces: `create_approval_record()`, `approve_baseline()`, `supersede_baseline()`, states `DRAFT`, `CI_EVALUATED`, `AWAITING_APPROVAL`, `APPROVED`, `REJECTED`, `SUPERSEDED`.

- [ ] **Step 1: Write failing state-transition tests**

Reject direct `DRAFT -> APPROVED`, approval without approver identity, approval without timestamp, approval without manifest ID, and approval of a BLOCKED cycle.

- [ ] **Step 2: Run tests and verify failure**

```bash
python -m unittest tools.performance.a3.tests.test_approval -v
```

- [ ] **Step 3: Implement approval record**

Record approver, UTC timestamp, manifest ID, Git SHA, per-level verdicts, capacity verdict, warnings, and rationale. Sign the canonical JSON with a SHA-256 checksum stored beside the record.

- [ ] **Step 4: Implement supersession**

Approving a new baseline must preserve the previous approval record and create a separate supersession record linking old and new manifest IDs.

- [ ] **Step 5: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_approval -v
```

- [ ] **Step 6: Commit**

```bash
git add tools/performance/a3/approval.py tools/performance/a3/tests/test_approval.py
git commit -m "feat: govern A3 approved baselines"
```

---

### Task 11: Build CLI orchestration for prepare, control, run, evaluate, report, and approve

**Files:**
- Create: `tools/performance/a3/cli.py`
- Create: `tools/performance/a3/tests/test_cli.py`
- Modify: `tools/performance/a3/__init__.py`

**Interfaces:**
- Consumes: all previous task interfaces.
- Produces command line:

```text
python -m tools.performance.a3.cli prepare --config <path>
python -m tools.performance.a3.cli control idle --cycle <id>
python -m tools.performance.a3.cli control webgl --cycle <id>
python -m tools.performance.a3.cli run --cycle <id> --users 500 --run 1
python -m tools.performance.a3.cli evaluate --cycle <id>
python -m tools.performance.a3.cli report --cycle <id>
python -m tools.performance.a3.cli approve --cycle <id> --approver <name> --rationale <text>
```

- [ ] **Step 1: Write failing CLI parser tests**

Test required arguments, allowed load levels, run number 1-3, refusal to run before controls, refusal to evaluate without three valid runs per level, and refusal to approve without CI-evaluated status.

- [ ] **Step 2: Run tests and verify failure**

```bash
python -m unittest tools.performance.a3.tests.test_cli -v
```

- [ ] **Step 3: Implement `prepare`**

Capture manifest, validate config, generate dataset plan, create artifact directories, render Prometheus runtime config, and write cycle state as `DRAFT`.

- [ ] **Step 4: Implement control and run commands**

Enforce lifecycle order. `run` must support non-catastrophic continuation and catastrophic abort semantics. Invalid runs remain stored but do not count toward the required three.

- [ ] **Step 5: Implement evaluate/report/approve commands**

`evaluate` computes provisional verdicts and changes state to `CI_EVALUATED`; `report` generates all artifacts; `approve` requires explicit human identity and rationale.

- [ ] **Step 6: Run tests**

```bash
python -m unittest tools.performance.a3.tests.test_cli -v
```

- [ ] **Step 7: Run a dry-run integration test**

```bash
python -m tools.performance.a3.cli prepare --config tools/performance/a3/config/a3.example.json --dry-run
```

Expected: exit code 0, no service execution, manifest preview and planned artifact paths printed.

- [ ] **Step 8: Commit**

```bash
git add tools/performance/a3/cli.py tools/performance/a3/tests/test_cli.py tools/performance/a3/__init__.py
git commit -m "feat: add A3 baseline orchestration CLI"
```

---

### Task 12: Add CI, runbook, and full verification gate

**Files:**
- Create: `.github/workflows/a3-baseline-tests.yml`
- Create: `docs/observability/A3_BASELINE_RUNBOOK.md`
- Modify: `.github/workflows/observability-tests.yml`

**Interfaces:**
- Consumes: complete A3 package.
- Produces: pull-request CI and operator runbook.

- [ ] **Step 1: Add CI workflow**

Run on Python 3.11 and 3.12 for changes under `tools/performance/a3/**`, `docs/observability/A3_*`, and the workflow itself. Execute:

```bash
python -m unittest discover -s tools/performance/a3/tests -v
python -m tools.performance.a3.cli prepare --config tools/performance/a3/config/a3.example.json --dry-run
```

- [ ] **Step 2: Extend observability path filters**

Add A3 paths to `.github/workflows/observability-tests.yml` only where shared observability changes should trigger both suites. Do not duplicate the A3 test matrix inside the existing workflow.

- [ ] **Step 3: Write operator runbook**

Document prerequisites, host preparation, MariaDB fixed-config verification, NTP/chrony verification, dataset generation, controls, run sequence, invalid-run replacement, catastrophic stop procedure, external artifact storage, report generation, approval, retention, and rollback.

- [ ] **Step 4: Run all A3 tests locally**

```bash
python -m unittest discover -s tools/performance/a3/tests -v
```

Expected: all tests pass under Python 3.11 or newer.

- [ ] **Step 5: Run existing observability tests**

```bash
python -m unittest discover -s tools/observability/tests -v
```

Expected: all existing Python observability tests pass.

- [ ] **Step 6: Validate JSON files**

```bash
python -m json.tool tools/performance/a3/config/a3.example.json >/dev/null
python -m json.tool tools/performance/a3/config/slo-thresholds.json >/dev/null
python -m json.tool tools/performance/a3/config/workload-profile.json >/dev/null
python -m json.tool tools/performance/a3/config/grafana-dashboard.json >/dev/null
```

Expected: all commands exit 0.

- [ ] **Step 7: Review scope against design**

Confirm the implementation includes all approved metrics, validity gates, three-run rules, scaling rules, regression budgets, artifact formats, retention metadata, and manual approval governance. Confirm it does not add sharding, DB deep tuning, failover, distributed DB, or login-burst implementation.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/a3-baseline-tests.yml .github/workflows/observability-tests.yml docs/observability/A3_BASELINE_RUNBOOK.md
git commit -m "ci: verify A3 baseline tooling"
```

---

## Implementation Sequence and Review Gates

Implement tasks strictly in order. After each task:

1. Run the task-specific test command.
2. Inspect the diff for scope creep and accidental runtime changes.
3. Commit using the specified message.
4. Record the commit SHA in the subagent-development ledger.
5. Request a specification review and a code-quality review before starting the next task.

Do not begin real 500-5,000-user execution as part of this implementation PR. The implementation PR delivers the reproducible harness, schemas, configuration, tests, CI, and runbook. Actual baseline execution is a separate operational cycle on the approved reference hardware and produces external artifacts indexed by Git checksums.

## Final Verification Checklist

- [ ] All A3 unit tests pass on Python 3.11 and 3.12.
- [ ] Existing observability tests remain green.
- [ ] Example configuration passes strict validation.
- [ ] Dry-run CLI produces a complete cycle plan without executing services.
- [ ] Manifest comparison rejects every frozen-environment change.
- [ ] Dataset generation is deterministic and contains no production data.
- [ ] Invalid runs cannot contribute to level aggregation.
- [ ] One failed valid run cannot be hidden by the median.
- [ ] Catastrophic failures block the cycle.
- [ ] Non-catastrophic failures permit testing through 5,000 users.
- [ ] Dashboard thresholds are generated from the same source as SLO evaluation.
- [ ] Approval cannot occur automatically or without human identity and rationale.
- [ ] Documentation clearly separates implementation from actual benchmark execution.
