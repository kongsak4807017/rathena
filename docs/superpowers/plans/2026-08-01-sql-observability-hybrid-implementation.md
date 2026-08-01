# SQL Observability Hybrid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add disabled-by-default SQL performance instrumentation for normal queries, prepared statements, connection health, and bounded explicit subsystem tags without exporting SQL text or changing database behavior.

**Architecture:** Instrument the central common SQL layer and append metrics through the existing core observability writer. Use a fixed subsystem enum (`login`, `char`, `map`, `log`, `web`, `unknown`) selected explicitly at process or SQL-role startup; never infer labels from SQL text.

**Tech Stack:** C++17, rAthena common SQL wrappers, MySQL/MariaDB C API, existing core observability textfile exporter, GitHub Actions, GCC, Clang, MSVC, CMake, CodeQL.

## Global Constraints

- Feature is disabled unless `RATHENA_SQL_OBSERVABILITY` is one of `1`, `true`, `on`, or `yes`, case-insensitive.
- `RATHENA_SQL_OBSERVABILITY_SLOW_MS` defaults to `50` and is clamped to `[1, 60000]` milliseconds.
- `RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS` defaults to `16` and is clamped to `[4, 64]`.
- Approved subsystem labels are exactly: `login`, `char`, `map`, `log`, `web`, `unknown`.
- Never export, retain, hash, normalize, classify, or log SQL text, statement text, parameters, result values, database/schema/table/column/index names, credentials, hostnames, ports, error messages, player identifiers, source files, functions, or line numbers.
- Disabled hooks perform no clock read, allocation, lock, SQL-text inspection, formatting, file I/O, or behavior change.
- Do not change SQL return values, result handling, retry/reconnect policy, transaction behavior, persistence behavior, database schema, or indexes.
- Use unsigned 64-bit saturating counters.
- Reuse the existing core observability writer, timer, atomic textfile path, and graceful-shutdown snapshot; do not add another writer, timer, endpoint, or metrics path.
- Unrelated refactoring and unrelated local working-tree changes are prohibited from every commit.

---

## File Map

**Create**

- `src/common/sql_observability_pure.hpp` — configuration parsing, fixed subsystem labels, bounded model, saturating arithmetic, deterministic rendering helpers.
- `src/common/sql_observability.hpp` — public runtime API used by SQL hooks, server startup, and core observability.
- `src/common/sql_observability_internal.hpp` — runtime state structures and test-only reset/snapshot access.
- `src/common/sql_observability.cpp` — lifecycle, event recording, subsystem context, rendering.
- `tools/observability/tests/sql_observability_test.cpp` — pure and runtime-model tests without requiring a database.
- `docs/observability/SQL_OBSERVABILITY_V1.md` — operation, metrics, privacy, validation, rollback.

**Modify after inspection**

- `src/common/sql.cpp` — connect, ping, normal query, prepared execution, reconnect-event hooks.
- `src/common/sql.hpp` — only if required for explicit subsystem context or test-visible interfaces.
- `src/map/core_observability.cpp` and its header only where needed to append SQL metrics and coordinate lifecycle.
- Server startup files for login, char, map, and web processes to set one explicit process subsystem.
- Stable log-SQL initialization point only if a distinct log role is unambiguously available.
- Build manifests for common sources and MSVC projects.
- `.github/workflows/observability-tests.yml` path filters and compile commands.

---

### Task 1: Define Configuration and Privacy Behavior with Failing Tests

**Files:**
- Create: `tools/observability/tests/sql_observability_test.cpp`
- Create: `src/common/sql_observability_pure.hpp`
- Modify: `.github/workflows/observability-tests.yml`

**Interfaces:**
- Produces `SqlObservabilityConfig`, `sql_observability_parse_bool`, `sql_observability_parse_u32`, `sql_observability_is_slow`, and fixed subsystem definitions used by later tasks.

- [ ] **Step 1: Inspect packet observability test/build conventions**

Run:

```bash
git grep -n "packet_observability_test\|packet_observability_pure" -- .github tools src
```

Use the existing compile flags, include paths, test executable pattern, and workflow structure. Do not copy packet-specific metric names.

- [ ] **Step 2: Write failing configuration tests**

Define tests that assert:

