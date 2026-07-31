// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef PACKET_OBSERVABILITY_PURE_HPP
#define PACKET_OBSERVABILITY_PURE_HPP

// Pure, dependency-free helpers for the packet observability instrumentation.
// Everything in this header is testable without a running map-server and must
// stay free of rAthena runtime dependencies.

#include <algorithm>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string>
#include <utility>
#include <vector>

namespace packet_observability_detail {

struct unsigned_parse_result {
	uint64_t value;
	bool valid;
};

/**
 * Parse a non-negative decimal integer from a C string.
 * Leading/trailing whitespace, signs and non-digit characters are rejected.
 * Overflow is detected without wrapping.
 */
inline unsigned_parse_result parse_unsigned_decimal( const char* raw ){
	if( raw == nullptr || raw[0] == '\0' ){
		return { 0, false };
	}

	uint64_t value = 0;

	for( const char* p = raw; *p != '\0'; ++p ){
		const unsigned char c = static_cast<unsigned char>( *p );

		if( c < '0' || c > '9' ){
			return { 0, false };
		}

		const uint64_t digit = static_cast<uint64_t>( c - '0' );
		const uint64_t max = std::numeric_limits<uint64_t>::max();

		if( value > ( max - digit ) / 10 ){
			return { 0, false };
		}

		value = value * 10 + digit;
	}

	return { value, true };
}

} // namespace packet_observability_detail

inline constexpr uint32_t packet_observability_default_slow_ms = 25;
inline constexpr uint32_t packet_observability_min_slow_ms = 1;
inline constexpr uint32_t packet_observability_max_slow_ms = 60000;

inline constexpr size_t packet_observability_default_capacity = 512;
inline constexpr size_t packet_observability_min_capacity = 16;
inline constexpr size_t packet_observability_max_capacity = 4096;

/**
 * Parse the RATHENA_PACKET_OBSERVABILITY toggle.
 * Only "1", "true", "on" and "yes" (case-insensitive ASCII) enable
 * instrumentation. Everything else, including nullptr, keeps it disabled.
 */
inline bool packet_observability_parse_enabled( const char* value ){
	if( value == nullptr ){
		return false;
	}

	// Manual case folding keeps the comparison independent of the current C locale.
	const auto lower_ascii = []( char c ) -> char {
		if( c >= 'A' && c <= 'Z' ){
			return static_cast<char>( c + ( 'a' - 'A' ) );
		}
		return c;
	};

	const char* p = value;
	char lowered[8]{};
	size_t i = 0;

	for( ; *p != '\0' && i < sizeof( lowered ) - 1; ++p, ++i ){
		lowered[i] = lower_ascii( *p );
	}

	if( *p != '\0' ){
		// Input is longer than any recognized keyword.
		return false;
	}

	lowered[i] = '\0';

	return std::strcmp( lowered, "1" ) == 0
		|| std::strcmp( lowered, "true" ) == 0
		|| std::strcmp( lowered, "on" ) == 0
		|| std::strcmp( lowered, "yes" ) == 0;
}

/**
 * Parse RATHENA_PACKET_OBSERVABILITY_SLOW_MS.
 * Missing/empty/malformed/signed/overflow input yields the default and sets
 * used_fallback=true so the caller can warn once. Valid numeric input is
 * clamped to [min_slow_ms, max_slow_ms] without setting the fallback flag.
 */
inline uint32_t packet_observability_parse_slow_ms( const char* value, bool& used_fallback ){
	packet_observability_detail::unsigned_parse_result r = packet_observability_detail::parse_unsigned_decimal( value );

	if( !r.valid ){
		used_fallback = true;
		return packet_observability_default_slow_ms;
	}

	used_fallback = false;

	const uint64_t clamped = std::max(
		static_cast<uint64_t>( packet_observability_min_slow_ms ),
		std::min( r.value, static_cast<uint64_t>( packet_observability_max_slow_ms ) )
	);

	return static_cast<uint32_t>( clamped );
}

/**
 * Parse RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS.
 * Missing/empty/malformed/signed/overflow input yields the default and sets
 * used_fallback=true. Valid numeric input is clamped to
 * [min_capacity, max_capacity] without setting the fallback flag.
 */
inline size_t packet_observability_parse_capacity( const char* value, bool& used_fallback ){
	packet_observability_detail::unsigned_parse_result r = packet_observability_detail::parse_unsigned_decimal( value );

	if( !r.valid ){
		used_fallback = true;
		return packet_observability_default_capacity;
	}

	used_fallback = false;

	const uint64_t clamped = std::max(
		static_cast<uint64_t>( packet_observability_min_capacity ),
		std::min( r.value, static_cast<uint64_t>( packet_observability_max_capacity ) )
	);

	return static_cast<size_t>( clamped );
}

/**
 * Add increment to current without wrapping. If the sum would exceed UINT64_MAX,
 * the result is UINT64_MAX.
 */
