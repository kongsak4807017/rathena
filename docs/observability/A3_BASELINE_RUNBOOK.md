# A3 Baseline Operator Runbook

## Purpose and Scope

This runbook describes how to execute the approved A3 baseline capacity cycle
for rAthena on the approved reference hardware, from host preparation through
manual approval. It covers only the approved scope: one reference server, one
MariaDB instance, synthetic load at 500/1000/2500/5000 users, and manual
approval governance. Out of scope: sharding, multi-node deployment, deep
MariaDB tuning, failover, distributed database, maximum login-burst testing,
and any use of production player data.

## Implementation Versus Operational Execution

The A3 toolchain in `tools/performance/a3/` is implemented and verified by CI.
**Toolchain readiness is not capacity evidence.** No 500-user, 1000-user,
2500-user, or 5000-user benchmark has been executed by this implementation.
Capacity remains **NOT ESTABLISHED** until the complete approved cycle is
executed on the reference hardware by following this runbook.

Adapters that still require environment-specific implementation before the
cycle can run:

- the real workload harness (`run_harness` CLI adapter)
- the real WebGL control adapter (`run_control` CLI adapter)
- environment-specific service start/stop commands wired into the lifecycle
  controller (`RunCommand` instances for login/char/map servers)

## Roles and Approval Authority

- **Operator**: prepares hosts, executes controls and runs, preserves
  artifacts. The operator may not approve.
- **Approver**: a named human who reviews the technical report and approves
  or rejects the baseline. Approval is manual only; CI never approves a
  baseline. Every approval requires an explicit approver name, an explicit
  rationale, and an explicit UTC timestamp. A PASS_WITH_WARNING approval
  requires a rationale that explicitly acknowledges warnings. Approved
  records are append-only; supersession preserves the old approval.

## Reference Hardware

One bare-metal server:

- Ubuntu Server 24.04.4 LTS
- 8 physical cores / 16 threads
- 32 GB RAM
- NVMe storage
- 1 Gbps network link

## Network and Host Topology

- **Reference server** (above): runs one login-server, one char-server, one
  map-server, and MariaDB 10.11 LTS on the same server.
- **Load generator**: a separate machine; never on the reference server.
- **WebGL validation machine**: a separate machine; never on the reference
  server.

## Required Software

- Python 3.11 or 3.12 (toolchain only, standard library only)
- Prometheus, node_exporter, MariaDB exporter, Grafana
- pidstat, sar, vmstat, iostat (sysstat)
- MariaDB 10.11 LTS
- chrony

## Repository and Commit Preparation

1. Clone the repository and check out the approved commit for the cycle.
2. Confirm the working tree is clean (`git status --porcelain` is empty).
3. Record the commit SHA; the reproducibility manifest pins it.

## MariaDB 10.11 Fixed Configuration

- Install MariaDB 10.11 LTS on the reference server.
- Freeze `/etc/mysql/my.cnf`; the manifest records its SHA-256.
- Do not tune between runs of the same cycle. Any configuration change
  requires a new manifest and a full cycle restart.

## NTP and Chrony Verification

- Ensure chrony is active and synchronized (`chronyc sources`).
- The manifest freezes the time-sync source; unhealthy synchronization fails
  preflight and run validity.

## CPU Governor and Power Settings

- Set the CPU governor to `performance`.
- Set the BIOS power profile to `performance`.
- The manifest freezes both; a governor or power-profile change is frozen
  drift and blocks execution.

## rAthena Build Verification

- Build login-server, char-server, and map-server in Release mode with the
  approved compiler flags.
- The manifest records SHA-256 of all three binaries plus compiler and
  build flags.

## PACKETVER and Protocol Verification

- Confirm `PACKETVER` in `src/config/packets.hpp` (or
  `src/custom/defines_pre.hpp`) matches the approved value for the cycle.
- The manifest freezes PACKETVER and the packet database revision.

## Synthetic Dataset Preparation

- The dataset is deterministic: seed `20260802`, 6000 accounts, 12000
  characters, 200 guilds, 500 parties, with deterministic inventory, storage,
  and quest profile tiers.
