# Packet Observability V1 (map-server instrumentation)

In-process packet instrumentation for `map-server`, introduced as work
package A3 on top of the A2 core observability instrumentation.

## Overview

Packet observability is a disabled-by-default hybrid instrumentation layer
that records coarse packet volume, timing and broadcast metrics without
changing the packet protocol or database schema. It sits alongside the
existing core observability writer: packet counters accumulate in memory
whenever `RATHENA_PACKET_OBSERVABILITY=1`, but the `.prom` textfile is only
produced when core observability is also enabled (`RATHENA_CORE_OBSERVABILITY=1`).

## Configuration

Configuration is via environment variables only:

| Variable | Default | Description |
| --- | --- | --- |
| `RATHENA_PACKET_OBSERVABILITY` | `0` (off) | `1`, `true`, `on`, `yes` enable; everything else keeps it off |
| `RATHENA_PACKET_OBSERVABILITY_SLOW_MS` | `25` | Threshold in ms for counting a packet as slow. Valid values are clamped to `[1, 60000]`; missing, empty, malformed, signed or overflow input falls back to the default with a single startup warning |
| `RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS` | `512` | Maximum number of distinct packet IDs tracked individually. Valid values are clamped to `[16, 4096]`; missing, empty, malformed, signed or overflow input falls back to the default with a single startup warning |

The bounded registry is allocated only when packet observability is enabled,
so disabled servers pay no memory cost for the per-ID storage.

Core observability must be enabled for the `.prom` textfile export to be
written:

```text
RATHENA_CORE_OBSERVABILITY=1
RATHENA_PACKET_OBSERVABILITY=1
RATHENA_PACKET_OBSERVABILITY_SLOW_MS=25
RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS=512
```

When `RATHENA_CORE_OBSERVABILITY=0`, packet counters still accumulate in
memory and will appear in the next snapshot if core export is enabled later
within the same process lifetime.

## Example

PowerShell:

```powershell
$env:RATHENA_CORE_OBSERVABILITY = "1"
$env:RATHENA_PACKET_OBSERVABILITY = "1"
$env:RATHENA_PACKET_OBSERVABILITY_SLOW_MS = "25"
$env:RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS = "512"
.\map-server.exe
```

Bash:

```bash
export RATHENA_CORE_OBSERVABILITY=1
export RATHENA_PACKET_OBSERVABILITY=1
export RATHENA_PACKET_OBSERVABILITY_SLOW_MS=25
export RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS=512
./map-server
```

## Metrics list

Aggregate metrics (no labels):

| Metric | Type | Description |
| --- | --- | --- |
| `rathena_packet_transport_received_bytes_total` | counter | Total bytes received at the transport layer |
| `rathena_packet_transport_sent_bytes_total` | counter | Total bytes sent at the transport layer |
| `rathena_packet_received_packets_total` | counter | Total packets received at the map layer |
| `rathena_packet_received_bytes_total` | counter | Total bytes received at the map layer |
| `rathena_packet_invalid_packets_total` | counter | Total invalid packets rejected at the map layer |
| `rathena_packet_unknown_packets_total` | counter | Total unknown packets received at the map layer |
| `rathena_packet_sent_packets_total` | counter | Total packets sent from the map layer |
| `rathena_packet_sent_bytes_total` | counter | Total bytes sent from the map layer |
| `rathena_packet_broadcast_calls_total` | counter | Total broadcast send calls |
| `rathena_packet_broadcast_recipients_total` | counter | Total broadcast recipients across all calls |
| `rathena_packet_broadcast_recipients_last` | gauge | Recipient count of the most recent broadcast call |
| `rathena_packet_broadcast_recipients_max` | gauge | Maximum observed broadcast recipient count |
| `rathena_packet_id_overflow_total` | counter | Total packet IDs rejected due to bounded registry capacity exhaustion |

Per-packet-ID metrics (label `packet="0xNNNN"`):

| Metric | Type | Description |
| --- | --- | --- |
| `rathena_packet_received_total` | counter | Total received packets by packet ID |
| `rathena_packet_received_bytes_total` | counter | Total received bytes by packet ID |
| `rathena_packet_sent_total` | counter | Total sent packets by packet ID |
| `rathena_packet_sent_bytes_total` | counter | Total sent bytes by packet ID |
| `rathena_packet_processing_duration_last_milliseconds` | gauge | Last processing duration by packet ID in milliseconds |
| `rathena_packet_processing_duration_max_milliseconds` | gauge | Maximum processing duration by packet ID in milliseconds |
| `rathena_packet_processing_slow_total` | counter | Total slow processing events by packet ID |

Per-ID samples are sorted by packet ID during rendering and never in packet
hooks, so the textfile output is deterministic.

## Cardinality cap and overflow

Distinct packet IDs are tracked in a bounded registry whose capacity is set
by `RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS` (clamped to `[16, 4096]`).
Once the registry is full, new packet IDs are not tracked individually;
instead, every rejected observation increments
`rathena_packet_id_overflow_total`. Already-admitted IDs continue to update
normally. The overflow counter itself saturates at `UINT64_MAX` instead of
wrapping.

If `rathena_packet_id_overflow_total` is non-zero, raise the capacity or
investigate unexpected packet IDs rather than increasing the cap blindly.

## Privacy exclusions

The instrumentation intentionally records only volume, timing and broadcast
shape. The following are never exported:

- packet payload bytes beyond length
- player names, account IDs, character IDs or party/guild names
- IP addresses or port numbers
- chat text, whispers or item descriptions
- positional coordinates or map-instance internals
- SQL queries or script contents

The only label on per-ID metrics is `packet="0xNNNN"`, which is the numeric
packet ID in lowercase hexadecimal.

## Disabled/enabled overhead

- Disabled: a single `packet_observability_enabled()` boolean check per hook.
  No counter updates, no registry operations, no allocations, no I/O.
- Enabled: each hook performs a small number of saturating 64-bit additions
  and, for per-ID metrics, a bounded linear lookup in the registry. Hooks are
  designed to be lock-free and never block packet processing.

## Runtime verification

After enabling and generating some traffic:

1. Confirm that `log/metrics/<RATHENA_CORE_OBSERVABILITY_OUTPUT>` contains
   `rathena_packet_*` metrics.
2. Check that the number of distinct `packet="..."` labels does not exceed
   `RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS`.
3. Verify that `rathena_packet_id_overflow_total` is zero under normal load.
4. Confirm that no player names, IPs, chat text or account/character IDs
   appear in the `.prom` file.

## Failure handling

Observability failures never interrupt packet processing. Saturating counters
prevent numeric wrap-around, bounded capacity prevents unbounded memory
growth, and textfile write failures are handled by the core observability
writer. If the packet observability layer fails internally, the packet
continues through the normal map-server code path.

## Rollback

To disable packet observability:

1. unset `RATHENA_PACKET_OBSERVABILITY` or set it to `0`, or
2. restart `map-server` without the variable.

The `.prom` output file is runtime data and may be removed if desired; it is
not required for map-server operation.
