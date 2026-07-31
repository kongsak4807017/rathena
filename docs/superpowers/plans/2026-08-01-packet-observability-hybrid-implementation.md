# Packet Observability Hybrid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add disabled-by-default hybrid packet observability to `map-server`, combining low-cost transport totals with bounded packet-level receive, send, latency, invalid/unknown, and broadcast fan-out metrics.

**Architecture:** A pure header-only helper layer owns configuration parsing, bounded packet-ID admission, saturating counters, formatting, and Prometheus rendering. A runtime state module exposes allocation-free disabled-path hooks for transport, receive dispatch, send, and broadcast fan-out, while the existing core observability snapshot appends packet metrics and remains the only timer and writer.

**Tech Stack:** C++17, rAthena socket/session and `clif` packet paths, existing core observability textfile writer, GCC, Clang, MSVC, GitHub Actions, CodeQL.

## Global Constraints

- Branch: `feat/runtime-performance-instrumentation-v2`.
- Feature flag: `RATHENA_PACKET_OBSERVABILITY=0` by default.
- Slow threshold: `RATHENA_PACKET_OBSERVABILITY_SLOW_MS=25`, clamped to `[1, 60000]`.
- Packet-ID capacity: `RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS=512`, clamped to `[16, 4096]`.
- No packet payload, player name, account ID, character ID, file descriptor, IP address, token, chat text, SQL text, or personal data may be exported or logged.
- No protocol, packet layout, parser return value, validation order, disconnect policy, gameplay, balancing, SQL schema, persistence, or database write-path change.
- No new runtime dependency, HTTP endpoint, timer, writer, temporary-file scheme, or metrics output path.
- Packet labels must be bounded and lowercase hexadecimal.
- Observability failures must never interrupt packet processing.
- Disabled hooks must perform only a predictable enable check and must not allocate, format, read a clock, lock, scan a map, or perform file I/O.
- Use unsigned 64-bit saturating counters rather than wraparound.
- Full CI must pass for GCC, Clang, macOS Clang, MSVC, CMake, Pre-Renewal, Renewal, VIP, packet-version matrix, NPC/DB validation, observability tests, and CodeQL.

---

## File Structure

### New files

- `src/map/packet_observability_pure.hpp` — dependency-free configuration, bounded registry, saturating arithmetic, metric model, and rendering.
- `src/map/packet_observability.hpp` — public runtime hook declarations used by socket, parser, send, and core observability code.
- `src/map/packet_observability_internal.hpp` — runtime-only state structures and internal helpers.
- `src/map/packet_observability.cpp` — feature lifecycle, counters, bounded per-ID state, hook implementations, and snapshot rendering bridge.
- `tools/observability/tests/packet_observability_test.cpp` — dependency-free C++ unit tests.
- `docs/observability/PACKET_OBSERVABILITY_V1.md` — configuration, metrics, privacy, overhead, operation, and rollback.

### Existing files expected to change

- `src/common/socket.cpp` or the narrowest stable socket/session functions — aggregate transport receive/send totals only.
- `src/map/clif.cpp` — central client packet receive-dispatch timing/classification and central send/fan-out hooks.
- `src/map/core_observability.cpp` — append packet metrics to the existing snapshot.
- `src/map/map.cpp` — packet observability lifecycle only if runtime state cannot safely initialize lazily; prefer no extra lifecycle call when static initialization is sufficient.
- `src/map/map-server.vcxproj` — list new source/header files.
- `src/map/map-server-generator.vcxproj` — list new source/header files if required by current project conventions.
- `.github/workflows/observability-tests.yml` — compile and run packet tests on GCC, Clang, and MSVC.

---

### Task 1: Define pure configuration and arithmetic behavior with failing tests

**Files:**
- Create: `tools/observability/tests/packet_observability_test.cpp`
- Create: `src/map/packet_observability_pure.hpp`

**Interfaces:**
- Produces:
  - `bool packet_observability_parse_enabled(const char* value)`
  - `uint32_t packet_observability_parse_slow_ms(const char* value, bool& used_fallback)`
  - `size_t packet_observability_parse_capacity(const char* value, bool& used_fallback)`
  - `uint64_t packet_observability_saturating_add(uint64_t current, uint64_t increment)`
  - `bool packet_observability_is_slow(uint64_t duration_ms, uint32_t threshold_ms)`
  - `std::string packet_observability_format_packet_id(uint16_t packet_id)`