- The dataset is synthetic only. **No production player data** may be used
  or present on the reference server.
- `prepare` emits the dataset SQL and metadata sidecar into the cycle
  artifact directory.

## Artifact Storage Preparation

- Provision external artifact storage. Raw Prometheus blocks, Linux
  collector logs, and service logs are external to Git.
- Git holds schemas, templates, indexes, and checksums only.

## Prometheus and Grafana Preparation

- Start Prometheus with the rendered cycle configuration (5-second scrapes
  of rAthena, node_exporter, MariaDB exporter).
- Import the rendered Grafana dashboard; every threshold line references
  `slo-thresholds.json` by `a3_threshold_ref`.

## Preflight Checklist

- [ ] Reference hardware matches the frozen topology exactly
- [ ] Working tree clean; correct commit checked out
- [ ] MariaDB 10.11 running with frozen configuration
- [ ] chrony synchronized
- [ ] CPU governor and BIOS power profile at `performance`
- [ ] All three server binaries built and present
- [ ] PACKETVER correct
- [ ] Dataset generated and staged
- [ ] Prometheus/Grafana/exporters running with 5-second sampling
- [ ] External artifact storage reachable

## Prepare Command

```bash
python -m tools.performance.a3.cli prepare \
  --config tools/performance/a3/config/a3.example.json
```

Creates the cycle directory, frozen manifest, dataset SQL, rendered
Prometheus/Grafana configuration, and the DRAFT cycle state. Add
`--dry-run` to validate without writing anything.

## Idle Control

```bash
python -m tools.performance.a3.cli control idle \
  --cycle <baseline_cycle_id>
```

Runs services idle for 10 minutes. Must complete before the WebGL control
and any benchmark run.

## WebGL Control

```bash
python -m tools.performance.a3.cli control webgl \
  --cycle <baseline_cycle_id>
```

Exactly 20 real WebGL clients for 10 minutes from the separate WebGL
machine. These clients are not counted in synthetic load.

## Load-Level Run Sequence

```bash
python -m tools.performance.a3.cli run \
  --cycle <baseline_cycle_id> \
  --users 500 \
  --run 1
```

Each run executes: environment check, service start, preconditioning
(10 minutes), ramp (5 minutes), steady state (20 minutes), cooldown
(5 minutes), validation, reporting. Sampling is every 5 seconds.

Progression is enforced: complete three valid 500-user runs before 1000,
three valid 1000-user runs before 2500, and three valid 2500-user runs
before 5000.

## Three-Valid-Run Rule

Exactly three valid runs per tested load level are required. Invalid runs
are preserved but never count toward the three.

## Invalid-Run Replacement

When a run is invalid, keep its artifacts untouched and start a replacement
with a new run identity. Never overwrite invalid-run evidence.

## Non-Catastrophic Failure Procedure

A FAIL result does not automatically stop later levels. Preserve the run
artifacts, record the failure, and continue according to the approved
design.

## Catastrophic Stop Procedure

On a catastrophic signal (crash, corruption, deadlock, OOM, restart,
manifest drift, pipeline failure):

1. Stop the cycle immediately; do not continue to higher levels.
2. Preserve all available evidence (collectors, service logs, event log,
   artifact capture).
3. Perform root-cause analysis.
4. Apply the fix.
5. Create a new manifest.
6. Restart the complete cycle from the beginning. Never mix pre-fix and
   post-fix runs in one cycle.

Warm-cache procedure after a restart: restart services, perform the approved
preconditioning, and do not clear the filesystem cache.

## Evaluation

```bash
python -m tools.performance.a3.cli evaluate \
  --cycle <baseline_cycle_id>
```

Aggregates each level's three valid runs, evaluates scaling guardrails,
regression budgets, and capacity, and transitions the cycle to
CI_EVALUATED.

## Report Generation

```bash
python -m tools.performance.a3.cli report \
  --cycle <baseline_cycle_id>
```

Writes the technical report, executive summary, comparison CSV, capacity,
scaling, regression, anomaly, index, retention, and checksum artifacts, and
transitions to AWAITING_APPROVAL.

## Manual Approval

