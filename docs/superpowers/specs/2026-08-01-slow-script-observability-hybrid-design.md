# A2.2-3 Slow Script Observability — Hybrid Central Hook + Explicit Bounded Category Context

Date: 2026-08-01
Status: Approved design
Branch: `feat/slow-script-instrumentation-v1`

## 1. Objective

Add opt-in, low-overhead observability for slow rAthena script execution without changing gameplay behavior, script scheduling, script semantics, or content. The system must identify which bounded script category is contributing to execution latency while preserving privacy and metric cardinality.

The implementation will combine:

1. a central timing and accounting hook in `run_script_main(struct script_state* st)`; and
2. an explicit bounded category context supplied by script entry points.

This milestone measures execution cost only. It is not a script profiler, debugger, scheduler rewrite, or automatic termination system.

## 2. Measurement Unit

One observation is one invocation of `run_script_main(struct script_state* st)`.

This is called an **execution slice**.

An execution slice includes only the time spent actively executing script bytecode during that invocation. It excludes:

- `sleep` duration;
- dialog or menu wait time;
- player input wait time;
- timer delay;
- time between suspension and resume;
- time spent outside the script engine between invocations.

When a suspended script resumes, the resumed invocation is recorded as a new execution slice.

## 3. Architecture

### 3.1 Central execution hook

`run_script_main()` is the single timing and accounting point. When observability is enabled it will:

1. read the monotonic clock at slice start;
2. execute the existing bytecode loop without semantic changes;
3. count commands executed in the slice;
4. classify the completion as normal, suspended, or failed/aborted;
5. read the monotonic clock at slice end;
6. record aggregate and per-category metrics.

Timing logic must not be duplicated across script callers.

### 3.2 Explicit category context

Script entry points assign a fixed enum category before entering or resuming the script engine. Category assignment must be explicit and must not be inferred from content strings.

Approved categories:

- `npc`
- `event`
- `timer`
- `item`
- `skill`
- `quest`
- `instance`
- `unknown`

If an entry point does not provide a valid category, the system records `unknown`.

The category context must survive only as long as needed to classify the current execution slice. It must not create unbounded dynamic state.

### 3.3 No dynamic classification

The implementation must not classify scripts from:

- script filename or path;
- NPC name;
- event label;
- script source text;
- map name;
- item, skill, quest, account, character, or player identifiers;
- command or function names.

## 4. Configuration

```text
RATHENA_SCRIPT_OBSERVABILITY=0
RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS=25
```

Rules:

- observability is disabled by default;
- the default slow threshold is 25 milliseconds;
- valid threshold range is 1–60000 milliseconds;
- values below or above the range are clamped;
- malformed values fall back to 25 milliseconds;
- configuration parsing must be deterministic and dependency-free.

### 4.1 Disabled-path contract

When disabled, execution must exit the observability path after one enable check.

The disabled path must perform no:

- clock read;
- heap allocation;
- lock acquisition;
- string formatting;
- file I/O;
- source inspection;
- category string generation.

## 5. Metrics

All metrics are exported through the existing Prometheus textfile writer introduced by prior observability milestones. This milestone must not introduce a second writer, timer, or metrics file.

### 5.1 Aggregate metrics

```text
rathena_script_execution_slices_total
rathena_script_execution_failures_total
rathena_script_slow_execution_slices_total
rathena_script_execution_duration_last_milliseconds
rathena_script_execution_duration_max_milliseconds
rathena_script_commands_total
rathena_script_commands_max_per_slice
```

### 5.2 Per-category metrics

The same bounded category set may be emitted as a `category` label where defined:

```text
rathena_script_execution_slices_total{category="npc"}
rathena_script_execution_failures_total{category="event"}
rathena_script_slow_execution_slices_total{category="timer"}
rathena_script_commands_total{category="item"}
rathena_script_execution_duration_max_milliseconds{category="instance"}
```

Only the approved fixed category values are permitted. No caller-controlled or content-derived label value may be emitted.

### 5.3 Counter behavior

- counters use saturating `uint64_t` arithmetic;
- maximum values never wrap;
- duration values are represented in whole milliseconds;
- a slice is slow when duration is greater than or equal to the configured threshold;
- metric rendering order is deterministic.

## 6. Failure Semantics

`rathena_script_execution_failures_total` records only execution errors or aborts that cause a slice to end abnormally.

Included examples:

- unknown bytecode command;
- command-count infinity-loop guard;
- goto-count infinity-loop guard;
- runtime error that forces the script state to end;
- explicit forced abort path inside the script engine.

Excluded examples:

- warning messages;
- deprecated command warnings;
- argument mismatch warnings;
- normal completion;
- sleep or suspension;
- dialog wait;
- rerun or resume;
- player disconnect unless the script engine records it as an execution failure;
- a slice exceeding the slow threshold without an execution error.

