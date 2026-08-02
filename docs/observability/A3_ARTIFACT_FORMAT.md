# A3 Artifact Format

This document defines the on-disk contract for A3 baseline artifacts produced
by `tools/performance/a3/reporting.py`. It is the single reference for
directory structure, file ownership, integrity, retention, and data-safety
rules.

## Directory structure

All artifacts for one baseline cycle live under the artifact root:

```text
artifacts/performance/a3/<baseline_cycle_id>/
├── manifest.json
├── technical-report.md
├── executive-summary.md
├── comparison.csv
├── capacity.json
├── scaling.json
├── regression.json
├── anomalies.json
├── artifact-index.json
├── retention.json
├── grafana-dashboard.json
├── checksums.json
└── runs/
    └── <run_id>/
        ├── run.json
        ├── summary.json
        ├── timeseries.csv
        ├── workload.csv
        ├── slo-verdict.json
        ├── anomalies.json
        ├── prometheus-queries.json
        ├── event-log.json
        ├── collectors/
        │   ├── collectors.json
        │   ├── pidstat.log / pidstat.stderr.log
        │   ├── sar.log / sar.stderr.log
        │   ├── vmstat.log / vmstat.stderr.log
        │   └── iostat.log / iostat.stderr.log
        ├── service-logs/
        │   ├── login-server.log
        │   ├── char-server.log
        │   └── map-server.log
        └── checksums.json
```

## File ownership

- **Run artifacts** describe exactly one run at one load level and are written
  by `write_run_artifacts()`. They are never overwritten: an existing run
  directory is refused by default.
- **Cycle artifacts** describe the whole baseline cycle and are written by
  `write_cycle_reports()` after all runs complete.
- `collectors/*.log`, `service-logs/*.log`, and `event-log.json` are copied
  byte-for-byte from the run environment; the reporter never edits, scans, or
  redacts them, preserving raw-log integrity.

## Checksum process

- Every run directory and every cycle directory ends with a `checksums.json`
  written **last**. An incomplete run never receives a final `checksums.json`.
- Each entry records the relative path, SHA-256 (Task 1 `sha256_file()`), and
  exact size in bytes of one regular file. Entries are sorted by relative
  path, contain no absolute paths, no duplicates, no symlinks, and exclude
  `checksums.json` itself.
- Cycle checksums cover cycle-level files only; run directories carry their
  own checksums.

## Canonical JSON

- UTF-8 without BOM, deterministic sorted keys, `indent=2` for readable
  output. Canonical checksum serialization elsewhere in the toolchain uses
  `separators=(",", ":")`; checksums always cover the actual emitted bytes.
- No NaN or Infinity values, no Python `repr`, enums serialize with their
  approved string values, and all writes are atomic (sibling temporary file,
  flush, fsync, `os.replace`).

## CSV contracts

- `timeseries.csv` and `workload.csv` use exact stable header orders defined
  by the reporter, `\n` line endings, rows sorted by timestamp (and category
  for workload), finite numeric values only, and no formula-injection cells.
- `comparison.csv` contains exactly one row per approved load level
  (500/1000/2500/5000); a missing level is represented explicitly with
  `valid_run_count=0`, `verdict=BLOCKED`, and empty numeric cells.

## Prometheus query provenance

- `prometheus-queries.json` records the exact expressions, time range, and
  5-second step used for the run so every plotted value can be re-queried.
  URLs must not contain credentials, and no secret material is permitted in
  any structured artifact.

## Grafana threshold provenance

- `tools/performance/a3/config/grafana-dashboard.json` is the committed
  template. Every threshold line carries an `a3_threshold_ref` that resolves
  against `slo-thresholds.json`; values are validated by
  `validate_dashboard_thresholds()` and are never hand-copied without a
  threshold reference. Warning lines equal exactly 90% of the positive hard
  threshold; zero-tolerance metrics never carry warning lines.
- Runtime rendering replaces only `${A3_BASELINE_CYCLE_ID}`,
  `${A3_MANIFEST_ID}`, and `${A3_RUN_ID}`; the committed template always
  retains its placeholders.

## External raw artifact storage and retention

- Git stores schemas, templates, indexes, and checksums — **not** large raw
  artifacts. Raw Prometheus blocks, Linux collector logs, and service logs
  are stored on external artifact storage and are external to Git.
- Minimum raw retention: **180 days** for raw Prometheus data, Linux logs,
  and service logs (`raw_prometheus_minimum_days`,
  `linux_logs_minimum_days`, `service_logs_minimum_days`).
- permanent retention: `summary.json`, `manifest.json`, all CSV files,
  and the rendered Grafana dashboard (these never expire).
- Retention policy is encoded in `retention.json` with
  `external_storage_required: true`. No destructive cleanup is implemented by
  the reporting layer.

## Invalid and catastrophic runs

- Artifacts of invalid runs are preserved exactly like valid ones; the
  validity verdict is part of `run.json` and `summary.json`.
- Catastrophic runs preserve the full artifact-capture set (event log,
  collector output, service logs, checksums) so root-cause analysis can be
  performed after the fact.

## Data safety

- No production player data appears in any artifact. Datasets are synthetic
  only. Structured artifacts are rejected if they contain secret markers
  (password, token, secret, api_key, private_key, authorization, bearer);
  raw copied service logs are excluded from scanning to preserve byte
  integrity, and reports or indexes never embed raw log content.
