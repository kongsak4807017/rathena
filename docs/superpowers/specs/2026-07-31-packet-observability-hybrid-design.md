# Packet Observability Hybrid Design

Date: 2026-07-31
Status: Approved design
Branch: `feat/runtime-performance-instrumentation-v2`
Milestone: A2.2-1

## Objective

Add disabled-by-default packet performance instrumentation to `map-server` so operators can identify packet floods, broadcast pressure, malformed traffic, and expensive packet-processing paths without changing gameplay, protocol behavior, persistence, or database schema.

## Scope

The implementation uses a hybrid model:

1. low-cost transport totals at the socket/session boundary; and
2. map-server packet detail at `clif` receive/send boundaries.

The feature records counts, byte totals, bounded packet-ID detail, processing latency, invalid/unknown packet events, and broadcast fan-out. It never records packet payloads or player-identifying values.

## Non-goals

This milestone does not include:

- SQL latency instrumentation;
- slow-script instrumentation;
- packet payload logging;
- per-player, per-account, per-character, or per-IP metrics;
- protocol, packet structure, disconnect-policy, gameplay, or balancing changes;
- network metrics exposition or a new HTTP endpoint;
- database migration or schema changes.

## Configuration

Instrumentation is off unless explicitly enabled.

```text
RATHENA_PACKET_OBSERVABILITY=0
RATHENA_PACKET_OBSERVABILITY_SLOW_MS=25
RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS=512
```

Rules:

- accepted enable values match core observability: `1`, `true`, `on`, `yes`, case-insensitive;
- slow threshold is clamped to `[1, 60000]` milliseconds;
- packet-ID capacity is clamped to `[16, 4096]`;
- malformed values fall back to defaults and emit at most one startup warning per setting;
- when disabled, no packet-ID registry, timing, extra map scans, or extra file output is created.

## Architecture

### 1. Pure helper layer

Proposed file:

`src/map/packet_observability_pure.hpp`

Responsibilities:

- environment parsing and clamps;
- packet ID formatting as lowercase hexadecimal;
- bounded packet-ID admission;
- latency and slow-event calculations;
- Prometheus rendering for packet metrics;
- deterministic tests without a running server.

This layer must not depend on map-server runtime state.

### 2. Runtime state layer

Proposed files:

- `src/map/packet_observability.hpp`
- `src/map/packet_observability_internal.hpp`
- `src/map/packet_observability.cpp`

Runtime state contains monotonic counters and last/max gauges only. No histograms and no unbounded labels are introduced in V1.

State includes:

- transport receive/send packets and bytes;
- client packet receive count and bytes;
- client packet send count and bytes;
- invalid packet count;
- unknown packet count;
- packet-processing duration last/max;
- slow packet count;
- broadcast call count;
- broadcast recipient total and last/max recipient count;
- bounded per-packet-ID receive/send count and bytes;
- bounded per-packet-ID processing last/max duration and slow count.

### 3. Transport hooks

The socket/session boundary records aggregate packet and byte totals. The hook must:

- execute only after the feature-enable check;
- use integer additions only;
- avoid allocations, string formatting, clocks, and locks in the common disabled path;
- remain protocol-neutral;
- avoid duplicate accounting with map-level hooks by using separate metric names for transport and client packet layers.

### 4. Map receive hook

The map packet parser records:

- packet ID;
- packet length;
- valid, invalid, or unknown classification;
- processing duration around dispatch;
- slow-event count when duration is at or above the configured threshold.

Timing must wrap the smallest stable packet-dispatch boundary available. It must not change parser return values, validation order, disconnect behavior, or packet consumption.

### 5. Map send hook

The `clif_send` boundary records:

- packet ID when safely available;
- payload length;
- send call count;
- resolved recipient count for broadcast-style targets.

Instrumentation must observe final fan-out without changing recipient selection or sending behavior.

### 6. Metrics integration

Packet metrics are appended to the existing core observability Prometheus snapshot when both core observability and packet observability are enabled.

If packet observability is enabled while core observability is disabled, packet counters may accumulate but no separate writer or timer is created. Documentation must state that textfile export requires core observability to be enabled.

This avoids a second timer, a second atomic writer, competing temporary files, and duplicate lifecycle management.

## Metrics

Aggregate metrics:

```text
rathena_packet_transport_received_packets_total
rathena_packet_transport_received_bytes_total
rathena_packet_transport_sent_packets_total
rathena_packet_transport_sent_bytes_total
rathena_packet_received_packets_total
rathena_packet_received_bytes_total
rathena_packet_sent_packets_total
rathena_packet_sent_bytes_total
rathena_packet_invalid_total
rathena_packet_unknown_total
rathena_packet_processing_duration_last_milliseconds
rathena_packet_processing_duration_max_milliseconds
rathena_packet_slow_total
rathena_packet_broadcast_calls_total
rathena_packet_broadcast_recipients_total
rathena_packet_broadcast_recipients_last
rathena_packet_broadcast_recipients_max
rathena_packet_id_overflow_total
```