- [ ] **Step 1: Write failing tests for enable parsing**

```cpp
assert(packet_observability_parse_enabled("1"));
assert(packet_observability_parse_enabled("TRUE"));
assert(packet_observability_parse_enabled("On"));
assert(packet_observability_parse_enabled("yes"));
assert(!packet_observability_parse_enabled(nullptr));
assert(!packet_observability_parse_enabled("0"));
assert(!packet_observability_parse_enabled("enabled"));
```

- [ ] **Step 2: Write failing tests for threshold and capacity parsing**

```cpp
bool fallback = false;
assert(packet_observability_parse_slow_ms("25", fallback) == 25 && !fallback);
assert(packet_observability_parse_slow_ms("0", fallback) == 1 && !fallback);
assert(packet_observability_parse_slow_ms("999999", fallback) == 60000 && !fallback);
assert(packet_observability_parse_slow_ms("bad", fallback) == 25 && fallback);
assert(packet_observability_parse_capacity("8", fallback) == 16 && !fallback);
assert(packet_observability_parse_capacity("512", fallback) == 512 && !fallback);
assert(packet_observability_parse_capacity("99999", fallback) == 4096 && !fallback);
assert(packet_observability_parse_capacity("bad", fallback) == 512 && fallback);
```

- [ ] **Step 3: Write failing tests for saturating arithmetic, slow classification, and formatting**

```cpp
assert(packet_observability_saturating_add(5, 7) == 12);
assert(packet_observability_saturating_add(UINT64_MAX - 1, 5) == UINT64_MAX);
assert(!packet_observability_is_slow(24, 25));
assert(packet_observability_is_slow(25, 25));
assert(packet_observability_format_packet_id(0x64) == "0x0064");
assert(packet_observability_format_packet_id(0xffff) == "0xffff");
```

- [ ] **Step 4: Run the test and verify it fails because the interfaces are missing**

Run:

```bash
g++ -std=c++17 -Wall -Wextra -Werror -Isrc/map tools/observability/tests/packet_observability_test.cpp -o packet_observability_test
```

Expected: compile failure for undefined packet observability helpers.

- [ ] **Step 5: Implement the minimal pure helpers**

Use ASCII case-insensitive comparisons without locale dependence. Parse decimal values with complete-string validation, clamp valid numeric values, and set `used_fallback=true` only for null, empty, malformed, signed, or overflow input.

- [ ] **Step 6: Run GCC, Clang, and MSVC unit tests**

```bash
g++ -std=c++17 -Wall -Wextra -Werror -Isrc/map tools/observability/tests/packet_observability_test.cpp -o packet_observability_test && ./packet_observability_test
clang++ -std=c++17 -Wall -Wextra -Werror -Isrc/map tools/observability/tests/packet_observability_test.cpp -o packet_observability_test_clang && ./packet_observability_test_clang
cl /std:c++17 /EHsc /W4 /WX /Isrc\map tools\observability\tests\packet_observability_test.cpp && packet_observability_test.exe
```

Expected: `all packet observability tests passed` and exit code `0`.

- [ ] **Step 7: Commit**

```bash
git add src/map/packet_observability_pure.hpp tools/observability/tests/packet_observability_test.cpp
git commit -m "test: define packet observability configuration behavior"
```

---

### Task 2: Add bounded packet-ID registry and deterministic rendering model

**Files:**
- Modify: `src/map/packet_observability_pure.hpp`
- Modify: `tools/observability/tests/packet_observability_test.cpp`

**Interfaces:**
- Produces:
  - `struct packet_observability_packet_metrics`
  - `struct packet_observability_snapshot`
  - `class packet_observability_bounded_registry`
  - `packet_observability_packet_metrics* admit(uint16_t packet_id)`
  - `std::string packet_observability_render_prometheus(const packet_observability_snapshot& snapshot)`

- [ ] **Step 1: Add failing bounded-admission tests**

Create a registry with capacity `2`; admit `0x0064` and `0x0085`; verify repeated admission returns the same entry; verify `0x0090` returns `nullptr`; increment overflow once outside the registry.

