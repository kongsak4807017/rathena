# SQL Observability V1 (hybrid SQL instrumentation)

In-process SQL performance instrumentation for all rAthena server processes,
introduced as work package A2.2-2 on top of the A2 core observability
instrumentation.

## Overview

SQL observability is a disabled-by-default hybrid layer that records coarse
query volume, latency, failures, prepared-statement pressure, and connection
health from the central common SQL layer. It does not change SQL execution
order, return codes, retry behavior, transaction semantics, or the database
schema.

The instrumentation is intentionally text-free:

- no SQL query text, prepared-statement text, or parameters are ever recorded
- no database/schema/table/column/index names are exported
- no MySQL/MariaDB error messages, hostnames, ports, usernames, passwords, or
  connection strings are exported
- no account IDs, character IDs, guild/party IDs, item IDs, IPs, session
  tokens, chat text, or transaction identifiers are exported
- no source file, function, line, or call-site labels are exported

Metrics are appended to the existing core observability `.prom` textfile when
both SQL observability and core observability are enabled. No new writer,
timer, or output file is introduced.

## Configuration

Configuration is via environment variables only:

| Variable | Default | Description |
| --- | --- | --- |
| `RATHENA_SQL_OBSERVABILITY` | `0` (off) | `1`, `true`, `on`, `yes` enable; everything else keeps it off |
| `RATHENA_SQL_OBSERVABILITY_SLOW_MS` | `50` | Threshold in ms for counting a query/prepared execution as slow. Valid values are clamped to `[1, 60000]`; missing, empty, malformed, signed or overflow input falls back to the default with a single startup warning |
| `RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS` | `16` | Reserved subsystem capacity hint. Valid values are clamped to `[4, 64]`; missing, empty, malformed, signed or overflow input falls back to the default with a single startup warning. V1 uses the fixed approved six labels below; this setting reserves headroom for future bounded expansion |

The slow threshold is inclusive: a query whose duration is greater than or
equal to the threshold counts as slow.