inline uint64_t packet_observability_saturating_add( uint64_t current, uint64_t increment ){
	const uint64_t max = std::numeric_limits<uint64_t>::max();

	if( current > max - increment ){
		return max;
	}

	return current + increment;
}

/**
 * Return true when duration_ms is greater than or equal to threshold_ms.
 */
inline bool packet_observability_is_slow( uint64_t duration_ms, uint32_t threshold_ms ){
	return duration_ms >= static_cast<uint64_t>( threshold_ms );
}

/**
 * Format a packet id as a lowercase four-digit hexadecimal string with a 0x
 * prefix, e.g. 0x0064 or 0xffff.
 */
inline std::string packet_observability_format_packet_id( uint16_t packet_id ){
	char buffer[7]{};
	std::snprintf( buffer, sizeof( buffer ), "0x%04x", static_cast<int>( packet_id ) );
	return std::string( buffer );
}

/// Per-packet-ID counters. All fields default to zero and must be updated
/// with packet_observability_saturating_add (or an equivalent saturation
/// discipline) so long-running servers never wrap around unexpectedly.
struct packet_observability_packet_metrics {
	uint64_t received_total = 0;
	uint64_t received_bytes_total = 0;
	uint64_t sent_total = 0;
	uint64_t sent_bytes_total = 0;
	uint64_t processing_duration_last_ms = 0;
	uint64_t processing_duration_max_ms = 0;
	uint64_t processing_slow_total = 0;
};

/// Snapshot of all packet observability counters exported to Prometheus.
/// The packets vector is sorted by packet_id only during rendering; hooks
/// must never sort it.
struct packet_observability_snapshot {
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
	std::vector<std::pair<uint16_t, packet_observability_packet_metrics>> packets;
};

/**
 * Bounded packet-ID registry backed by a pre-sized std::vector.
 *
 * Admission uses linear lookup and fills the first empty slot. Capacity is
 * bounded outside this class (the caller is expected to pass a value already
 * clamped to packet_observability_max_capacity). The registry never allocates
 * after construction, so pointers returned by admit() remain stable for the
 * lifetime of the registry.
 */
class packet_observability_bounded_registry {
public:
	struct entry {
		uint16_t packet_id = 0;
		bool occupied = false;
		packet_observability_packet_metrics metrics;
	};

	explicit packet_observability_bounded_registry( size_t capacity ){
		entries_.resize( capacity );
	}

	/// Return the metrics slot for packet_id, creating it if space remains.
	/// If packet_id was already admitted, return the same pointer. If the
	/// registry is full, return nullptr.
	packet_observability_packet_metrics* admit( uint16_t packet_id ){
		for( entry& e : entries_ ){
			if( e.occupied && e.packet_id == packet_id ){
				return &e.metrics;
			}
		}

		for( entry& e : entries_ ){
			if( !e.occupied ){
				e.packet_id = packet_id;
				e.occupied = true;
				return &e.metrics;
			}
		}

		return nullptr;
	}

	/// Count currently occupied entries.
	size_t size() const{
		size_t count = 0;

		for( const entry& e : entries_ ){
			if( e.occupied ){
				++count;
			}
		}

		return count;
	}

	/// Total reserved capacity.
	size_t capacity() const{
		return entries_.size();
	}

	/// Direct read-only access to the underlying storage for deterministic
	/// snapshot iteration. Occupied entries are preserved in insertion order.
	const std::vector<entry>& storage() const{
		return entries_;
	}

private:
	std::vector<entry> entries_;
};

namespace packet_observability_detail {

inline void append_metric_header( std::string& out, const char* name, const char* help, const char* type ){
	out += "# HELP ";
	out += name;
	out += ' ';
	out += help;
	out += '\n';
	out += "# TYPE ";
	out += name;
	out += ' ';
	out += type;
	out += '\n';
}

inline void append_uint64_metric( std::string& out, const char* name, uint64_t value ){
	out += name;
	out += ' ';
	out += std::to_string( value );
	out += '\n';
}

inline void append_labeled_uint64_metric( std::string& out, const char* name, const std::string& label, uint64_t value ){
	out += name;
	out += "{packet=\"";
	out += label;
	out += "\"} ";
	out += std::to_string( value );
	out += '\n';
}

} // namespace packet_observability_detail

/**
 * Render a packet observability snapshot as Prometheus textfile exposition.
 *
 * Output is deterministic: per-ID samples are sorted by packet_id during
 * rendering and never in packet hooks. The result ends with a single
 * trailing newline and contains no blank lines.
 */