- [ ] **Step 2: Add failing rendering tests**

Populate aggregate and per-ID values and assert exact lines such as:

```text
rathena_packet_received_packets_total 3
rathena_packet_received_total{packet="0x0064"} 2
rathena_packet_processing_duration_max_milliseconds{packet="0x0064"} 7
```

Also assert deterministic packet-ID ordering and terminal newline.

- [ ] **Step 3: Add privacy regression input**

Insert marker strings such as `PRIVATE_PLAYER_NAME`, `192.0.2.15`, and `secret-chat-text` into test-local variables that are never part of the metric model, render output, and assert none appears.

- [ ] **Step 4: Run tests and verify failure**

Expected: missing registry/model/rendering symbols.

- [ ] **Step 5: Implement the bounded model**

Use pre-sized `std::vector` storage with explicit occupancy and linear lookup for V1 capacity bounded to 4096. Do not use an unbounded associative container. Preserve insertion-independent deterministic rendering by sorting only a temporary list during snapshot rendering, never in packet hooks.

- [ ] **Step 6: Make all counter updates saturating**

Every aggregate and per-ID field update in pure helper methods must call `packet_observability_saturating_add`.

- [ ] **Step 7: Run all pure tests on three compilers**

Expected: pass with warnings treated as errors.

- [ ] **Step 8: Commit**

```bash
git add src/map/packet_observability_pure.hpp tools/observability/tests/packet_observability_test.cpp
git commit -m "feat: add bounded packet observability model"
```

---

### Task 3: Add runtime configuration and state without hot-path integration

**Files:**
- Create: `src/map/packet_observability.hpp`
- Create: `src/map/packet_observability_internal.hpp`
- Create: `src/map/packet_observability.cpp`
- Modify: `src/map/map-server.vcxproj`
- Modify: `src/map/map-server-generator.vcxproj`

**Interfaces:**
- Produces:
  - `void packet_observability_init()`
  - `void packet_observability_final()`
  - `bool packet_observability_enabled()`
  - `void packet_observability_record_transport_receive(size_t bytes)`
  - `void packet_observability_record_transport_send(size_t bytes)`
  - `void packet_observability_record_receive(uint16_t packet_id, size_t bytes)`
  - `void packet_observability_record_invalid()`
  - `void packet_observability_record_unknown()`
  - `void packet_observability_record_processing(uint16_t packet_id, uint64_t duration_ms)`
  - `void packet_observability_record_send(uint16_t packet_id, size_t bytes)`
  - `void packet_observability_record_broadcast(size_t recipients)`
  - `std::string packet_observability_render_snapshot()`

- [ ] **Step 1: Add a runtime-state test seam**

Under a test-only macro, expose reset and snapshot-copy helpers without exposing them in production builds:

```cpp
#ifdef RATHENA_PACKET_OBSERVABILITY_TESTING
void packet_observability_test_reset(bool enabled, uint32_t slow_ms, size_t capacity);
packet_observability_snapshot packet_observability_test_snapshot();
#endif
```

- [ ] **Step 2: Add failing runtime-state tests**

Compile `packet_observability.cpp` with `RATHENA_PACKET_OBSERVABILITY_TESTING`; verify disabled hooks leave all counters zero; enabled hooks update aggregate and per-ID counters; capacity overflow increments exactly once per rejected new ID event.

- [ ] **Step 3: Implement initialization**

Read the three environment variables once. Emit at most one warning per malformed setting. Allocate bounded registry storage only when enabled. Make `init` and `final` idempotent.

- [ ] **Step 4: Implement disabled-path guards**

Each public hook must begin with:

```cpp
if (!packet_observability_enabled()) {
    return;
}
```

No helper call may occur before this guard.

- [ ] **Step 5: Implement aggregate and per-ID state updates**

All increments and last/max updates are non-throwing. Catch no exceptions in packet hooks by ensuring all capacity allocation occurs during initialization and all later operations use existing storage.

- [ ] **Step 6: Run pure and runtime-state tests**

Expected: pass under GCC, Clang, and MSVC.

- [ ] **Step 7: Build map-server in at least one local configuration**

Expected: compile and link cleanly with no hook call sites yet.

- [ ] **Step 8: Commit**

