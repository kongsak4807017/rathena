# Slow Script Observability Hybrid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add disabled-by-default slow-script execution observability around `run_script_main()` with explicit bounded categories, execution-slice timing, command accounting, and failure/abort metrics without changing script or gameplay behavior.

**Architecture:** Script callers assign one fixed `ScriptObservabilityCategory` to the script state or invocation context. `run_script_main()` remains the single timing/accounting hook: it measures one active execution slice, counts bytecode-loop iterations, detects abnormal termination, and records bounded aggregate/per-category metrics through the existing core observability writer.

**Tech Stack:** C++17, rAthena map-server script engine, existing core/packet/SQL observability textfile exporter, monotonic rAthena timer utilities, GitHub Actions, GCC, Clang, MSVC, CMake, CodeQL.

## Global Constraints

- Feature is disabled unless `RATHENA_SCRIPT_OBSERVABILITY` is one of `1`, `true`, `on`, or `yes`, case-insensitive.
- `RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS` defaults to `25` and is clamped to `[1, 60000]` milliseconds.
- One observation is one invocation of `run_script_main(struct script_state* st)`.
- Sleep, dialog wait, player input wait, timer delay, and time between suspension and resume are excluded.
- Approved category labels are exactly `npc`, `event`, `timer`, `item`, `skill`, `quest`, `instance`, and `unknown`.
- Category assignment is explicit; never infer a category from filename, path, NPC name, event label, script text, map name, IDs, command names, or function names.
- Failure metrics count execution error/abort only; warnings and normal suspend/resume are not failures.
- Disabled hooks perform no clock read, allocation, lock, formatting, file I/O, source inspection, or category-string generation beyond the enable check.
- Enabled hot path uses two monotonic clock reads per slice, one local command counter, fixed enum lookup, bounded fixed storage, and saturating `uint64_t` updates.
- Do not change bytecode order, return values, script states, sleep/resume/rerun behavior, infinity-loop thresholds, scheduler semantics, gameplay, database behavior, or protocol behavior.
- Reuse the existing core observability writer, timer, output path, temporary-file behavior, and shutdown snapshot. Do not add another writer, timer, endpoint, or metrics file.
- Never export script source, filename/path, NPC/event/function/command names, line numbers, map names, IDs, variables, arguments, values, error text, or stack traces.
- Unrelated refactoring and unrelated working-tree files are prohibited from commits.

---

## File Map

**Create**

- `src/map/script_observability_pure.hpp` — config parsing, category enum/labels, saturating model, deterministic Prometheus rendering.
- `src/map/script_observability.hpp` — public runtime API and execution-slice record interface.
- `src/map/script_observability_internal.hpp` — bounded runtime state and test seams.
- `src/map/script_observability.cpp` — lifecycle, runtime recording, rendering, clock seam.
- `tools/observability/tests/script_observability_test.cpp` — pure/runtime tests independent of a live server.
- `docs/observability/SCRIPT_OBSERVABILITY_V1.md` — operating, privacy, validation, and rollback guide.

**Modify**

- `src/map/script.hpp` — category type/context only where required by authoritative script state or invocation interfaces.
- `src/map/script.cpp` — central `run_script_main()` hook, command counter, failure classification, category-preserving resume path.
- Explicit script entry points in `src/map/npc.cpp`, `src/map/itemdb.cpp`, `src/map/skill.cpp`, `src/map/quest.cpp`, `src/map/instance.cpp`, and other files only after inspection proves they are authoritative callers.
- `src/map/core_observability.cpp` — lifecycle and Prometheus append through the existing writer.
- `src/map/CMakeLists.txt`, `src/map/Makefile.in`, and relevant MSVC project files — include new map observability source/header files.
- `.github/workflows/observability-tests.yml` — path filters and GCC/Clang/MSVC tests.

---

### Task 1: Define Configuration, Categories, and Privacy Contract

**Files:**
- Create: `src/map/script_observability_pure.hpp`
- Create: `tools/observability/tests/script_observability_test.cpp`
- Modify: `.github/workflows/observability-tests.yml`

**Interfaces:**
- Produces `ScriptObservabilityCategory`, `script_observability_category_label`, `script_observability_parse_bool`, `script_observability_parse_u32`, `script_observability_is_slow`, and saturating arithmetic helpers.

- [ ] **Step 1: Inspect existing observability test conventions**