```bash
python -m tools.performance.a3.cli approve \
  --cycle <baseline_cycle_id> \
  --approver "<human name>" \
  --rationale "<explicit rationale>" \
  --approved-utc YYYY-MM-DDTHH:MM:SSZ
```

Verify artifact checksums before approval. Approval requires an approvable
capacity verdict and established safe capacity.

## Rejection and Supersession

```bash
python -m tools.performance.a3.cli reject \
  --cycle <baseline_cycle_id> \
  --approver "<human name>" \
  --rationale "<explicit rationale>" \
  --rejected-utc YYYY-MM-DDTHH:MM:SSZ
```

A rejected cycle never becomes an approved baseline. When a newer baseline
is approved, supersede the old one explicitly; the old approval record is
preserved unchanged.

## Artifact Integrity Verification

Verify `checksums.json` in every run directory and in the cycle directory
before upload and before approval. Recompute SHA-256 of each listed file
and compare path, hash, and size.

## External Artifact Upload

Upload raw Prometheus blocks, Linux collector logs, and service logs to the
external artifact store after checksum verification. Git stores only
schemas, templates, indexes, and checksums.

## Retention Requirements

- Summaries, manifests, CSV files, and the Grafana dashboard: permanent.
- Raw Prometheus data: minimum 180 days.
- Linux collector logs: minimum 180 days.
- Service logs: minimum 180 days.
- External storage is required. Never store secrets in any artifact.

## Rollback and Cleanup

- Remove only DRAFT cycles that never produced a finalized report.
- Never delete invalid-run evidence, approved records, or supersession
  records.
- Roll back to a previous approved baseline by pointing operators at its
  preserved artifacts and superseding when a replacement is approved.

## Troubleshooting

- **Manifest ineligible**: a capture group failed (missing binary, host
  file, or tool). Fix the host, recapture, and do not execute with an
  ineligible manifest.
- **Manifest drift**: a frozen field changed (binary, config, kernel,
  PACKETVER, governor, dataset, exporter). Restore the frozen value or
  create a new manifest and restart the cycle.
- **Missing binaries**: rebuild login/char/map-server in Release mode and
  recapture the manifest.
- **Wrong PACKETVER**: correct `src/config/packets.hpp` or
  `src/custom/defines_pre.hpp` and recapture.
- **Chrony/NTP unhealthy**: restart chrony, confirm `chronyc sources`
  shows a selected source, and rerun preflight.
- **Collector start failure**: install sysstat tools, verify
  pidstat/sar/vmstat/iostat run manually, and retry.
- **Collector stop failure**: investigate the stuck process, stop it
  manually, preserve its partial log, and treat the run as invalid.
- **Missing source logs**: do not fabricate files; the run cannot be
  finalized. Preserve what exists and mark the run invalid.
- **Invalid run**: preserve artifacts and start a replacement with a new
  identity.
- **Fewer than three valid runs**: complete more valid runs at that level;
  evaluation refuses incomplete levels.
- **Report checksum failure**: do not approve. Regenerate the report from
  preserved run artifacts and re-verify.
- **Approval refusal**: check capacity verdict, safe capacity, CI status,
  rationale wording (warnings must be acknowledged for PASS_WITH_WARNING),
  and identifier/UTC rules.
- **Catastrophic cycle**: follow the Catastrophic Stop Procedure; the cycle
  cannot continue or be evaluated normally.
- **GitHub CI dry-run returns exit 3 on a generic runner**: expected. The
  generic runner is not the reference host, so manifest capture is
  ineligible. CI verifies the toolchain only; no files are written and no
  operational execution is claimed.

## Final Sign-Off Checklist

- [ ] Preflight checklist complete
- [ ] Prepare completed (DRAFT state)
- [ ] Idle control completed (10 minutes)
- [ ] WebGL control completed (20 clients, 10 minutes)
- [ ] Three valid runs at 500, 1000, 2500, and 5000 users
- [ ] No unresolved catastrophic event
- [ ] Evaluation completed (CI_EVALUATED)
- [ ] Report generated and checksums verified
- [ ] External artifacts uploaded with retention policy applied
- [ ] Manual approval or rejection recorded with approver, rationale, and
      explicit UTC