```bash
git add src/map/packet_observability.hpp src/map/packet_observability_internal.hpp src/map/packet_observability.cpp src/map/map-server.vcxproj src/map/map-server-generator.vcxproj tools/observability/tests/packet_observability_test.cpp
git commit -m "feat: add bounded packet observability runtime state"
```

---

### Task 4: Integrate lifecycle and existing core observability writer

**Files:**
- Modify: `src/map/core_observability.cpp`
- Modify: `src/map/map.cpp` only if explicit lifecycle is required
- Modify: `tools/observability/tests/packet_observability_test.cpp`

**Interfaces:**
- Consumes: `packet_observability_init`, `packet_observability_final`, `packet_observability_render_snapshot`.
- Produces: packet metrics appended to the existing atomic `.prom` snapshot.

- [ ] **Step 1: Add a failing combined-render test**

Create a packet snapshot and verify the packet renderer can be appended after core metrics without missing or duplicate terminal newlines.

- [ ] **Step 2: Initialize packet state before the first possible hook**

Prefer invoking `packet_observability_init()` from `core_observability_init()` because the existing map lifecycle already calls it. If packet hooks can execute before core init, place one explicit packet init call at the earliest stable map-server initialization boundary and document why.

- [ ] **Step 3: Finalize packet state after hooks stop**

Call `packet_observability_final()` during map-server shutdown without changing existing shutdown ordering.

- [ ] **Step 4: Append metrics only when both systems are enabled**

In core snapshot rendering:

```cpp
if (packet_observability_enabled()) {
    output += packet_observability_render_snapshot();
}
```

Packet observability enabled with core observability disabled must accumulate counters but create no writer or timer.

- [ ] **Step 5: Run unit tests and disabled `--run-once` smoke test**

Expected: no packet metric lines and no packet startup log when packet observability is disabled.

- [ ] **Step 6: Commit**

```bash
git add src/map/core_observability.cpp src/map/map.cpp tools/observability/tests/packet_observability_test.cpp
git commit -m "feat: export packet metrics through core observability"
```

---

### Task 5: Instrument aggregate transport receive and send totals

**Files:**
- Modify: `src/common/socket.cpp` or the exact central session receive/send implementation found during code inspection
- Modify: `src/map/packet_observability.hpp` only if C/C++ boundary wrappers are needed

**Interfaces:**
- Consumes: `packet_observability_record_transport_receive(size_t bytes)`, `packet_observability_record_transport_send(size_t bytes)`.

- [ ] **Step 1: Identify the narrowest stable byte-acceptance points**

Select the functions where received bytes have been successfully read into a session buffer and sent bytes have been accepted by the socket send path. Do not count requested bytes before the underlying operation result is known.

- [ ] **Step 2: Add transport receive hook after successful read accounting**

Pass only the positive byte count returned by the underlying receive operation.

- [ ] **Step 3: Add transport send hook after successful send accounting**

Pass only the positive byte count accepted by the underlying send operation. Do not count retries or failed writes as successful bytes.

- [ ] **Step 4: Verify no socket behavior changes**

Review the diff to confirm return values, buffer offsets, retry branches, error branches, and close behavior are unchanged.

- [ ] **Step 5: Build login, char, and map servers**

Expected: all build cleanly. If common socket hooks affect non-map processes, the packet hook implementation must remain a no-op outside map-server or be compiled behind the existing map target boundary; do not introduce unresolved symbols.

- [ ] **Step 6: Runtime smoke test**

With both observability flags enabled, establish a client connection and verify transport counters increase. With packet observability disabled, verify no packet metric lines appear.

- [ ] **Step 7: Commit**

```bash
git add src/common/socket.cpp src/map/packet_observability.hpp
git commit -m "feat: instrument packet transport totals"
```

---

### Task 6: Instrument central map receive dispatch and classification

**Files:**
- Modify: `src/map/clif.cpp`
- Modify: `src/map/packet_observability.hpp`

**Interfaces:**
- Consumes: receive, invalid, unknown, and processing-duration hooks.

- [ ] **Step 1: Locate the single central client dispatch boundary**

Find the function that reads the packet ID and length, validates the packet database entry, and invokes the registered parser. Record exact branches for malformed length and unknown packet ID before editing.

