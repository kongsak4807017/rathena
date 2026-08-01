# SQL Observability Hybrid Design

Date: 2026-08-01
Status: Approved design
Branch: `feat/sql-performance-instrumentation-v1`
Milestone: A2.2-2

## Objective

Add disabled-by-default SQL performance instrumentation to rAthena so operators can measure query volume, latency, slow operations, failures, connection health, and prepared-statement pressure without exporting SQL text, credentials, schema details, table names, player identifiers, or transaction data.

## Chosen approach

Use a hybrid architecture:

1. instrument the central common SQL layer for complete coverage of normal queries and prepared statements; and
2. attach an explicit bounded subsystem tag selected once by each server process or SQL role.

The implementation must not classify operations by inspecting SQL text.

## Scope

This milestone includes:

- normal query execution through `Sql_QueryV` and `Sql_QueryStr`;
- prepared statement execution through the central `SqlStmt` execution path;
- connection attempts and failures;
- ping attempts and failures;
- reconnect/error-handler events where the existing code exposes a stable central boundary;
- aggregate and bounded per-subsystem metrics;
- export through the existing core observability Prometheus textfile writer;
- tests, documentation, CI coverage, and runtime smoke verification.

## Non-goals

This milestone does not include:

- SQL text logging, hashing, normalization, fingerprinting, or sampling;
- database, schema, table, column, index, or statement labels;
- source file, function, or line-number labels;
- account, character, guild, item, IP, session, or transaction identifiers;
- database migrations or index changes;
- connection pooling or pool sizing changes;
- retry, timeout, reconnect, transaction, or error-handling policy changes;
- query cancellation;
- automated `EXPLAIN`;
- slow-query logs containing statements;
- MySQL or MariaDB server configuration tuning;
- HTTP endpoints, a second writer, a second timer, or a new metrics path.

## Configuration

Instrumentation is disabled unless explicitly enabled.

```text
RATHENA_SQL_OBSERVABILITY=0
RATHENA_SQL_OBSERVABILITY_SLOW_MS=50
RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS=16
```

Rules:

- accepted enable values are `1`, `true`, `on`, and `yes`, case-insensitive;
- the slow threshold is clamped to `[1, 60000]` milliseconds;
- subsystem capacity is clamped to `[4, 64]`;
- malformed values fall back to defaults and emit at most one startup warning per setting;
- disabled mode must not allocate observability state, read clocks, inspect SQL text, format labels, write files, or alter SQL behavior;
- textfile export requires existing core observability to be enabled.

## Architecture

### 1. Pure helper layer

Proposed file:

`src/common/sql_observability_pure.hpp`

Responsibilities:

- environment parsing and clamps;
- subsystem enum-to-label conversion;
- bounded subsystem admission and fallback;
- saturating counter updates;
- slow-operation classification;
- deterministic Prometheus rendering;
- pure tests without a database or running server.

This layer must not depend on MySQL/MariaDB handles, server runtime state, or SQL text.

### 2. Runtime state layer

Proposed files:

- `src/common/sql_observability.hpp`
- `src/common/sql_observability_internal.hpp`
- `src/common/sql_observability.cpp`

The runtime layer owns monotonic counters and last/max duration gauges. V1 introduces no histograms and no unbounded labels.

Public interfaces should include explicit lifecycle, subsystem selection, event recording, rendering, and reset functions. Exact signatures will be fixed in the implementation plan after inspection of existing common-library conventions.

### 3. Explicit subsystem context

Subsystem tags are process-level or SQL-role-level identifiers, not inferred from statements.

Approved V1 labels:

```text
login
char
map
log
web
unknown
```

Rules:

- each server selects its default subsystem once during startup;
- a dedicated log SQL handle may use `log` when the existing architecture provides a stable explicit role boundary;
- any unset, invalid, or excess subsystem maps to `unknown`;
- labels must remain bounded by configuration;
- V1 does not add thread-local per-call labels or dynamic arbitrary strings.

### 4. Normal query hooks

Instrument the smallest stable boundary surrounding the actual database call in:

- `Sql_QueryV`;
- `Sql_QueryStr`.

For each operation, record:

- attempt count;
- success or failure;
- duration last/max;
- slow count when duration is greater than or equal to the configured threshold;
- subsystem.

Timing must include the database execution and result-store call already performed by the function, while preserving the current order of buffer formatting, result cleanup, database call, error handling, and return values.

### 5. Prepared statement hooks

Instrument the central prepared-statement execution boundary, including execution failures and result-store failures where applicable.

Record:

- execution count;
- failure count;
- duration last/max;
- slow count;
- subsystem.

Preparation, binding, fetch, and execution behavior must remain unchanged. V1 does not export statement text or parameter metadata.

### 6. Connection health hooks

Instrument stable boundaries for:

- `Sql_Connect` attempts and failures;
- `Sql_Ping` attempts and failures;
- reconnect/error-handler events exposed by the current reconnect path.

The instrumentation must not change reconnect configuration, retry counts, timeout behavior, logging, or return codes.

### 7. Metrics integration

SQL metrics are appended to the existing core observability snapshot.

If SQL observability is enabled while core observability is disabled, counters may accumulate but no new writer, timer, output file, or lifecycle loop is created.

This preserves the existing atomic textfile workflow and avoids competing temporary files.

## Metrics

### Aggregate query metrics

```text
rathena_sql_queries_total
rathena_sql_query_failures_total
rathena_sql_slow_queries_total
rathena_sql_query_duration_last_milliseconds
rathena_sql_query_duration_max_milliseconds
```

### Prepared statement metrics

