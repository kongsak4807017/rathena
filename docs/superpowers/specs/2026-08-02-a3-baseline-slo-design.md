# A3 Baseline and SLO Design

**Status:** Approved design, awaiting written-spec review  
**Date:** 2026-08-02  
**Repository:** `kongsak4807017/rathena`  
**Roadmap position:** A3, after A1/A2/A2.2 observability and before A4 load testing

## 1. Purpose

A3 establishes a reproducible single-node performance baseline for rAthena before large-scale load testing and optimization. It must determine:

1. The highest safe concurrent-user level.
2. The first load level where scaling degrades.
3. Whether the principal bottleneck is CPU, memory, tick latency, packet handling, SQL, scripts, storage, or network.
4. Whether future code or configuration changes create performance regressions.

A3 measures a controlled reference system. It does not perform sharding, multi-node deployment, deep MariaDB tuning, failover testing, distributed database work, or maximum login-burst testing. Those belong to A4-A7.

## 2. Reference Architecture

### 2.1 Topology

Three physically separate roles are required:

- **Reference server:** rAthena and MariaDB.
- **Synthetic load generator:** protocol-native clients and test controller.
- **WebGL validation machine:** 20 real WebGL clients.

The load generator and WebGL validation machine must not share CPU, RAM, or storage with the reference server.

### 2.2 Reference server

- Bare metal.
- Ubuntu Server 24.04.4 LTS.
- 8 physical cores / 16 threads.
- 32 GB RAM.
- NVMe SSD.
- 1 Gbps network.
- MariaDB 10.11 LTS with fixed `my.cnf`.
- One `login-server`.
- One `char-server`.
- One `map-server`.
- One MariaDB instance.

### 2.3 System path

Every synthetic client must traverse the production path:

`Login Server -> Character Server -> Character Selection -> Map Server -> Mixed Gameplay Workload`

The baseline must not create sessions directly on the map server.

## 3. Dataset

Use a deterministic, production-like synthetic dataset. No real player data is permitted.

Initial target size:

- 6,000 accounts.
- 12,000 characters.
- 200 guilds.
- 500 parties.
- Inventory, storage, quest, social, refine, equipment, and item-stack distributions generated from a fixed profile.

The generator must be deterministic for a given seed. The manifest must contain the seed, row counts, canonical export checksum, and consistency checks for account-character, guild, party, and identifier relationships.

Production-candidate NPC, item, monster, skill, map, and script content is used and checksummed.

## 4. Workload Model

### 4.1 Load levels

Test these target concurrency levels:

- 500 users.
- 1,000 users.
- 2,500 users.
- 5,000 users.

Each level requires three valid runs.

### 4.2 Hybrid clients

- Protocol-native clients generate the full target load.
- Twenty real WebGL clients validate end-to-end behavior during every run.
- The 20 WebGL clients are additional and are not included in the target synthetic concurrency.

### 4.3 Mixed gameplay profile

Steady-state activity mix:

- 35% movement and direction changes.
- 20% idle / heartbeat.
- 15% combat.
- 10% NPC interaction.
- 8% item use / inventory.
- 5% map change / warp.
- 4% chat.
- 3% login, logout, and character selection.

The aggregate workload mix must remain within +/-5 percentage points of each target proportion.

## 5. Test Lifecycle

### 5.1 Control runs

Before the 500-user runs:

1. **Idle control:** 10 minutes, all services running, no clients.
2. **WebGL-only control:** 10 minutes, 20 WebGL clients, no synthetic clients.

Control results are references only. They are not automatically subtracted from production measurements.

### 5.2 Per-run lifecycle

Each run uses warm-cache operation:

1. Restart MariaDB and all rAthena services.
2. Verify environment manifest and checksums.
3. Run 10 minutes of preconditioning.
4. Ramp linearly to target concurrency over 5 minutes.
5. Hold steady state for 20 minutes.
6. Cool down and log out over 5 minutes.
7. Validate the run and generate artifacts.

A full valid run therefore occupies 40 minutes including preconditioning.

### 5.3 State machine

`ENVIRONMENT_CHECK -> SERVICE_START -> PRECONDITIONING -> RAMP_UP -> STEADY_STATE -> COOL_DOWN -> VALIDATION -> REPORTING`

A catastrophic failure transitions to:

`ABORTED -> ARTIFACT_CAPTURE -> ROOT_CAUSE_ANALYSIS`

### 5.4 Cache policy

Do not drop the Linux filesystem cache between runs. The baseline represents a warmed operational system rather than first-start behavior.

