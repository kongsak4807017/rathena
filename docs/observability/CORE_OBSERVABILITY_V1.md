# Core Observability V1 (map-server instrumentation)

In-process performance instrumentation for `map-server`, introduced as work
package A2 on top of the A1 out-of-process observability sidecar.

The instrumentation is **disabled by default** and designed to be a no-op
unless explicitly enabled:

- no extra timer, no map scans, no file output when disabled
- no gameplay, packet protocol or database schema changes
- no external dependencies, no network access, no HTTP endpoint
- metrics never contain player names, account IDs, character IDs or IPs

## Configuration

Configuration is via environment variables only:

| Variable | Default | Description |
| --- | --- | --- |
| `RATHENA_CORE_OBSERVABILITY` | `0` (off) | `1`, `true`, `on`, `yes` enable; everything else keeps it off |
| `RATHENA_CORE_OBSERVABILITY_INTERVAL_MS` | `10000` | Snapshot interval in ms. Clamped to [1000, 3600000]. Malformed values fall back to the default with a single startup warning |
| `RATHENA_CORE_OBSERVABILITY_OUTPUT` | `log/metrics/rathena_map.prom` | Destination of the Prometheus textfile |

Example (enabled, one snapshot per second):

```powershell
$env:RATHENA_CORE_OBSERVABILITY = "1"
$env:RATHENA_CORE_OBSERVABILITY_INTERVAL_MS = "1000"
$env:RATHENA_CORE_OBSERVABILITY_OUTPUT = "log/metrics/rathena_map.prom"
.\map-server.exe
```

## Metrics

Core metrics:

```text
rathena_core_timer_drift_last_milliseconds      # gauge: drift of the most recent observability timer callback
rathena_core_timer_drift_max_milliseconds       # gauge: maximum observed callback drift
rathena_core_snapshots_total                    # counter: snapshots taken since start
rathena_core_snapshot_duration_last_milliseconds # gauge: duration of the most recent snapshot
rathena_core_snapshot_duration_max_milliseconds  # gauge: maximum observed snapshot duration
rathena_core_write_errors_total                 # counter: failed textfile writes
```

Per-map entity gauges (one series per map and type) and server-wide totals:

```text
rathena_map_entities{map="prontera",type="player"} 120
rathena_map_entities{map="prontera",type="mob"} 35
rathena_map_entities{map="prontera",type="npc"} 84
rathena_map_entities{map="prontera",type="item"} 12
rathena_map_entities{map="prontera",type="skill"} 7

rathena_core_entities_total{type="player"} 500
rathena_core_entities_total{type="mob"} 12000
rathena_core_entities_total{type="npc"} 3400
rathena_core_entities_total{type="item"} 250
rathena_core_entities_total{type="skill"} 130
```

Timer drift is measured on the dedicated observability timer itself
(scheduled tick vs. actual execution tick, negative drift clamped to zero)
and serves as a proxy for event-loop lag. `do_timer()` is not modified.

Entity counts reuse the existing map block lists (`map_data::block`,
`map_data::block_mob`) and the maintained `users` / `npc_num` counters.
No duplicate entity registry is kept and no global entity database is
walked. Maps that are not initialized, not local to this map-server or in
teardown are skipped.

## Output behavior

Metrics are written in Prometheus text exposition format using an atomic
replace:

1. the exposition is written to `<output>.tmp`
2. the temporary file is flushed and closed
3. it is atomically moved over the final path
   (`rename()` on Linux, `MoveFileExA` with `MOVEFILE_REPLACE_EXISTING` on Windows)

The node_exporter textfile collector (or any other collector reading the
file) therefore never sees a partially written file. Missing parent
directories are created on demand; failures increment
`rathena_core_write_errors_total` and log a warning without crashing the
map-server. The `.prom` file is runtime output and must not be committed.

## Lifecycle

- `core_observability_init()` is called at the end of
  `MapServer::initialize()`, after maps and all core systems are ready.
  When disabled it returns immediately without creating a timer.
  Repeated calls (e.g. after script reloads) do not create duplicate timers.
- `core_observability_final()` is called at the top of
  `MapServer::finalize()`, before any measured data structure is
  destroyed, and deletes the timer.

## Performance impact

- Disabled: near zero. One environment lookup during startup; no timer,
  no scanning, no I/O afterwards.
- Enabled: one block-list walk per map per interval plus one small file
  write. The time spent per snapshot is itself exported via
  `rathena_core_snapshot_duration_*` metrics, so a slow snapshot is
  visible in monitoring.

## Tests

Pure logic (environment parsing, interval clamping, label escaping, drift
calculation, rendering, atomic output) is covered by dependency-free unit
tests that do not require a running map-server:

```powershell
# Windows (MSVC)
cl /std:c++17 /EHsc /W4 /Isrc\map tools\observability\tests\core_observability_test.cpp /Fe:core_observability_test.exe
.\core_observability_test.exe
```

```sh
# Linux
g++ -std=c++17 -Wall -Wextra -Werror -Isrc/map tools/observability/tests/core_observability_test.cpp -o core_observability_test
./core_observability_test
```

The `Observability tests` workflow runs these on GCC, Clang and MSVC.

## Rollback

The instrumentation is additive and self-contained:

1. keep it disabled (default) — zero behavioral change, or
2. revert the commits of this branch; the only touch points in existing
   code are two calls in `src/map/map.cpp` and file list entries in the
   MSVC projects.

## Out of scope (follow-up work)

- packet / SQL / slow-script instrumentation (needs separately testable hooks)
- histograms (only last/max gauges for now)
- exposing metrics over the network (use the A1 sidecar / node_exporter
  textfile collector instead)