```cpp
assert(sql_observability_parse_bool(nullptr, false) == false);
assert(sql_observability_parse_bool("YES", false) == true);
assert(sql_observability_parse_bool("off", true) == false);
assert(sql_observability_parse_u32(nullptr, 50, 1, 60000) == 50);
assert(sql_observability_parse_u32("0", 50, 1, 60000) == 1);
assert(sql_observability_parse_u32("70000", 50, 1, 60000) == 60000);
assert(sql_observability_parse_u32("bad", 50, 1, 60000) == 50);
assert(sql_observability_is_slow(49, 50) == false);
assert(sql_observability_is_slow(50, 50) == true);
```

Add compile-time/runtime assertions that the only labels are `login`, `char`, `map`, `log`, `web`, and `unknown`.

- [ ] **Step 3: Run the test and verify failure**

Use the same compiler command pattern as packet observability. Expected result: compilation fails because the SQL observability interfaces do not exist.

- [ ] **Step 4: Implement minimal pure configuration helpers**

Create the fixed enum:

```cpp
enum class SqlObservabilitySubsystem : uint8_t {
    Login,
    Char,
    Map,
    Log,
    Web,
    Unknown,
};
```

Implement case-insensitive boolean parsing, bounded integer parsing, threshold classification, and `constexpr` enum-to-label conversion. No dynamic strings and no SQL dependency.

- [ ] **Step 5: Run tests with warnings as errors**

Run with the available local compiler and exact workflow flags. Expected: all configuration tests pass and no warnings are emitted.

- [ ] **Step 6: Commit**

```bash
git add src/common/sql_observability_pure.hpp tools/observability/tests/sql_observability_test.cpp .github/workflows/observability-tests.yml
git commit -m "test: define SQL observability configuration behavior"
```

---

### Task 2: Add the Bounded Metrics Model

**Files:**
- Modify: `src/common/sql_observability_pure.hpp`
- Modify: `tools/observability/tests/sql_observability_test.cpp`

**Interfaces:**
- Produces `SqlObservabilityCounters`, `SqlObservabilitySubsystemCounters`, `sql_observability_saturating_add`, and deterministic rendering helpers.

- [ ] **Step 1: Add failing tests for counter saturation and subsystem admission**

Test saturation at `UINT64_MAX`, valid label admission, invalid enum fallback to `unknown`, configured-capacity overflow, deterministic enum ordering, and no arbitrary label acceptance.

- [ ] **Step 2: Run and verify failure**

Expected: compilation failure for missing counter/model types.

- [ ] **Step 3: Implement bounded model**

Define aggregate counters for normal queries, prepared statements, connect, ping, reconnect, and overflow. Define fixed per-subsystem slots indexed by enum; do not use `unordered_map`, dynamic label strings, or SQL text.

Durations use `uint64_t` last/max gauges. Counter updates saturate at `UINT64_MAX`.

- [ ] **Step 4: Implement deterministic Prometheus rendering**

Render aggregate metrics first, then subsystem metrics in fixed enum order. Escape logic is unnecessary because labels are compile-time constants.

- [ ] **Step 5: Add privacy sentinel test**

Pass sentinel strings such as:

```text
SELECT password FROM login
secret-password
account_id=123
DB error private value
```

into test scope but never into model interfaces. Assert none appear in rendered output.

- [ ] **Step 6: Run tests and commit**

```bash
git add src/common/sql_observability_pure.hpp tools/observability/tests/sql_observability_test.cpp
git commit -m "feat: add bounded SQL observability model"
```

---

### Task 3: Add Runtime State and Public API

**Files:**
- Create: `src/common/sql_observability.hpp`
- Create: `src/common/sql_observability_internal.hpp`
- Create: `src/common/sql_observability.cpp`
- Modify: build manifests and MSVC project files discovered by `git grep`.
- Modify: `tools/observability/tests/sql_observability_test.cpp`

**Interfaces:**
- Produces:

```cpp
void sql_observability_init();
void sql_observability_final();
bool sql_observability_enabled();
void sql_observability_set_subsystem(SqlObservabilitySubsystem subsystem);
SqlObservabilitySubsystem sql_observability_get_subsystem();
void sql_observability_record_query(uint64_t duration_ms, bool success);
void sql_observability_record_prepared(uint64_t duration_ms, bool success);
void sql_observability_record_connect(bool success);
void sql_observability_record_ping(bool success);
void sql_observability_record_reconnect();
std::string sql_observability_render_prometheus();
```