## 6. Run Validity

A run is invalid and excluded from baseline calculations when any of the following occurs:

- Actual concurrency is below 98% of target.
- Unexpected disconnects exceed 1%.
- Prometheus data is missing continuously for more than 15 seconds.
- Any rAthena service or MariaDB restarts or crashes.
- Unrelated background workload consumes more than 5% average reference-server CPU.
- Packet loss between test machines exceeds 0.1%.
- Workload mix deviates by more than +/-5 percentage points.
- Environment fingerprint or checksum changes.

Invalid-run artifacts are retained, but the run must be replaced until three valid runs exist at the load level.

## 7. Failure Continuation

### 7.1 Non-catastrophic failure

When a run exceeds a CPU, latency, packet, SQL, script, disk, network, or other non-catastrophic SLO, testing continues to higher levels through 5,000 users to determine the tested ceiling.

### 7.2 Catastrophic failure

Crash, data corruption, deadlock, OOM, database inconsistency, or an invalid environment blocks the cycle. The procedure is:

1. Stop the cycle.
2. Capture dumps, logs, metrics, and manifest.
3. Diagnose and fix the cause.
4. Create a new manifest ID.
5. Restart the full baseline cycle.

Results from before and after a fix must never be combined.

## 8. Metrics Collection

### 8.1 Primary telemetry

- Prometheus.
- `node_exporter`.
- rAthena textfile observability output.
- MariaDB exporter or equivalent SQL telemetry.

Prometheus is the authoritative dataset.

### 8.2 Independent cross-check

Collect every 5 seconds with:

- `pidstat`.
- `sar`.
- `vmstat`.
- `iostat`.

Material disagreement between Prometheus and Linux tools must be treated as an anomaly and resolved before approval.

### 8.3 Sampling

- Prometheus scrape interval: 5 seconds.
- rAthena snapshot interval: 5,000 ms.
- Linux-tool interval: 5 seconds.

Steady state yields about 240 samples per metric per run. Percentiles used for SLO decisions are calculated from steady-state samples only.

## 9. SLOs and Guardrails

### 9.1 CPU

- Steady-state median <= 75%.
- p95 <= 85%.
- CPU above 95% must not continue for more than 30 seconds.

Report host CPU and per-process CPU for login, char, map, and MariaDB, including system, IRQ, and steal time.

### 9.2 Memory

- Total resident memory <= 80% of RAM.
- Memory growth during steady state <= 5%.
- Swap-in = 0.
- Swap-out = 0.
- OOM = 0.
- Allocation failure = 0.

Report per-process RSS, page faults, cache/buffer use, MariaDB buffer use, and memory per concurrent user.

### 9.3 Tick latency / timer drift

- p50 <= 10 ms.
- p95 <= 25 ms.
- p99 <= 50 ms.
- max <= 100 ms.
- Drift above 50 ms must not continue for more than 10 seconds.

Percentiles use sampled `timer drift last`. Cumulative maximum drift is reported separately and must not be used as a percentile substitute.

### 9.4 Packet processing

- p95 <= 5 ms.
- p99 <= 10 ms.
- max <= 25 ms.
- Packet backlog must not grow continuously for more than 10 seconds.
- Dropped or rejected packets <= 0.01%.
- Malformed packets must not damage a session or process.
- Unknown packets must not increase from the known baseline.

Report rates and latency by server component, direction, category/family, accepted, rejected, malformed, unknown, and backlog proxy.

### 9.5 SQL

- p95 <= 25 ms.
- p99 <= 75 ms.
- max <= 500 ms.
- Slow-query ratio <= 0.5%.
- SQL execution failure <= 0.01%.
- Connection acquisition failure = 0.
- Deadlock = 0.
- Lock-wait timeout = 0.
- Connection usage p95 <= 75% of configured maximum.

The slow-query threshold is frozen in the manifest.

### 9.6 Script execution

- p95 <= 5 ms.
- p99 <= 20 ms.
- max <= 100 ms.
- Slow-script ratio <= 0.5%.
- Script execution failure <= 0.01%.
- No category latency may exceed 2x its 500-user baseline.
- Unknown-category ratio <= 5% and must not grow with load.

Report NPC, Event, Timer, Item, Instance, and Unknown. Skill and Quest remain Unknown until authoritative central categorization exists.

### 9.7 Errors and disconnects

