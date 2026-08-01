// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef SQL_OBSERVABILITY_INTERNAL_HPP
#define SQL_OBSERVABILITY_INTERNAL_HPP

// Runtime state structures and test-only reset/snapshot seam for SQL
// observability. This header is intended for use by sql_observability.cpp and
// by unit tests built with -DRATHENA_SQL_OBSERVABILITY_TESTING.

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

#include "sql_observability_pure.hpp"

struct sql_observability_state {
	bool enabled = false;
	bool initialized = false;
	uint32_t slow_threshold_ms = 50;
	size_t max_subsystems = 16;
	SqlObservabilitySubsystem current_subsystem = SqlObservabilitySubsystem::Unknown;
	SqlObservabilitySnapshot snapshot;
};

extern sql_observability_state g_sql_observability_state;

/**
 * Check whether a non-empty boolean environment value is one of the recognized
 * forms. Empty/missing values are considered valid (they use the default).
 */
inline bool is_valid_bool_setting( const char* raw ){
	if( raw == nullptr || raw[0] == '\0' ){
		return true;
	}

	const auto lower_ascii = []( char c ) -> char {
		if( c >= 'A' && c <= 'Z' ){
			return static_cast<char>( c + ( 'a' - 'A' ) );
		}
		return c;
	};

	const char* p = raw;
	char lowered[8]{};
	size_t i = 0;

	for( ; *p != '\0' && i < sizeof( lowered ) - 1; ++p, ++i ){
		lowered[i] = lower_ascii( *p );
	}

	if( *p != '\0' ){
		return false;
	}

	lowered[i] = '\0';

	return std::strcmp( lowered, "0" ) == 0
		|| std::strcmp( lowered, "1" ) == 0
		|| std::strcmp( lowered, "true" ) == 0
		|| std::strcmp( lowered, "false" ) == 0
		|| std::strcmp( lowered, "on" ) == 0
		|| std::strcmp( lowered, "off" ) == 0
		|| std::strcmp( lowered, "yes" ) == 0
		|| std::strcmp( lowered, "no" ) == 0;
}

/**
 * Check whether a non-empty unsigned environment value is a valid decimal
 * integer that fits into uint32_t. Empty/missing values are valid; values that
 * overflow uint32_t or contain non-digit characters are not.
 */
inline bool is_valid_u32_setting( const char* raw ){
	if( raw == nullptr || raw[0] == '\0' ){
		return true;
	}

	sql_observability_detail::unsigned_parse_result r = sql_observability_detail::parse_unsigned_decimal( raw );

	if( !r.valid ){
		return false;
	}

	return r.value <= static_cast<uint64_t>( std::numeric_limits<uint32_t>::max() );
}

#endif // SQL_OBSERVABILITY_INTERNAL_HPP