- [ ] **Step 1: Write failing lifecycle and recording tests**

Test disabled initialization, enabled initialization via controlled test configuration, idempotent init/final, default `unknown`, subsystem selection, successful and failed query/prepared recording, slow thresholds, connection events, reset, and rendered output.

- [ ] **Step 2: Run and verify failure**

Expected: missing runtime API.

- [ ] **Step 3: Implement runtime state**

Use one bounded state object. Disabled record functions return immediately before clock/model work. Avoid locks unless inspection proves SQL calls occur concurrently in the same process; if synchronization is required, document the existing threading evidence in the commit message and keep disabled path lock-free.

- [ ] **Step 4: Add startup warnings and idempotent lifecycle**

Read environment once at initialization. Emit at most one warning per malformed setting. Never include raw environment values if they could contain sensitive content; only name the invalid setting and applied default.

- [ ] **Step 5: Run tests/builds and commit**

```bash
git add src/common/sql_observability.hpp src/common/sql_observability_internal.hpp src/common/sql_observability.cpp tools/observability/tests/sql_observability_test.cpp src
git commit -m "feat: add SQL observability runtime state"
```

Before committing, inspect `git diff --cached --name-only` and remove unrelated files from the index.

---

### Task 4: Instrument Connection, Ping, and Reconnect Health

**Files:**
- Modify: `src/common/sql.cpp`
- Modify: `tools/observability/tests/sql_observability_test.cpp` only for model-level event expectations.

**Interfaces:**
- Consumes runtime record functions from Task 3.

- [ ] **Step 1: Locate exact central boundaries**

Run:

```bash
git grep -n "Sql_Connect\|Sql_Ping\|ra_mysql_error_handler\|mysql_reconnect" src/common src/login src/char src/map src/web
```

Use only stable central paths. Do not add duplicate reconnect counting at both caller and handler.

- [ ] **Step 2: Add hooks without changing control flow**

For `Sql_Connect`, record one attempt and the final success/failure result. For `Sql_Ping`, record one attempt and result. For reconnect, record exactly once at the existing authoritative event boundary.

- [ ] **Step 3: Verify disabled-path ordering**

Each record function must internally guard disabled mode. Do not add environment reads or clock reads in `Sql_Connect`/`Sql_Ping`.

- [ ] **Step 4: Build and run tests**

Verify existing SQL return codes and logs remain unchanged by comparing the relevant function bodies around the hooks.

- [ ] **Step 5: Commit**

```bash
git add src/common/sql.cpp
git commit -m "feat: instrument SQL connection health"
```

---

### Task 5: Instrument Normal Query Execution

**Files:**
- Modify: `src/common/sql.cpp`

**Interfaces:**
- Consumes `sql_observability_enabled()` and `sql_observability_record_query(duration_ms, success)`.

- [ ] **Step 1: Preserve baseline structure**

Record the original `Sql_QueryV` and `Sql_QueryStr` flow in the task report: result cleanup, buffer construction, `mysql_real_query`, error handler, `mysql_store_result`, second error check, return.

- [ ] **Step 2: Add conditional timing around the database operation**

When disabled, execute the original path without clock reads. When enabled, measure from immediately before `mysql_real_query` through `mysql_store_result` and final MySQL error evaluation.

Use existing monotonic rAthena tick utilities; do not use wall-clock time.

- [ ] **Step 3: Record exactly one result per attempted query**

Success is true only when the function returns `SQL_SUCCESS`. Every failure path must record once before returning. Do not inspect or copy `self->buf` for observability.

- [ ] **Step 4: Build and run query smoke tests**

Use existing database-backed test facilities if present. If none exist, run map/login/char startup against the configured local database and verify success/failure counters while confirming baseline return behavior.

- [ ] **Step 5: Commit**

```bash
git add src/common/sql.cpp
git commit -m "feat: instrument normal SQL query execution"
```

---

### Task 6: Instrument Prepared Statement Execution

**Files:**
- Modify: `src/common/sql.cpp`
- Modify: `src/common/sql.hpp` only if the central execution method declaration requires no-behavior-change helper integration.