Core observability must be enabled for the `.prom` textfile export to be
written:

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_SQL_OBSERVABILITY=1
RATHENA_SQL_OBSERVABILITY_SLOW_MS=50
RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS=16
```

When `RATHENA_CORE_OBSERVABILITY=0`, SQL counters still accumulate in memory
and will appear in the next snapshot if core export is enabled later within the
same process lifetime.

## Example

PowerShell:

```powershell
$env:RATHENA_CORE_OBSERVABILITY = "1"
$env:RATHENA_SQL_OBSERVABILITY = "1"
$env:RATHENA_SQL_OBSERVABILITY_SLOW_MS = "1"
$env:RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS = "16"
.\map-server.exe
```

Bash:

```bash
export RATHENA_CORE_OBSERVABILITY=1
export RATHENA_SQL_OBSERVABILITY=1
export RATHENA_SQL_OBSERVABILITY_SLOW_MS=1
export RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS=16
./map-server
```

## Metrics list

Aggregate metrics (no labels):

| Metric | Type | Description |
| --- | --- | --- |
| `rathena_sql_queries_total` | counter | Total SQL queries executed |
| `rathena_sql_query_failures_total` | counter | Total SQL query failures |
| `rathena_sql_slow_queries_total` | counter | Total slow SQL queries |
| `rathena_sql_query_duration_last_milliseconds` | gauge | Duration of the last SQL query in milliseconds |
| `rathena_sql_query_duration_max_milliseconds` | gauge | Maximum observed SQL query duration in milliseconds |
| `rathena_sql_prepared_executions_total` | counter | Total prepared statement executions |
| `rathena_sql_prepared_failures_total` | counter | Total prepared statement execution failures |
| `rathena_sql_prepared_slow_total` | counter | Total slow prepared statement executions |
| `rathena_sql_prepared_duration_last_milliseconds` | gauge | Duration of the last prepared statement execution in milliseconds |
| `rathena_sql_prepared_duration_max_milliseconds` | gauge | Maximum observed prepared statement execution duration in milliseconds |
| `rathena_sql_connect_attempts_total` | counter | Total SQL connection attempts |
| `rathena_sql_connect_failures_total` | counter | Total SQL connection failures |
| `rathena_sql_ping_total` | counter | Total SQL ping operations |
| `rathena_sql_ping_failures_total` | counter | Total SQL ping failures |
| `rathena_sql_reconnect_events_total` | counter | Total SQL reconnect events |
| `rathena_sql_subsystem_overflow_total` | counter | Total subsystem label overflows |

Per-subsystem metrics (label `subsystem="..."`):

| Metric | Type | Description |
| --- | --- | --- |
| `rathena_sql_queries_total` | counter | Total SQL queries executed by subsystem |
| `rathena_sql_query_failures_total` | counter | Total SQL query failures by subsystem |
| `rathena_sql_slow_queries_total` | counter | Total slow SQL queries by subsystem |
| `rathena_sql_query_duration_last_milliseconds` | gauge | Duration of the last SQL query by subsystem in milliseconds |
| `rathena_sql_query_duration_max_milliseconds` | gauge | Maximum observed SQL query duration by subsystem in milliseconds |
| `rathena_sql_prepared_executions_total` | counter | Total prepared statement executions by subsystem |
| `rathena_sql_prepared_failures_total` | counter | Total prepared statement execution failures by subsystem |
| `rathena_sql_prepared_slow_total` | counter | Total slow prepared statement executions by subsystem |
| `rathena_sql_prepared_duration_last_milliseconds` | gauge | Duration of the last prepared statement execution by subsystem in milliseconds |
| `rathena_sql_prepared_duration_max_milliseconds` | gauge | Maximum observed prepared statement execution duration by subsystem in milliseconds |

Per-subsystem samples are emitted in fixed enum order during rendering, so the
textfile output is deterministic.

## Subsystem labels

Approved V1 labels:

```text
login
char
map
log
web
unknown
```

Each server process selects its default subsystem once during startup:

- `login-server` selects `login`
- `char-server` selects `char`
- `map-server` selects `map`
- `web-server` selects `web`
- the optional dedicated log SQL handle may select `log` where the existing
  architecture provides a stable explicit role boundary
- any unset, invalid, or future subsystem maps to `unknown`

V1 does not add thread-local per-call labels or dynamic arbitrary strings.

## Cardinality cap and overflow

V1 uses a fixed bounded array for the six approved subsystems. Invalid or
out-of-range enum values are coerced to `unknown` and increment
`rathena_sql_subsystem_overflow_total` exactly once per admission. All counters
saturate at `UINT64_MAX` instead of wrapping.

If `rathena_sql_subsystem_overflow_total` is non-zero, verify that all server
startup points call `sql_observability_set_subsystem()` with one of the approved
values before any SQL operation is recorded.

## Privacy exclusions

The following are never exported, retained, hashed, classified, or logged
through the observability system:

- SQL or prepared-statement text
- SQL parameters, bind values, or result rows
- database, schema, table, column, index, or view names
- hostnames, ports, usernames, passwords, connection strings, or SSL settings
- MySQL/MariaDB error messages, error codes, or SQLSTATE values
- account, character, guild, party, item, IP, session, token, chat, or
  transaction identifiers
- source file, function, line, or call-site identifiers

Only counters, durations, success/failure state, and the approved subsystem
labels above are exported.

## Disabled/enabled overhead

- Disabled: a single `sql_observability_enabled()` boolean check per hook. No
  clock read, no counter update, no allocation, no lock, no SQL text inspection,
  no formatting, no file I/O.
- Enabled: two clock reads per measured database operation, a small number of
  saturating 64-bit additions, and deterministic rendering only during the
  existing core observability snapshot. Hooks are designed to be lock-free and
  never block SQL execution.

## Runtime verification

### Disabled smoke test

```powershell
$env:RATHENA_CORE_OBSERVABILITY = "1"
$env:RATHENA_SQL_OBSERVABILITY = "0"
.\map-server.exe --run-once
```

Verify:

- process exits with code 0
- the `.prom` file contains core metrics but no SQL metric lines
- no extra `.tmp` files remain in `log/metrics`

### Enabled smoke test

```powershell
$env:RATHENA_CORE_OBSERVABILITY = "1"
$env:RATHENA_SQL_OBSERVABILITY = "1"
$env:RATHENA_SQL_OBSERVABILITY_SLOW_MS = "1"
$env:RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS = "16"
.\map-server.exe --run-once
```

Verify:

- SQL metric lines appear in `log/metrics/rathena_map.prom`
- counters increase as the map-server performs queries, prepared executions,
  pings, and connection attempts
- subsystem labels are only from the approved set
- `rathena_sql_slow_queries_total` and `rathena_sql_prepared_slow_total` may be
  non-zero because the threshold is set to 1 ms

### Privacy scan

Search `log/metrics/rathena_map.prom` for:

- SQL query fragments (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, table names)
- credentials or passwords
- account/character IDs
- MySQL error text
- test sentinels

Expected result: zero matches.

### Local builds

- Build `map-server` Debug and Release if available
- Build `login-server`, `char-server`, and `web-server`
- Run the SQL observability unit tests:

```powershell
# Windows (MSVC)
cl /std:c++17 /EHsc /W4 /Isrc\common tools\observability\tests\sql_observability_test.cpp /Fe:sql_observability_test.exe
.\sql_observability_test.exe

cl /std:c++17 /EHsc /W4 /DRATHENA_SQL_OBSERVABILITY_TESTING /Isrc\common tools\observability\tests\sql_observability_test.cpp /Fe:sql_observability_test_runtime.exe
.\sql_observability_test_runtime.exe
```

```sh
# Linux (GCC/Clang)
g++ -std=c++17 -Wall -Wextra -Werror -Isrc/common tools/observability/tests/sql_observability_test.cpp -o sql_observability_test
./sql_observability_test

g++ -std=c++17 -Wall -Wextra -Werror -DRATHENA_SQL_OBSERVABILITY_TESTING -Isrc/common tools/observability/tests/sql_observability_test.cpp -o sql_observability_test_runtime
./sql_observability_test_runtime
```

## Failure handling

Observability failures never interrupt SQL execution:

- invalid configuration falls back safely with a single startup warning
- invalid or excess subsystem values map to `unknown` and increment the overflow
  counter
- saturating counters prevent numeric wrap-around
- textfile write failures are handled by the core observability writer
- lifecycle calls are idempotent
- shutdown clears observability state without changing SQL handle shutdown order
- all existing SQL return codes and error-handler calls remain authoritative

## Rollback

To disable SQL observability:

1. unset `RATHENA_SQL_OBSERVABILITY` or set it to `0`, or
2. restart the server without the variable.

The instrumentation is additive and self-contained; the only touch points in
existing code are the common SQL call sites, server startup subsystems selection,
core observability rendering integration, and build project file entries for the
new common sources. The `.prom` output file is runtime data and may be removed
if desired; it is not required for server operation.