```bash
git grep -n "packet_observability_test\|sql_observability_test" -- .github tools src
```

Use the existing warning-as-error compiler flags and standalone test structure.

- [ ] **Step 2: Write failing category/configuration tests**

Add checks equivalent to:

```cpp
CHECK(script_observability_parse_bool(nullptr, false) == false);
CHECK(script_observability_parse_bool("YES", false) == true);
CHECK(script_observability_parse_bool("off", true) == false);
CHECK(script_observability_parse_u32(nullptr, 25, 1, 60000) == 25);
CHECK(script_observability_parse_u32("0", 25, 1, 60000) == 1);
CHECK(script_observability_parse_u32("70000", 25, 1, 60000) == 60000);
CHECK(script_observability_parse_u32("bad", 25, 1, 60000) == 25);
CHECK(script_observability_is_slow(24, 25) == false);
CHECK(script_observability_is_slow(25, 25) == true);
CHECK(script_observability_is_slow(26, 25) == true);
```

Test labels for all eight approved categories and invalid-enum fallback to `unknown`.

- [ ] **Step 3: Run test and verify failure**

Expected: compilation fails because the new interfaces do not exist.

- [ ] **Step 4: Implement minimal dependency-free helpers**

Define:

```cpp
enum class ScriptObservabilityCategory : uint8_t {
    Npc,
    Event,
    Timer,
    Item,
    Skill,
    Quest,
    Instance,
    Unknown,
};
```

Implement deterministic enum-to-label mapping, safe parsing, threshold comparison, and saturating addition. Do not add rAthena runtime headers to the pure file.

- [ ] **Step 5: Run GCC/Clang or available local compiler with warnings as errors**

Expected: all tests pass with no warning.

- [ ] **Step 6: Commit**

```bash
git add src/map/script_observability_pure.hpp tools/observability/tests/script_observability_test.cpp .github/workflows/observability-tests.yml
git commit -m "test: define script observability behavior"
```

---

### Task 2: Implement the Bounded Metrics Model

**Files:**
- Modify: `src/map/script_observability_pure.hpp`
- Modify: `tools/observability/tests/script_observability_test.cpp`

**Interfaces:**
- Produces `ScriptObservabilityCounters`, `ScriptObservabilitySnapshot`, `record_slice(...)`, and `script_observability_render_prometheus(const ScriptObservabilitySnapshot&)`.

- [ ] **Step 1: Write failing model tests**

Cover aggregate/per-category totals, failures, slow slices, last/max duration, total/max commands, invalid-category fallback, and `UINT64_MAX` saturation.

Use this record signature in tests:

```cpp
snapshot.record_slice(
    ScriptObservabilityCategory::Npc,
    25,
    17,
    false,
    25
);
```

The arguments are category, duration milliseconds, command count, failed, and slow threshold.

- [ ] **Step 2: Verify test failure**

Expected: missing model and render interfaces.

- [ ] **Step 3: Implement fixed storage**

Use `std::array<ScriptObservabilityCounters, 8>` plus one aggregate counter. Do not use maps, dynamic labels, or heap allocation for admission/update.

Counters include:

```cpp
uint64_t execution_slices_total;
uint64_t execution_failures_total;
uint64_t slow_execution_slices_total;
uint64_t execution_duration_last_ms;
uint64_t execution_duration_max_ms;
uint64_t commands_total;
uint64_t commands_max_per_slice;
```

- [ ] **Step 4: Implement deterministic Prometheus output**

Render aggregate metrics first, followed by category-labeled metrics in enum order. Use the exact metric names from the approved spec.

- [ ] **Step 5: Add privacy sentinel scan**

Assert output does not contain sentinels representing NPC names, script paths, event labels, player IDs, map names, source text, or error strings.

- [ ] **Step 6: Run tests and commit**

```bash
git add src/map/script_observability_pure.hpp tools/observability/tests/script_observability_test.cpp
git commit -m "feat: add bounded script observability model"
```

---

### Task 3: Add Runtime State, Lifecycle, and Clock Seam

**Files:**
- Create: `src/map/script_observability.hpp`
- Create: `src/map/script_observability_internal.hpp`
- Create: `src/map/script_observability.cpp`
- Modify: `src/map/CMakeLists.txt`
- Modify: `src/map/Makefile.in`
- Modify: relevant MSVC project files discovered by inspection
- Modify: `tools/observability/tests/script_observability_test.cpp`

**Interfaces:**
- Produces:

```cpp
void script_observability_init();
void script_observability_final();
bool script_observability_enabled();
void script_observability_record_slice(
    ScriptObservabilityCategory category,
    uint64_t duration_ms,
    uint64_t commands,
    bool failed
);
std::string script_observability_render_prometheus();
```

Test-only interfaces under `RATHENA_SCRIPT_OBSERVABILITY_TESTING`:

```cpp
void script_observability_test_reset(bool enabled, uint32_t slow_ms);
ScriptObservabilitySnapshot script_observability_test_snapshot();
void script_observability_test_set_clock(uint64_t (*clock_fn)());
```

- [ ] **Step 1: Write failing lifecycle/runtime tests**

Test disabled recording, enabled recording, default threshold 25, idempotent init/final, malformed config fallback, rendering, and injected deterministic clock support.

- [ ] **Step 2: Verify failure**

Expected: missing runtime API.

- [ ] **Step 3: Implement one bounded state object**

Read environment only in `script_observability_init()`. Emit at most one warning per malformed setting and never echo raw input.

- [ ] **Step 4: Preserve the disabled-path contract**

`script_observability_record_slice()` must begin with:

```cpp
if (!script_observability_enabled()) {
    return;
}
```

No clock is read inside this record function; `run_script_main()` owns the two enabled-only reads.

- [ ] **Step 5: Add source/build manifests**

Follow existing map source registration conventions. Do not move unrelated files.

- [ ] **Step 6: Run standalone runtime tests and map-server build**

Expected: tests and build pass.

- [ ] **Step 7: Commit**

```bash
git add src/map/script_observability.hpp src/map/script_observability_internal.hpp src/map/script_observability.cpp src/map/CMakeLists.txt src/map/Makefile.in tools/observability/tests/script_observability_test.cpp
git add <exact-msvc-project-files-inspected>
git commit -m "feat: add script observability runtime state"
```

Inspect `git diff --cached --name-only` before committing.

---

### Task 4: Establish Explicit Category Context

**Files:**
- Modify: `src/map/script.hpp`
- Modify: `src/map/script.cpp`
- Test: `tools/observability/tests/script_observability_test.cpp`

**Interfaces:**
- Produces one authoritative category value associated with the current script execution state or invocation.
- Default/fallback is `ScriptObservabilityCategory::Unknown`.

- [ ] **Step 1: Map script allocation, run, sleep, and resume paths**

```bash
git grep -n "script_alloc_state\|run_script_main\|run_script\|run_script_timer\|sleep.timer\|RERUNLINE" src/map/script.cpp src/map/script.hpp src/map
```

Document which field or invocation parameter survives suspend/resume without dynamic allocation.

- [ ] **Step 2: Write failing context tests**

Test default `unknown`, explicit category preservation into the current slice, and preservation across sleep/resume while counting resumed execution as a new slice.

- [ ] **Step 3: Add minimal fixed enum context**

Prefer a field on `script_state` when lifecycle inspection confirms it is allocated and copied consistently. Otherwise add an explicit parameter to the authoritative allocation/run wrapper. Do not use global mutable category state or infer from content.

- [ ] **Step 4: Verify no behavior changes**

Build with observability disabled and compare the script state transitions around `RUN`, `STOP`, `RERUNLINE`, `GOTO`, and `END` to baseline.

- [ ] **Step 5: Commit**

```bash
git add src/map/script.hpp src/map/script.cpp tools/observability/tests/script_observability_test.cpp
git commit -m "feat: add explicit script category context"
```

---

### Task 5: Instrument `run_script_main()` as the Single Execution Hook

**Files:**
- Modify: `src/map/script.cpp`
- Modify: `tools/observability/tests/script_observability_test.cpp`

**Interfaces:**
- Consumes category context and `script_observability_record_slice(...)`.
- Produces exactly one metric record for each invocation of `run_script_main()` when enabled.

- [ ] **Step 1: Capture baseline control flow**

Record the existing entry, command/goto counters, bytecode loop, all state exits, and cleanup behavior. Do not restructure the switch except where required for unambiguous failure flags.

- [ ] **Step 2: Write failing slice-boundary tests**

Use the deterministic clock seam to verify 24/25/26 ms classification, separate suspend/resume slices, normal completion, and exactly-one recording.

- [ ] **Step 3: Add enabled-only start clock and local counters**

At function entry:

```cpp
const bool observe = script_observability_enabled();
const uint64_t started_ms = observe ? script_observability_monotonic_milliseconds() : 0;
uint64_t observed_commands = 0;
bool observed_failure = false;
```

The actual helper name may follow repository naming, but tests and implementation must match exactly.

- [ ] **Step 4: Count one documented unit consistently**

Increment `observed_commands` once per bytecode-loop iteration after a command is decoded. Do not change `cmdcount` or `gotocount` semantics.

- [ ] **Step 5: Classify abnormal exits explicitly**

Set `observed_failure = true` only for unknown command, command-count guard, goto-count guard, runtime error/forced abort paths proven by code inspection. Do not classify warnings, sleep, suspend, dialog wait, normal `END`, or `RERUNLINE` as failures.

- [ ] **Step 6: Record at one common exit**

When `observe` is true, read the monotonic clock once at exit, compute non-negative duration, and record exactly once. Do not read the clock in the loop.

- [ ] **Step 7: Run focused tests and full map-server build**

Expected: all tests pass and no warnings.

- [ ] **Step 8: Commit**

```bash
git add src/map/script.cpp tools/observability/tests/script_observability_test.cpp
git commit -m "feat: instrument script execution slices"
```

---

### Task 6: Assign Explicit Categories at Authoritative Call Sites

**Files:**
- Modify only authoritative entry-point files found by inspection, expected among `src/map/npc.cpp`, `src/map/itemdb.cpp`, `src/map/skill.cpp`, `src/map/quest.cpp`, `src/map/instance.cpp`, and `src/map/script.cpp`.
- Modify tests as needed for call-site/category contracts.

**Interfaces:**
- Consumes the category context API from Task 4.

- [ ] **Step 1: Inventory every direct entry into the script engine**

```bash
git grep -n "run_script(\|run_script_main(\|script_alloc_state(" src/map
```

Produce a table in the execution report mapping each authoritative caller to one category or `unknown`.

- [ ] **Step 2: Apply conservative categories**

Use:

```text
NPC interaction/general NPC script -> npc
named server event -> event
scheduled/timer callback -> timer
item use/equip/autobonus script -> item
skill-triggered script -> skill
quest-triggered script -> quest
instance lifecycle script -> instance
ambiguous/shared/internal caller -> unknown
```

Do not guess from filenames, labels, or script text. A shared caller remains `unknown` unless its caller supplies a category explicitly.

- [ ] **Step 3: Verify bounded labels only**

Search the diff for string-based category assignment. Expected: none.

- [ ] **Step 4: Build and exercise representative paths**

Run or test at least one available NPC/event/timer/item/instance path. Skill/quest paths may remain `unknown` only when no safe authoritative boundary exists; document evidence rather than forcing a misleading label.

- [ ] **Step 5: Commit**

```bash
git add <exact-authoritative-entry-point-files> tools/observability/tests/script_observability_test.cpp
git commit -m "feat: assign bounded script categories"
```

---

### Task 7: Export Through Existing Core Observability

**Files:**
- Modify: `src/map/core_observability.cpp`
- Modify: `tools/observability/tests/script_observability_test.cpp` if rendering integration requires coverage

**Interfaces:**
- Consumes `script_observability_init()`, `script_observability_final()`, `script_observability_enabled()`, and `script_observability_render_prometheus()`.

- [ ] **Step 1: Follow packet/SQL integration order**

```bash
git grep -n "packet_observability_init\|sql_observability_init\|render_prometheus\|write_snapshot" src/map/core_observability.cpp
```

- [ ] **Step 2: Initialize/finalize exactly once**

Initialize script observability during existing observability startup before script execution can produce measured slices. Finalize only after the final snapshot opportunity.

- [ ] **Step 3: Append to the existing output string**

Append only when script observability is enabled. Do not create a new file, writer, timer, endpoint, or flush path.

- [ ] **Step 4: Run enabled/disabled snapshot tests**

Disabled: no `rathena_script_` metrics. Enabled: all approved aggregate/category metrics appear in the existing `.prom` file.

- [ ] **Step 5: Commit**

```bash
git add src/map/core_observability.cpp
git commit -m "feat: export script metrics through core observability"
```

---

### Task 8: Complete CI, Documentation, and Runtime Verification

**Files:**
- Modify: `.github/workflows/observability-tests.yml`
- Create: `docs/observability/SCRIPT_OBSERVABILITY_V1.md`
- Modify: `tools/observability/tests/script_observability_test.cpp`

