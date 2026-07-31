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
#include <limits>
#include <string>

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

#endif // PACKET_OBSERVABILITY_PURE_HPP
