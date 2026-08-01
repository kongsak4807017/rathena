// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef PACKET_OBSERVABILITY_INTERNAL_HPP
#define PACKET_OBSERVABILITY_INTERNAL_HPP

#include <cstddef>
#include <cstdint>

#include "packet_observability_pure.hpp"

namespace packet_observability_internal {

/// Mutable runtime state for packet observability. Kept in this header so
/// test builds can inspect and reset it without reaching into the .cpp file.
struct packet_observability_state {
	bool enabled = false;
	uint32_t slow_ms = packet_observability_default_slow_ms;
	size_t capacity = 0;

	uint64_t transport_received_bytes_total = 0;
	uint64_t transport_sent_bytes_total = 0;
	uint64_t received_packets_total = 0;
	uint64_t received_bytes_total = 0;
	uint64_t invalid_packets_total = 0;
	uint64_t unknown_packets_total = 0;
	uint64_t sent_packets_total = 0;
	uint64_t sent_bytes_total = 0;
	uint64_t broadcast_calls_total = 0;
	uint64_t broadcast_recipients_total = 0;
	uint64_t broadcast_recipients_last = 0;
	uint64_t broadcast_recipients_max = 0;
	uint64_t packet_id_overflow_total = 0;

	packet_observability_bounded_registry registry{ 0 };
};

extern packet_observability_state state;

} // namespace packet_observability_internal

#endif // PACKET_OBSERVABILITY_INTERNAL_HPP