Bounded packet-ID metrics:

```text
rathena_packet_received_total{packet="0x...."}
rathena_packet_received_bytes_total{packet="0x...."}
rathena_packet_sent_total{packet="0x...."}
rathena_packet_sent_bytes_total{packet="0x...."}
rathena_packet_processing_duration_last_milliseconds{packet="0x...."}
rathena_packet_processing_duration_max_milliseconds{packet="0x...."}
rathena_packet_slow_total{packet="0x...."}
```

Packet IDs beyond the configured capacity are aggregated into overflow counters and are not added as new labels.

## Privacy and security

The instrumentation must not export or log:

- packet payloads;
- player names;
- account IDs;
- character IDs;
- session file descriptors;
- IP addresses;
- authentication tokens;
- chat text;
- SQL statements or data.

Packet ID and aggregate size/timing values are the only packet-derived values exported.

All counters must be integer-safe for long-running servers. Overflow behavior must be defined and tested; unsigned 64-bit saturating increments are preferred over wraparound for observability counters.

## Performance requirements

Disabled path:

- one predictable enable check at each hook;
- no clock read;
- no allocation;
- no map lookup;
- no formatting;
- no file I/O.

Enabled path:

- aggregate counters use constant-time operations;
- per-ID storage is pre-sized or bounded;
- packet ID lookup must not grow without limit;
- clock reads occur only around receive dispatch when timing is enabled;
- rendering occurs only during the existing observability snapshot;
- packet payload bytes are never copied for instrumentation.

## Error handling

Instrumentation failures must never interrupt packet processing.

- invalid configuration falls back safely;
- packet-ID capacity exhaustion increments `rathena_packet_id_overflow_total`;
- rendering failures follow existing core observability write-error behavior;
- lifecycle calls are idempotent;
- shutdown clears instrumentation state without changing socket or map shutdown order.

## Testing

### Pure unit tests

Add `tools/observability/tests/packet_observability_test.cpp` covering:

- enable parsing;
- slow threshold parsing and clamp;
- packet-ID capacity parsing and clamp;
- packet ID hexadecimal formatting;
- bounded admission and overflow;
- saturating counter behavior;
- slow-event classification;
- aggregate rendering;
- bounded packet-ID rendering;
- empty state rendering;
- proof that rendered output contains no supplied payload or identity strings.

### Build coverage

Run tests with:

- GCC `-Wall -Wextra -Werror`;
- Clang `-Wall -Wextra -Werror`;
- MSVC `/W4 /WX`;
- Pre-Renewal and Renewal server builds;
- existing packet-version matrix;
- CodeQL.

### Runtime verification

Disabled mode:

- `map-server --run-once` exits successfully;
- no packet instrumentation startup log;
- no additional metrics are rendered;
- no change to packet behavior.

Enabled mode:

- valid client activity increments receive/send packet and byte totals;
- per-ID metrics appear within the configured capacity;
- broadcast activity updates recipient metrics;
- malformed or unknown test traffic updates only the correct counters;
- no payload or player identity appears in output;
- shutdown is clean;
- no additional `.tmp` files remain.

## Expected existing-code touch points

The implementation should minimize edits to existing hot-path files. Expected touch points are:

- common socket/session receive and send boundaries for aggregate totals;
- the central map packet dispatch boundary for receive detail and duration;
- `clif_send` or the narrowest stable central fan-out function for send and recipient metrics;
- existing core observability rendering and lifecycle integration;
- MSVC project file lists;
- observability CI workflow paths.

Unrelated refactoring is prohibited.

## Commit strategy

Recommended commits:

1. `test: define packet observability behavior`
2. `feat: add packet observability pure helpers`
3. `feat: add bounded packet observability state`
4. `feat: instrument packet receive and transport totals`
5. `feat: instrument packet send and broadcast fanout`
6. `feat: export packet observability metrics`
7. `ci: run packet observability tests`
8. `docs: document packet observability operation`

## Definition of done

A2.2-1 is complete when:

- all approved metrics are implemented;
- disabled mode has no measurable functional effect;
- packet labels are bounded;
- no payload or identity data is exported;
- no protocol, gameplay, database, or persistence behavior changes;
- pure tests and full build matrix pass;
- CodeQL has no new open alert;
- runtime disabled and enabled verification pass;
- a draft PR targets `master` from `feat/runtime-performance-instrumentation-v2`;
- all review threads are resolved before readiness or merge.