- Process crash = 0.
- Data corruption = 0.
- Deadlock = 0.
- Login failure <= 0.1%.
- Character-selection failure <= 0.1%.
- Unexpected steady-state disconnect <= 0.5%.
- SQL execution failure <= 0.01%.
- Script execution failure <= 0.01%.

Each error rate must use its correct denominator.

### 9.8 Storage

- NVMe utilization p95 <= 75%.
- Await p95 <= 5 ms.
- Await p99 <= 10 ms.
- Queue depth must not grow continuously for more than 10 seconds.

Separate MariaDB, rAthena logs, Prometheus, and operating-system I/O where possible.

### 9.9 Network

- Utilization p95 <= 70% of 1 Gbps.
- Packet loss <= 0.1%.
- TCP retransmission <= 0.1%.
- Socket errors = 0.
- Listen drops = 0.

## 10. Scaling Guardrails

Between 500, 1,000, 2,500, and 5,000 users:

- Latency p95 may increase by no more than 50% from the previous level.
- Latency p99 may increase by no more than 75% from the previous level.
- Memory per user may increase by no more than 20% from the 500-user baseline.
- Error rate may increase by no more than 2x from the previous level and must remain inside the absolute error budget.
- Throughput growth below 80% of the proportional user increase is a scaling anomaly.
- CPU, packet, SQL, and script rates must not show an unexplained breakpoint or acceleration.

## 11. Warning Zone

A metric enters `PASS-WITH-WARNING` territory when it exceeds 90% of its hard threshold while remaining within the hard threshold.

Metrics that must equal zero have no warning zone. Any crash, corruption, deadlock, OOM, service restart, or connection acquisition failure immediately becomes FAIL or BLOCKED according to severity.

## 12. Multi-run Aggregation

For each load level:

- Three valid runs are mandatory.
- The median of the three runs is the headline value.
- The worst valid run is the stability guard.
- Per-run p95 and p99 remain binding.
- Min, max, and coefficient of variation are reported.

Verdicts:

- **PASS:** all three valid runs pass hard SLOs and scaling guardrails.
- **PASS-WITH-WARNING:** all three pass hard SLOs, but at least one run enters a warning zone or the median shows a scaling anomaly.
- **FAIL:** at least one valid run exceeds a non-catastrophic hard SLO.
- **BLOCKED:** any run has a catastrophic failure, or three valid runs cannot be obtained.

A median must never hide a failed run.

## 13. Capacity Verdict

The final report declares:

- **Safe Capacity:** highest load level with PASS.
- **Conditional Capacity:** highest load level with PASS-WITH-WARNING.
- **Tested Ceiling:** highest load level that completed even if FAIL.

If no level passes, Safe Capacity is `Not Established`.

## 14. Reproducibility Manifest

Each baseline cycle receives a unique `manifest_id`. The manifest is JSON and includes its own SHA-256 checksum.

Required identity and freeze data:

- Repository URL, branch/tag, Git commit SHA, clean working-tree status.
- Dependency and submodule revisions.
- Compiler, build-system version, Release flags, linked libraries.
- Login, char, and map binary checksums.
- PACKETVER and packet-database revision.
- rAthena configuration checksums.
- Observability configuration, slow SQL threshold, slow script threshold, snapshot interval.
- Script, NPC, item, monster, skill, and map checksums.
- Exact MariaDB version, `my.cnf` checksum, schema revision/checksum.
- Dataset seed, checksum, and row counts.
- Ubuntu, kernel, packages, filesystem, mount options, timezone, and time-sync source.
- CPU, RAM, NVMe, NIC, BIOS, firmware, governor, power profile, and NUMA topology.
- Prometheus, exporters, scrape configuration, Grafana, and dashboard checksums.
- Harness repository, commit, binary checksum, workload checksum, account range, random seed, and WebGL revision.

No binary, configuration, protocol, script, database, kernel, exporter, workload, harness, dataset, or power-setting change is permitted inside a group of three valid runs. A change requires a new manifest and restarts the run group.

## 15. Metric Integrity

Before a verdict is accepted:

- Counters must not reset during steady state.
- Timestamps must not jump.
- Scrape intervals must remain valid.
- Duplicate series must not exist.
- Label cardinality must remain controlled.
- Textfile writes must be atomic and complete.
- Metric write errors must not increase.
- Reference server, load generator, and WebGL machine must synchronize time with NTP or chrony.

A timing error that prevents trustworthy workload-to-metric correlation invalidates the run.

## 16. Artifacts

Recommended structure:

```text
artifacts/performance/a3/<baseline_cycle_id>/
├── manifest/
├── controls/idle/
├── controls/webgl-only/
├── runs/users-0500/run-01..03/
├── runs/users-1000/run-01..03/
├── runs/users-2500/run-01..03/
├── runs/users-5000/run-01..03/
├── prometheus/
├── linux-tools/
├── logs/
├── crash-artifacts/
├── reports/
├── grafana/
└── approval/
```

Each run includes:

- `run.json`.
- `summary.json`.
- `timeseries.csv`.
- `workload.csv`.
- `slo-verdict.json`.
- `anomalies.json`.
- `prometheus-queries.json`.
- Linux-tool logs.
- Service logs.

Every artifact must include or reference `baseline_cycle_id`, `manifest_id`, `run_id`, target and actual concurrency, timestamps, and workload seed.

## 17. Reporting

### 17.1 Machine-readable

- JSON for raw and summarized run results, SLO verdicts, anomalies, scaling ratios, and capacity verdict.
- CSV for time series and cross-level comparisons.
- Prometheus snapshots and the exact query expressions used for decisions.
- Manifest and checksums.

### 17.2 Human-readable

Technical Markdown report:

1. Manifest summary.
2. Test topology.
3. Dataset summary.
4. Control results.
5. Results by load level.
6. SLO table.
7. Scaling analysis.
8. Anomalies.
9. Bottleneck attribution.
10. Regression comparison.
11. Capacity verdict.
12. Recommendations for A4 and A5.

Executive summary must state Safe Capacity, Conditional Capacity, Tested Ceiling, first degradation point, primary bottleneck, readiness for A4, and required remediation.

### 17.3 Grafana

Version-controlled dashboard JSON must cover overview, CPU, memory, tick, packet, SQL, script, storage, network, scaling, warning thresholds, hard thresholds, and run annotations.

## 18. Regression Policy

Compare a candidate cycle only with the latest Approved Baseline that uses the same hardware profile, topology, workload profile, and dataset profile.

Regression budget:

- CPU median / p95: no more than 10% worse.
- Memory per user: no more than 10% worse.
- Tick, packet, SQL, and script p95: no more than 15% worse.
- Tick, packet, SQL, and script p99: no more than 20% worse.
- Throughput: no more than 10% lower.
- Error rate: no more than 25% higher and still inside absolute error budgets.

A candidate that passes absolute SLOs but exceeds the regression budget is at least PASS-WITH-WARNING. A broad or severe regression is FAIL.

## 19. Governance

CI performs:

- Manifest completeness and checksum validation.
- Valid-run counting.
- Percentile calculation.
- SLO and warning evaluation.
- Scaling analysis.
- Regression comparison.
- Report generation.
- Provisional verdict generation.

CI must not automatically establish an Approved Baseline.

Manual approval must record:

- Approver identity.
- Approval timestamp.
- Manifest ID.
- Git commit SHA.
- Verdict at every load level.
- Safe Capacity.
- Conditional Capacity.
- Tested Ceiling.
- Known warnings.
- Approval rationale.

Lifecycle states:

`DRAFT -> CI_EVALUATED -> AWAITING_APPROVAL -> APPROVED | REJECTED -> SUPERSEDED`

## 20. Retention

Retain permanently:

- Manifests and checksums.
- Summary and verdict JSON.
- Anomaly JSON.
- Comparison CSV.
- Markdown reports.
- Grafana exports.
- Approval records.

Retain raw Prometheus and time-series data for at least 180 days. Large raw artifacts may live outside Git, but Git must retain an index, checksum, and storage location.

Crash artifacts remain until the root-cause issue is closed and a subsequent Approved Baseline verifies the fix.

## 21. Exit Criteria

A3 is complete only when:

1. A complete reproducibility manifest passes validation.
2. Idle and WebGL-only controls exist.
3. Three valid runs exist at 500, 1,000, 2,500, and 5,000 users.
4. CPU, memory, tick, packet, SQL, script, storage, and network metrics are complete.
5. JSON, CSV, Markdown, Prometheus-query, manifest, and Grafana artifacts exist.
6. Metric-level and load-level verdicts exist.
7. Scaling analysis exists.
8. Regression analysis exists, or the report explicitly identifies the cycle as the first baseline.
9. Safe Capacity, Conditional Capacity, and Tested Ceiling are declared.
10. Manual approval is recorded.

A 5,000-user BLOCKED result does not complete A3. The root cause must be fixed and the cycle restarted unless an explicit design revision changes the benchmark scope.