A slow slice and a failed slice are independent conditions and may both be counted when both are true.

## 7. Command Accounting

The command counter records the number of bytecode loop iterations or executed commands in the current execution slice, using one clearly documented definition consistently across implementation and tests.

Requirements:

- the counter is local to one slice;
- it resets on each invocation of `run_script_main()`;
- suspended and resumed slices are counted separately;
- command accounting must not alter existing `check_cmdcount` or `check_gotocount` behavior;
- existing infinity-loop protections remain authoritative.

## 8. Performance Model

When enabled, the expected hot-path cost per slice is limited to:

- two monotonic clock reads;
- one local command counter;
- one fixed enum category lookup;
- bounded fixed-storage metric updates;
- saturating integer operations.

The implementation must avoid:

- heap allocation in the execution hot path;
- dynamic labels;
- script source parsing or string inspection;
- SQL or network I/O;
- per-command clock reads;
- additional Prometheus flush scheduling;
- mutex use in the hot path unless repository concurrency analysis proves it necessary and the design is amended before implementation.

## 9. Privacy and Information Disclosure

The metrics output must not contain:

- script source;
- script filename or path;
- NPC name;
- event label;
- function or command name;
- source line number;
- map name;
- item, skill, or quest identifier;
- account, character, player, or runtime object identifier;
- script arguments, variables, or values;
- error message text;
- stack traces.

The bounded category label is the only script classification exported in V1.

## 10. Behavior Preservation

This milestone must not change:

- script bytecode execution order;
- script return values;
- script state transitions;
- sleep, suspend, resume, rerun, or timer behavior;
- infinity-loop thresholds or enforcement;
- gameplay logic;
- NPC, item, skill, quest, or instance behavior;
- database behavior;
- map-server scheduling semantics;
- server protocol behavior.

Observability failures must never cause gameplay or server execution to fail.

## 11. Testing Requirements

### 11.1 Pure/configuration tests

- enabled and disabled parsing;
- malformed value fallback;
- threshold clamp at 1 and 60000 milliseconds;
- approved category enum mapping;
- invalid category fallback to `unknown`;
- saturating counters at `UINT64_MAX`;
- deterministic rendering;
- privacy token scan.

### 11.2 Runtime tests

- normal execution slice;
- slow threshold boundaries at 24, 25, and 26 milliseconds using an injectable or deterministic clock abstraction;
- normal suspend and resume counted as separate slices;
- command counting and maximum commands per slice;
- command-count infinity-loop abort;
- goto-count infinity-loop abort;
- unknown-command abort where safely constructible in test code;
- a slice that is both slow and failed;
- disabled path does not read the clock;
- invalid or missing category records `unknown`;
- existing script results and state transitions remain unchanged.

### 11.3 Integration and CI

- existing observability writer includes script metrics when enabled;
- no script metrics are emitted when disabled;
- map-server run-once or equivalent smoke validation;
- Pre-Renewal and Renewal builds;
- GCC, Clang, MSVC, and CMake builds;
- packet-version matrix;
- NPC script and database validation;
- CodeQL analysis;
- `git diff --check` clean.

## 12. Operational Documentation

Add an operator document describing:

- how to enable the feature;
- the default 25 ms threshold;
- execution-slice semantics;
- why sleep and player wait time are excluded;
- category definitions;
- metric definitions;
- privacy guarantees;
- expected overhead;
- interpretation cautions, including that aggregate/category metrics do not identify an individual NPC or script file.

## 13. Out of Scope

A2.2-3 does not include:

- Top-N slow NPC or script identities;
- filename-level or event-label-level metrics;
- dynamic labels;
- stack traces;
- per-command latency;
- per-command profiler output;
- automatic termination based on elapsed time;
- script scheduler redesign;
- sampling profiler;
- admin dashboard work;
- alert thresholds or SLO policy;
- content optimization or script rewrites.

These may be considered in later milestones only after A3 baseline data demonstrates a need.

## 14. Acceptance Criteria

A2.2-3 is complete when:

1. the central `run_script_main()` hook records execution-slice latency and commands without semantic changes;
2. explicit bounded categories are supplied by relevant entry points with safe `unknown` fallback;
3. slow slices use a default threshold of 25 ms;
4. only execution errors/aborts increment failure metrics;
5. disabled mode has no clock read, allocation, lock, formatting, or I/O overhead beyond the enable guard;
6. output uses the existing Prometheus writer and contains no prohibited information;
7. pure, runtime, integration, build-matrix, and CodeQL checks pass;
8. operator documentation is complete;
9. the pull request contains no unresolved review threads or unaddressed security/performance findings.