**Interfaces:**
- Produces the final operator and test contract.

- [ ] **Step 1: Add GCC, Clang, and MSVC test commands**

Compile pure and runtime variants with `-Wall -Wextra -Werror` or `/W4 /WX`. Add path filters for all new and integrated files.

- [ ] **Step 2: Run the complete local matrix available**

Run standalone tests, Release build, CMake, Pre-Renewal/Renewal, VIP, and packet-version builds available locally. Record unavailable platforms honestly for GitHub CI.

- [ ] **Step 3: Run disabled runtime smoke test**

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_SCRIPT_OBSERVABILITY=0
```

Verify baseline startup/script behavior and absence of script metrics.

- [ ] **Step 4: Run enabled runtime smoke test**

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_SCRIPT_OBSERVABILITY=1
RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS=1
```

Exercise representative scripts, confirm slice/category counters, duration, command counts, and no extra temporary file.

- [ ] **Step 5: Run privacy scan**

Search generated metrics for known NPC names, event labels, script paths, map names, account/character IDs, script snippets, and error text. Expected: zero matches.

- [ ] **Step 6: Write operator documentation**

Document configuration, exact metric names, execution-slice semantics, categories, failure semantics, privacy exclusions, enabled/disabled validation, performance model, and rollback:

```text
Unset RATHENA_SCRIPT_OBSERVABILITY or set it to 0.
```

- [ ] **Step 7: Run repository hygiene checks and commit**

```bash
git diff --check
git status --short
git add .github/workflows/observability-tests.yml tools/observability/tests/script_observability_test.cpp docs/observability/SCRIPT_OBSERVABILITY_V1.md
git commit -m "ci: test and document script observability"
```

---

### Task 9: Final Review, Push, and Draft PR

**Files:**
- Review every changed file against the approved spec.

- [ ] **Step 1: Confirm branch and clean staging discipline**

```bash
git branch --show-current
git status --short
git log --oneline --decorate -15
```

Branch must be `feat/slow-script-instrumentation-v1`.

- [ ] **Step 2: Run final verification from final HEAD**

Run all local tests/builds again and:

```bash
git diff --check master...HEAD
```

- [ ] **Step 3: Self-review the completion gate**

Verify exactly-one record per invocation, two clock reads only when enabled, no per-command clocks, fixed labels, 25 ms default, separate resume slices, correct abort semantics, unchanged script behavior, and existing writer reuse.

- [ ] **Step 4: Push branch**

```bash
git push -u origin feat/slow-script-instrumentation-v1
```

- [ ] **Step 5: Open a Draft PR to `master`**

Title:

```text
feat: add slow script performance instrumentation (A2.2-3)
```

The body must include architecture, configuration, metrics, category mapping table, exact `run_script_main()` hook semantics, privacy exclusions, local evidence, unavailable environments, scope safety, and rollback.

- [ ] **Step 6: Wait for every required GitHub gate**

Required:

- Observability tests
- GCC
- Clang
- Clang on macOS
- MSVS
- CMake
- Pre-Renewal and Renewal
- VIP mode
- different packet versions
- NPC Scripts and DB validation
- CodeQL

Do not mark Ready while any workflow is incomplete/failed, CodeQL has a new alert, or a review thread is unresolved.

- [ ] **Step 7: Produce final execution report**

Report commit list, files, category coverage, local tests, runtime smoke evidence, CI, CodeQL, review threads, PR URL, and unrelated files intentionally excluded.

---

## Completion Gate

Do not mark the PR Ready or merge until all are true:

- each `run_script_main()` invocation produces exactly one slice record when enabled;
- sleep/wait intervals are excluded and resume is a separate slice;
- categories are explicit and limited to the approved eight values;
- invalid/unset category maps to `unknown`;
- slow boundary is inclusive and defaults to 25 ms;
- command accounting has one documented definition and does not alter existing guards;
- only execution error/abort increments failure metrics;
- disabled mode performs no clock/allocation/lock/formatting/I/O/source inspection beyond the enable check;
- no script content or identifying data appears in metrics;
- script execution, state transitions, scheduling, gameplay, database, and protocol behavior remain unchanged;
- existing core observability remains the sole writer/timer/path;
- local tests and smoke tests pass;
- all GitHub workflows pass on the exact final HEAD;
- CodeQL has no new open alert;
- every review thread is resolved.