**Interfaces:**
- Consumes `sql_observability_record_prepared(duration_ms, success)`.

- [ ] **Step 1: Locate the single execution boundary**

Run:

```bash
git grep -n "mysql_stmt_execute\|mysql_stmt_store_result\|SqlStmt::Execute" src/common/sql.cpp src/common/sql.hpp
```

Choose the narrowest method that covers all prepared executions exactly once.

- [ ] **Step 2: Add enabled-only monotonic timing**

Measure execution through result-store/error evaluation. Do not include prepare/bind unless the existing `Execute` method already owns them.

- [ ] **Step 3: Record exactly one success/failure event**

Preserve all current error messages, debug behavior, return codes, result buffering, and statement lifecycle.

- [ ] **Step 4: Build and run prepared-statement smoke paths**

Exercise at least one successful prepared statement and one controlled failure using existing test utilities or a minimal local runtime path. Verify no statement text or parameters appear in metrics.

- [ ] **Step 5: Commit**

```bash
git add src/common/sql.cpp src/common/sql.hpp
git commit -m "feat: instrument prepared statement execution"
```

---

### Task 7: Append SQL Metrics to Core Observability

**Files:**
- Modify: `src/map/core_observability.cpp`
- Modify: associated core observability header only if needed.
- Modify: build manifests required to link common SQL observability into all relevant binaries.

**Interfaces:**
- Consumes `sql_observability_init`, `sql_observability_final`, and `sql_observability_render_prometheus`.

- [ ] **Step 1: Inspect existing packet metrics integration**

Run:

```bash
git grep -n "packet_observability.*render\|core_observability_final\|render_prometheus" src/map src/common
```

Follow the existing lifecycle and final-snapshot pattern.

- [ ] **Step 2: Integrate lifecycle exactly once**

Initialize SQL observability before SQL hooks may execute in each relevant process. If core observability currently exists only in map-server, do not falsely claim login/char export coverage: either extend a shared exporter in a focused way or explicitly scope V1 export to processes with the existing writer while preserving counters elsewhere. Document the verified architecture in the task report.

- [ ] **Step 3: Append rendered metrics to existing snapshot**

Only append when both core observability and SQL observability are enabled. Do not create another file or temporary-file convention.

- [ ] **Step 4: Verify graceful shutdown final snapshot**

Run `--run-once` or equivalent and confirm SQL metrics appear only when enabled and no additional `.tmp` file remains.

- [ ] **Step 5: Commit**

```bash
git add src/map/core_observability.cpp src/map src/common
git commit -m "feat: export SQL metrics through core observability"
```

Inspect staged paths before committing.

---

### Task 8: Assign Explicit Subsystem Tags

**Files:**
- Modify: startup files for login, char, map, and web processes discovered by inspection.
- Modify: stable log SQL-role initialization only if present.

**Interfaces:**
- Consumes `sql_observability_set_subsystem(SqlObservabilitySubsystem)`.

- [ ] **Step 1: Identify authoritative startup functions**

Run:

```bash
git grep -n "do_init\|main_core\|Sql_Malloc\|Sql_Connect" src/login src/char src/map src/web
```

Select one process-level initialization point before the first SQL operation.

- [ ] **Step 2: Set fixed process tags**

Assign `Login`, `Char`, `Map`, and `Web` once. Assign `Log` only to an explicitly distinct log SQL role; never infer it from query text or table names.

- [ ] **Step 3: Verify unknown fallback**

Any process not explicitly assigned remains `unknown`. Do not introduce arbitrary string configuration.

- [ ] **Step 4: Build all server targets**

Build login, char, map, map-server-generator, web target if present, and all configured variants.

- [ ] **Step 5: Commit**

```bash
git add src/login src/char src/map src/web
git commit -m "feat: assign explicit SQL subsystem tags"
```

---

### Task 9: Complete CI, Documentation, and Runtime Verification

**Files:**
- Modify: `.github/workflows/observability-tests.yml`
- Create: `docs/observability/SQL_OBSERVABILITY_V1.md`
- Modify: `tools/observability/tests/sql_observability_test.cpp`

**Interfaces:**
- Produces the final test and operating contract.

- [ ] **Step 1: Expand CI test matrix**