inline std::string packet_observability_render_prometheus( const packet_observability_snapshot& snapshot ){
	using packet_observability_detail::append_metric_header;
	using packet_observability_detail::append_uint64_metric;
	using packet_observability_detail::append_labeled_uint64_metric;

	std::string out;
	out.reserve( 4096 );

	append_metric_header( out, "rathena_packet_transport_received_bytes_total", "Total bytes received at the transport layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_transport_received_bytes_total", snapshot.transport_received_bytes_total );

	append_metric_header( out, "rathena_packet_transport_sent_bytes_total", "Total bytes sent at the transport layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_transport_sent_bytes_total", snapshot.transport_sent_bytes_total );

	append_metric_header( out, "rathena_packet_received_packets_total", "Total packets received at the map layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_received_packets_total", snapshot.received_packets_total );

	append_metric_header( out, "rathena_packet_received_bytes_total", "Total bytes received at the map layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_received_bytes_total", snapshot.received_bytes_total );

	append_metric_header( out, "rathena_packet_invalid_packets_total", "Total invalid packets rejected at the map layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_invalid_packets_total", snapshot.invalid_packets_total );

	append_metric_header( out, "rathena_packet_unknown_packets_total", "Total unknown packets received at the map layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_unknown_packets_total", snapshot.unknown_packets_total );

	append_metric_header( out, "rathena_packet_sent_packets_total", "Total packets sent from the map layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_sent_packets_total", snapshot.sent_packets_total );

	append_metric_header( out, "rathena_packet_sent_bytes_total", "Total bytes sent from the map layer.", "counter" );
	append_uint64_metric( out, "rathena_packet_sent_bytes_total", snapshot.sent_bytes_total );

	append_metric_header( out, "rathena_packet_broadcast_calls_total", "Total broadcast send calls.", "counter" );
	append_uint64_metric( out, "rathena_packet_broadcast_calls_total", snapshot.broadcast_calls_total );

	append_metric_header( out, "rathena_packet_broadcast_recipients_total", "Total broadcast recipients across all calls.", "counter" );
	append_uint64_metric( out, "rathena_packet_broadcast_recipients_total", snapshot.broadcast_recipients_total );

	append_metric_header( out, "rathena_packet_broadcast_recipients_last", "Recipient count of the most recent broadcast call.", "gauge" );
	append_uint64_metric( out, "rathena_packet_broadcast_recipients_last", snapshot.broadcast_recipients_last );

	append_metric_header( out, "rathena_packet_broadcast_recipients_max", "Maximum observed broadcast recipient count.", "gauge" );
	append_uint64_metric( out, "rathena_packet_broadcast_recipients_max", snapshot.broadcast_recipients_max );

	append_metric_header( out, "rathena_packet_id_overflow_total", "Total packet IDs rejected due to bounded registry capacity exhaustion.", "counter" );
	append_uint64_metric( out, "rathena_packet_id_overflow_total", snapshot.packet_id_overflow_total );

	// Sort a temporary copy so rendering is deterministic without mutating
	// the snapshot or sorting in packet hooks.
	std::vector<std::pair<uint16_t, packet_observability_packet_metrics>> sorted_packets = snapshot.packets;
	std::sort( sorted_packets.begin(), sorted_packets.end(), []( const std::pair<uint16_t, packet_observability_packet_metrics>& a, const std::pair<uint16_t, packet_observability_packet_metrics>& b ){
		return a.first < b.first;
	} );

	append_metric_header( out, "rathena_packet_received_total", "Total received packets by packet ID.", "counter" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_received_total", packet_observability_format_packet_id( p.first ), p.second.received_total );
	}

	append_metric_header( out, "rathena_packet_received_bytes_total", "Total received bytes by packet ID.", "counter" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_received_bytes_total", packet_observability_format_packet_id( p.first ), p.second.received_bytes_total );
	}

	append_metric_header( out, "rathena_packet_sent_total", "Total sent packets by packet ID.", "counter" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_sent_total", packet_observability_format_packet_id( p.first ), p.second.sent_total );
	}

	append_metric_header( out, "rathena_packet_sent_bytes_total", "Total sent bytes by packet ID.", "counter" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_sent_bytes_total", packet_observability_format_packet_id( p.first ), p.second.sent_bytes_total );
	}

	append_metric_header( out, "rathena_packet_processing_duration_last_milliseconds", "Last processing duration by packet ID in milliseconds.", "gauge" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_processing_duration_last_milliseconds", packet_observability_format_packet_id( p.first ), p.second.processing_duration_last_ms );
	}

	append_metric_header( out, "rathena_packet_processing_duration_max_milliseconds", "Maximum processing duration by packet ID in milliseconds.", "gauge" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_processing_duration_max_milliseconds", packet_observability_format_packet_id( p.first ), p.second.processing_duration_max_ms );
	}

	append_metric_header( out, "rathena_packet_processing_slow_total", "Total slow processing events by packet ID.", "counter" );
	for( const auto& p : sorted_packets ){
		append_labeled_uint64_metric( out, "rathena_packet_processing_slow_total", packet_observability_format_packet_id( p.first ), p.second.processing_slow_total );
	}

	return out;
}

#endif // PACKET_OBSERVABILITY_PURE_HPP