- [ ] **Step 2: Record valid receive count and bytes once**

After packet ID and final consumed length are known valid, call:

```cpp
packet_observability_record_receive(packet_id, packet_length);
```

Do not count the same packet in more than one receive hook.

- [ ] **Step 3: Record invalid and unknown classifications**

Call `packet_observability_record_invalid()` only for malformed or validation-failed packets. Call `packet_observability_record_unknown()` only when no parser/packet definition exists. Preserve all existing logs and disconnect behavior.

- [ ] **Step 4: Time only parser dispatch**

Read the monotonic tick immediately before the parser call and immediately after it returns. Convert to non-negative milliseconds and call:

```cpp
packet_observability_record_processing(packet_id, duration_ms);
```

Perform no clock read when disabled.

- [ ] **Step 5: Verify parser semantics by diff inspection**

The original parser call must occur exactly once, with the same arguments, under the same conditions. Packet consumption and return behavior must be identical.

- [ ] **Step 6: Add focused runtime verification**

Generate known valid client activity and confirm per-ID receive and timing metrics. Send one controlled unknown or malformed packet only in an isolated test environment and verify the correct single counter changes.

- [ ] **Step 7: Commit**

```bash
git add src/map/clif.cpp src/map/packet_observability.hpp
git commit -m "feat: instrument map packet receive dispatch"
```

---

### Task 7: Instrument central send and broadcast fan-out

**Files:**
- Modify: `src/map/clif.cpp`
- Modify: `src/map/packet_observability.hpp`

**Interfaces:**
- Consumes: `packet_observability_record_send`, `packet_observability_record_broadcast`.

- [ ] **Step 1: Locate final packet ID and length extraction in `clif_send`**

Read packet ID only when `len >= 2` and the buffer is non-null. Use byte-safe extraction consistent with existing packet code; do not assume alignment.

- [ ] **Step 2: Record one send call**

Call `packet_observability_record_send(packet_id, len)` once per central `clif_send` invocation that represents a valid client packet. Do not count per-recipient copies here unless the specification of the metric is explicitly changed; this metric measures logical send calls and bytes passed to `clif_send`.

- [ ] **Step 3: Count actual resolved recipients**

At the narrowest fan-out point, increment a local `size_t recipients` only when a recipient is actually selected for send. After selection completes, call:

```cpp
packet_observability_record_broadcast(recipients);
```

Only call this for broadcast-style targets, not `SELF` or a single explicit client.

- [ ] **Step 4: Preserve recipient filtering**

Do not reorder or alter visibility, map, party, guild, battleground, chat, area, or exclusion filters.

- [ ] **Step 5: Runtime verification**

Trigger a local area broadcast with two or more clients and verify calls, total recipients, last recipients, and max recipients. Confirm private single-client sends do not increment broadcast metrics.

- [ ] **Step 6: Commit**

```bash
git add src/map/clif.cpp src/map/packet_observability.hpp
git commit -m "feat: instrument packet send and broadcast fanout"
```

---

### Task 8: Add CI coverage and operational documentation

**Files:**
- Modify: `.github/workflows/observability-tests.yml`
- Create: `docs/observability/PACKET_OBSERVABILITY_V1.md`

**Interfaces:**
- Produces: repeatable CI and operator-facing configuration/rollback instructions.

- [ ] **Step 1: Add GCC packet unit-test job step**

Compile with `-std=c++17 -Wall -Wextra -Werror -Isrc/map` and execute the binary.

- [ ] **Step 2: Add Clang packet unit-test job step**

Use the same warnings-as-errors policy.

- [ ] **Step 3: Add MSVC packet unit-test job step**

Use `/std:c++17 /EHsc /W4 /WX /Isrc\map` and execute with PowerShell-compatible syntax.

- [ ] **Step 4: Write the operations document**

Document exact environment variables, dependency on core observability for textfile export, every aggregate and per-ID metric, cardinality cap, overflow behavior, privacy exclusions, disabled/enabled overhead, runtime verification, failure handling, and rollback.