```text
rathena_sql_prepared_executions_total
rathena_sql_prepared_failures_total
rathena_sql_prepared_slow_total
rathena_sql_prepared_duration_last_milliseconds
rathena_sql_prepared_duration_max_milliseconds
```

### Connection health metrics

```text
rathena_sql_connect_attempts_total
rathena_sql_connect_failures_total
rathena_sql_ping_total
rathena_sql_ping_failures_total
rathena_sql_reconnect_events_total
rathena_sql_subsystem_overflow_total
```

### Bounded subsystem metrics

The aggregate query and prepared-statement metrics above may also be rendered with a bounded subsystem label:

```text
rathena_sql_queries_total{subsystem="map"}
rathena_sql_query_failures_total{subsystem="char"}
rathena_sql_slow_queries_total{subsystem="login"}
rathena_sql_prepared_executions_total{subsystem="map"}
rathena_sql_prepared_failures_total{subsystem="log"}
```

Only approved labels may appear. Dynamic strings are prohibited.

## Privacy and security

The instrumentation must never export, retain, hash, classify, or log through the observability system:

- SQL or prepared-statement text;
- SQL parameters or result values;
- database, schema, table, column, or index names;
- hostnames, ports, usernames, passwords, or connection strings;
- MySQL/MariaDB error messages;
- account, character, guild, party, item, IP, session, token, chat, or transaction identifiers;
- source file, function, line, or call-site names.

Only counters, durations, success/failure state, and approved subsystem labels are exported.

Unsigned 64-bit observability counters should use saturating increments rather than wraparound.

## Performance requirements

Disabled path:

- one predictable enable check at each hook;
- no clock read;
- no allocation;
- no lock;
- no SQL text inspection;
- no formatting;
- no file I/O.

Enabled path:

- two clock reads per measured database operation;
- constant-time aggregate updates;
- fixed or bounded subsystem storage;
- no copying of SQL text or parameters;
- rendering only during the existing observability snapshot;
- instrumentation failure must never interrupt SQL execution.

## Error handling

- invalid configuration falls back safely;
- invalid or excess subsystem values map to `unknown` and increment overflow only when capacity is actually exceeded;
- rendering failures follow existing core observability write-error behavior;
- lifecycle calls are idempotent;
- shutdown clears observability state without changing SQL handle shutdown order;
- all existing SQL return codes and error-handler calls remain authoritative.

## Testing

### Pure unit tests

Add `tools/observability/tests/sql_observability_test.cpp` covering:

- enable parsing;
- slow-threshold parsing and clamp;
- subsystem-capacity parsing and clamp;
- approved subsystem labels;
- invalid subsystem fallback;
- bounded subsystem admission and overflow;
- saturating counters;
- success/failure recording;
- slow classification at threshold boundaries;
- normal and prepared-statement rendering;
- connection-health rendering;
- empty-state rendering;
- deterministic ordering;
- proof that supplied SQL text, credentials, schema names, error strings, and identifiers do not appear in output.

### Integration and runtime verification

Disabled mode:

- servers start and stop normally;
- no SQL observability startup log beyond approved configuration reporting;
- no SQL metric lines are rendered;
- no additional `.tmp` files remain;
- SQL results and failures match baseline behavior.

Enabled mode:

- successful normal queries increment query totals and durations;
- failed normal queries increment only the intended failure counters;
- prepared execution success/failure paths update the intended metrics;
- connect and ping attempts/failures update the intended counters;
- subsystem labels remain in the approved bounded set;
- no SQL text, parameter, credential, schema, error message, or player identifier appears in `.prom` output;
- graceful shutdown writes a final snapshot through existing behavior;
- no additional writer or timer exists.

### Build coverage

Run:

- GCC with `-Wall -Wextra -Werror`;
- Clang with `-Wall -Wextra -Werror`;
- MSVC with `/W4 /WX`;
- CMake builds;
- Pre-Renewal and Renewal builds;
- VIP mode;
- packet-version matrix;
- NPC and DB validation;
- observability tests;
- CodeQL.

## Expected existing-code touch points

Expected changes are limited to:

- `src/common/sql.cpp` and related common SQL headers;
- central prepared-statement execution code;
- server startup points needed to select the explicit subsystem;
- existing core observability rendering/lifecycle integration;
- build project files for new common sources;
- observability tests and CI path filters;
- SQL observability documentation.

Unrelated refactoring is prohibited.

## Commit strategy

Recommended commits:

1. `test: define SQL observability configuration behavior`
2. `feat: add bounded SQL observability model`
3. `feat: add SQL observability runtime state`
4. `feat: instrument SQL connection health`
5. `feat: instrument normal SQL query execution`
6. `feat: instrument prepared statement execution`
7. `feat: export SQL metrics through core observability`
8. `feat: assign explicit SQL subsystem tags`
9. `ci: test SQL observability`
10. `docs: document SQL observability operation`

## Definition of done

A2.2-2 is complete when:

- normal queries and prepared statements are measured at stable common-layer boundaries;
- connection, ping, and reconnect events are counted where supported by existing central paths;
- subsystem labels are explicit, approved, and bounded;
- SQL text and sensitive values are never exported, retained, hashed, or classified;
- the feature is disabled by default;
- no SQL return value, result handling, retry, reconnect, transaction, gameplay, persistence, or database behavior changes;
- no schema migration or index change is introduced;
- pure tests and the full CI matrix pass;
- CodeQL has no new open alert;
- enabled and disabled runtime verification pass;
- a draft PR targets `master` from `feat/sql-performance-instrumentation-v1`;
- all review threads are resolved before readiness or merge.
