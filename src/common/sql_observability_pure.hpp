// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef SQL_OBSERVABILITY_PURE_HPP
#define SQL_OBSERVABILITY_PURE_HPP

// Pure, dependency-free helpers for the SQL observability instrumentation.
// Everything in this header is testable without a running server and must
// stay free of rAthena runtime dependencies.

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

namespace sql_observability_detail {

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

} // namespace sql_observability_detail

/// SQL-related subsystem used to label per-server metrics.
enum class SqlObservabilitySubsystem : uint8_t {
	Login,
	Char,
	Map,
	Log,
	Web,
	Unknown,
};

/**
 * Return the approved Prometheus label string for a subsystem.
 * Any unrecognized value maps to "unknown".
 */
inline constexpr const char* sql_observability_subsystem_label( SqlObservabilitySubsystem subsystem ){
	switch( subsystem ){
		case SqlObservabilitySubsystem::Login:
			return "login";
		case SqlObservabilitySubsystem::Char:
			return "char";
		case SqlObservabilitySubsystem::Map:
			return "map";
		case SqlObservabilitySubsystem::Log:
			return "log";
		case SqlObservabilitySubsystem::Web:
			return "web";
		case SqlObservabilitySubsystem::Unknown:
			return "unknown";
	}

	// Defensive fallback for out-of-range enum values that bypass the switch.
	return "unknown";
}

/**
 * Parse the RATHENA_SQL_OBSERVABILITY toggle.
 * Only "1", "true", "on" and "yes" (case-insensitive ASCII) enable
 * instrumentation. nullptr and empty input return default_value; everything
 * else returns false.
 */
inline bool sql_observability_parse_bool( const char* value, bool default_value ){
	if( value == nullptr || value[0] == '\0' ){
		return default_value;
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
 * Parse an unsigned decimal integer with clamping.
 * Missing/empty/malformed/signed/overflow input yields fallback. Valid
 * numeric input is clamped to [min_value, max_value] without yielding fallback.
 */
inline uint32_t sql_observability_parse_u32( const char* value, uint32_t fallback, uint32_t min_value, uint32_t max_value ){
	sql_observability_detail::unsigned_parse_result r = sql_observability_detail::parse_unsigned_decimal( value );

	if( !r.valid ){
		return fallback;
	}

	if( r.value > static_cast<uint64_t>( std::numeric_limits<uint32_t>::max() ) ){
		return fallback;
	}

	const uint64_t clamped = std::max(
		static_cast<uint64_t>( min_value ),
		std::min( r.value, static_cast<uint64_t>( max_value ) )
	);

	return static_cast<uint32_t>( clamped );
}

/**
 * Return true when duration_ms is greater than or equal to threshold_ms.
 */
inline bool sql_observability_is_slow( uint64_t duration_ms, uint32_t threshold_ms ){
	return duration_ms >= static_cast<uint64_t>( threshold_ms );
}

#endif // SQL_OBSERVABILITY_PURE_HPP