Ensure SQL observability tests compile/run with GCC, Clang, and MSVC warning-as-error settings. Ensure workflow path filters include all new common SQL observability files and relevant integration files.

- [ ] **Step 2: Run the complete local verification matrix available**

Run pure tests, Debug/Release builds, CMake, Pre-Renewal/Renewal, VIP, and packet-version builds available locally. Record unavailable environments honestly for GitHub CI.

- [ ] **Step 3: Run disabled smoke test**

Set:

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_SQL_OBSERVABILITY=0
```

Verify server behavior matches baseline, no SQL metric lines appear, and no extra temporary files remain.

- [ ] **Step 4: Run enabled smoke tests**

Set:

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_SQL_OBSERVABILITY=1
RATHENA_SQL_OBSERVABILITY_SLOW_MS=1
RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS=16
```

Exercise normal query success/failure, prepared success/failure, connect/ping where safely testable, and each available process subsystem. Confirm counters and durations update.

- [ ] **Step 5: Run privacy scan**

Search generated `.prom` files for known query fragments, credentials, database/table names, MySQL error text, account IDs, and test sentinels. Expected: zero matches.

- [ ] **Step 6: Write operating documentation**

Document configuration, metrics, subsystem meanings, privacy exclusions, performance behavior, enabled/disabled validation, and rollback:

```text
Unset RATHENA_SQL_OBSERVABILITY or set it to 0.
```

- [ ] **Step 7: Run `git diff --check` and commit**

```bash
git diff --check
git add .github/workflows/observability-tests.yml tools/observability/tests/sql_observability_test.cpp docs/observability/SQL_OBSERVABILITY_V1.md
git commit -m "ci: test and document SQL observability"
```

---

### Task 10: Final Review, Push, and Draft PR

**Files:**
- Review all changes against the approved design spec.

- [ ] **Step 1: Confirm branch and unrelated working-tree safety**

```bash
git branch --show-current
git status --short
git log --oneline --decorate -15
```

Branch must be `feat/sql-performance-instrumentation-v1`. Do not stage pre-existing unrelated files.

- [ ] **Step 2: Run final complete verification**

Run all available local tests and builds again from the final HEAD. Run `git diff --check master...HEAD`.

- [ ] **Step 3: Self-review against the spec**

Verify every definition-of-done item, especially normal/prepared coverage, exact-once counting, disabled fast path, fixed subsystem labels, no SQL text retention, no behavior change, no second writer/timer, and no schema change.

- [ ] **Step 4: Push branch**

```bash
git push -u origin feat/sql-performance-instrumentation-v1
```

- [ ] **Step 5: Open a Draft PR to `master`**

Title:

```text
feat: add hybrid SQL performance instrumentation
```

PR body must include summary, configuration, complete metrics list, exact hot-path touch points, disabled-path guarantees, privacy exclusions, local verification evidence, known environment limitations, scope safety, and rollback.

- [ ] **Step 6: Wait for and evaluate every CI workflow**

Required gates:

- Observability tests
- GCC
- Clang
- Clang on macOS
- MSVS
- CMake
- Pre-Renewal and Renewal
- VIP mode
- different packet versions
- NPC/DB validation
- CodeQL

Fix failures in focused commits. Do not mark Ready while any workflow is incomplete/failed, any CodeQL alert is open, or any review thread is unresolved.

- [ ] **Step 7: Produce final execution report**

Report commit list, new/modified files, local tests, CI results, CodeQL status, review-thread status, PR URL, and unrelated working-tree files deliberately excluded.

---

## Completion Gate

Do not mark the PR Ready or merge until all of the following are true:

- normal queries and prepared statements are measured exactly once at central boundaries;
- connect, ping, and supported reconnect events are counted exactly once;
- subsystem labels are explicit and limited to the approved fixed set;
- no SQL text or sensitive value is exported, retained, hashed, or classified;
- disabled mode has no clock, allocation, lock, formatting, SQL inspection, or I/O overhead beyond the enable guard;
- SQL behavior, return codes, result handling, retry/reconnect policy, transactions, schema, and persistence remain unchanged;
- existing core observability is the only writer/timer/path;
- local smoke tests pass;
- all GitHub workflows pass on the exact final HEAD;
- CodeQL has zero new open alerts;
- all review threads are resolved.
