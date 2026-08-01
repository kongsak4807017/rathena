# Script Observability V1

This document describes the bounded, opt-in script execution metrics added to
the map-server.  The instrumentation is designed to help operators detect slow
or failing NPC/item/skill/event scripts without exposing any script source,
NPC names, map names, item IDs, or player identifiers.

## Enabling the feature

Set the environment variable before starting the map-server:

```bash
RATHENA_SCRIPT_OBSERVABILITY=1
```

Optionally change the slow-slice threshold (default 25 ms):

```bash
RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS=50
```

Valid range is `1` to `60000`.  Invalid values are rejected with a single
warning and fall back to the default.

When disabled, which is the default, the instrumentation adds only one boolean
check per script slice and performs no clock reads, no counting, and no output.

## Categories

Every script slice is recorded with one of the following fixed label values.
These are the only values that ever appear in the `category` label.

| Label       | Meaning                                                            |
|-------------|--------------------------------------------------------------------|
| `npc`       | Scripts started by direct NPC interaction or generic NPC triggers. |
| `event`     | Named server events such as `OnInit`, `OnHour00`, etc.             |
| `timer`     | Scheduled/timer callbacks and sleep/dialog resumes.                |
| `item`      | Item use, equip, unequip, and autobonus scripts.                   |
| `skill`     | Scripts triggered by skill execution.                              |
| `quest`     | Quest-triggered scripts.                                           |
| `instance`  | Instance lifecycle scripts.                                        |
| `unknown`   | Internal or ambiguous call sites and any unclassified entry point. |

Call sites that cannot be assigned a clear category are recorded as `unknown`.
This is intentional: the category set is bounded and must not be inferred from
script source text, filenames, NPC names, map names, or IDs.

## Metrics

Script metrics are appended to the existing Prometheus textfile written by
`core_observability`.  They are only emitted when both core observability and
script observability are enabled.

```text
# HELP rathena_script_execution_slices_total Total number of executed script slices.
# TYPE rathena_script_execution_slices_total counter
rathena_script_execution_slices_total 1234

# HELP rathena_script_execution_failures_total Total number of script slices that terminated abnormally.
# TYPE rathena_script_execution_failures_total counter
rathena_script_execution_failures_total 7

# HELP rathena_script_slow_execution_slices_total Total number of script slices exceeding the slow threshold.
# TYPE rathena_script_slow_execution_slices_total counter
rathena_script_slow_execution_slices_total 12

# HELP rathena_script_execution_duration_last_ms Duration of the last script slice in milliseconds.
# TYPE rathena_script_execution_duration_last_ms gauge
rathena_script_execution_duration_last_ms 14

# HELP rathena_script_execution_duration_max_ms Maximum observed script slice duration in milliseconds.
# TYPE rathena_script_execution_duration_max_ms gauge
rathena_script_execution_duration_max_ms 312

# HELP rathena_script_commands_total Total number of bytecode commands executed.
# TYPE rathena_script_commands_total counter
rathena_script_commands_total 987654

# HELP rathena_script_commands_max_per_slice Maximum number of bytecode commands observed in a single slice.
# TYPE rathena_script_commands_max_per_slice gauge
rathena_script_commands_max_per_slice 12345
```

The same metrics are then emitted per category with the `category` label, in
the fixed order `npc`, `event`, `timer`, `item`, `skill`, `quest`, `instance`,
`unknown`.  Aggregate (unlabelled) metrics appear first.

## What counts as a failure

A slice is counted as a failure only when it terminates abnormally:

* unknown bytecode command
* command-count guard (`check_cmdcount` infinity-loop protection)
* goto-count guard (`check_gotocount` infinity-loop protection)
* explicit runtime abort that forces `END`

Normal completion, sleep, dialog suspension, warnings, and slow-but-successful
execution do **not** count as failures.

## Measurement boundary

One invocation of `run_script_main()` equals one slice.  If a script sleeps or
opens a dialog, `run_script_main()` returns and the timer/sleep resume starts a
new slice when execution continues.  This means long real-time scripts do not
artificially inflate single-slice durations.

## Privacy

No script source, filenames, NPC or event names, map names, player IDs,
variables, or error text are exported.  The only dynamic values in the metrics
are counters, durations, and the fixed `category` label.

## Runtime cost

When disabled: one `bool` check per slice.

When enabled: two monotonic clock reads per slice, one local command counter
increment per bytecode iteration, and a small fixed-size update to bounded
counters.  There is no per-slice memory allocation and no unbounded cardinality.