- [ ] **Step 5: Add examples**

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_PACKET_OBSERVABILITY=1
RATHENA_PACKET_OBSERVABILITY_SLOW_MS=25
RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS=512
```

Explain that packet counters can accumulate with core observability disabled but no `.prom` output is written.

- [ ] **Step 6: Run `git diff --check` and unit tests**

Expected: no whitespace errors and all tests pass.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/observability-tests.yml docs/observability/PACKET_OBSERVABILITY_V1.md
git commit -m "ci: test and document packet observability"
```

---

### Task 9: Full build, security, runtime, and privacy verification

**Files:**
- Modify only files required by verified failures.

**Interfaces:**
- Produces: release evidence for the draft PR.

- [ ] **Step 1: Run local pure tests**

Run GCC, Clang, and MSVC commands from Task 1.

- [ ] **Step 2: Run `git diff --check`**

Expected: no output and exit code `0`.

- [ ] **Step 3: Build map-server configurations locally where available**

At minimum build one Pre-Renewal and one Renewal configuration, with warnings treated as errors for changed code.

- [ ] **Step 4: Disabled runtime verification**

Unset or set `RATHENA_PACKET_OBSERVABILITY=0`, run `map-server --run-once`, and verify:

- exit code `0`;
- no packet observability startup log;
- no packet metric lines;
- no extra directory or `.tmp` file;
- no behavior change.

- [ ] **Step 5: Enabled runtime verification**

Enable both core and packet observability. Generate valid client traffic and verify totals, per-ID metrics, timing, and clean shutdown. Confirm `rathena_packet_id_overflow_total=0` under normal capacity.

- [ ] **Step 6: Capacity verification**

Use a small test capacity of `16` in an isolated environment, exercise more than 16 distinct packet IDs, and verify the label count stays bounded while overflow increases.

- [ ] **Step 7: Privacy scan**

Search the generated `.prom` file for known player name, account/character ID, IP, chat marker, and payload marker used during testing. Expected: no matches.

- [ ] **Step 8: Malformed and unknown packet verification**

In an isolated test client, verify one malformed and one unknown packet affect only the intended counters and retain existing server handling.

- [ ] **Step 9: Push branch and wait for all GitHub workflows**

Every workflow must complete with `success`. Investigate any failure; do not retry blindly without understanding the cause.

- [ ] **Step 10: Review CodeQL and review threads**

Expected: zero new open CodeQL alerts and no unresolved review thread.

- [ ] **Step 11: Commit any verification-only fixes**

Use focused commit messages describing the actual fix. Re-run all affected tests after each fix.

---

### Task 10: Open the A2.2-1 draft pull request

**Files:**
- No source change required unless PR review finds an issue.

**Interfaces:**
- Produces: draft PR from `feat/runtime-performance-instrumentation-v2` to `master`.

- [ ] **Step 1: Confirm branch relationship**

```bash
git fetch origin
git merge-base --is-ancestor origin/master HEAD
```

Expected: exit code `0`.

- [ ] **Step 2: Confirm changed-file scope**

Expected changes only in packet observability files, selected socket/clif/core hooks, project files, CI, tests, docs, design, and plan. No SQL, DB schema, script, gameplay, or packet-definition changes.

- [ ] **Step 3: Open a draft PR**

Title:

```text
feat: add hybrid packet performance instrumentation
```

Base: `master`  
Head: `feat/runtime-performance-instrumentation-v2`

- [ ] **Step 4: Include evidence in PR body**

Document configuration, metrics, exact hot-path touch points, disabled-path guarantees, privacy exclusions, bounded labels, local tests, full workflow matrix, runtime disabled/enabled results, malformed/unknown test results, CodeQL state, database safety, and rollback.

- [ ] **Step 5: Keep PR draft until final review**

Do not mark ready until CI is fully green, runtime evidence is posted, CodeQL has no open alert, and all review threads are resolved.

---

## Plan Self-Review

- Spec coverage: configuration, hybrid hooks, bounded labels, saturating counters, writer integration, privacy, performance, error handling, tests, CI, runtime verification, documentation, and draft PR are each assigned to a task.
- Placeholder scan: no `TBD`, `TODO`, unspecified validation, or unnamed test step remains.
- Type consistency: all runtime hook signatures and pure helper names are defined once and reused consistently.
- Scope check: SQL instrumentation and slow-script instrumentation remain explicitly excluded and require separate plans/PRs.
