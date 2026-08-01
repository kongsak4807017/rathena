// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder.

#ifndef SCRIPT_OBSERVABILITY_PURE_HPP
#define SCRIPT_OBSERVABILITY_PURE_HPP

// Pure, dependency-free helpers for the script observability instrumentation.
// Everything in this header is testable without a running map-server and must
// stay free of rAthena runtime dependencies.

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>

namespace script_observability_detail {

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

} // namespace script_observability_detail

inline constexpr uint32_t script_observability_default_slow_ms = 25;
inline constexpr uint32_t script_observability_min_slow_ms = 1;
inline constexpr uint32_t script_observability_max_slow_ms = 60000;

/// Approved bounded script categories. These are the only label values that
/// may appear in the `category` Prometheus label.
enum class ScriptObservabilityCategory : uint8_t {
	Npc = 0,
	Event,
	Timer,
	Item,
	Skill,
	Quest,
	Instance,
	Unknown,
	Count,
};

inline constexpr size_t SCRIPT_OBSERVABILITY_CATEGORY_COUNT = static_cast<size_t>( ScriptObservabilityCategory::Count );

/**
 * Return the approved Prometheus label string for a category.
 * Any unrecognized value maps to "unknown".
 */
inline constexpr const char* script_observability_category_label( ScriptObservabilityCategory category ){
	switch( category ){
		case ScriptObservabilityCategory::Npc:
			return "npc";
		case ScriptObservabilityCategory::Event:
			return "event";
		case ScriptObservabilityCategory::Timer:
			return "timer";
		case ScriptObservabilityCategory::Item:
			return "item";
		case ScriptObservabilityCategory::Skill:
			return "skill";
		case ScriptObservabilityCategory::Quest:
			return "quest";
		case ScriptObservabilityCategory::Instance:
			return "instance";
		case ScriptObservabilityCategory::Unknown:
			return "unknown";
		default:
			return "unknown";
	}
}

/**
 * Parse the RATHENA_SCRIPT_OBSERVABILITY toggle.
 * Only "1", "true", "on" and "yes" (case-insensitive ASCII) enable
 * instrumentation. nullptr and empty input return default_value; everything
 * else returns false.
 */
inline bool script_observability_parse_bool( const char* value, bool default_value ){
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
 * Values that do not fit in uint32_t are treated as overflow and yield fallback.
 */
inline uint32_t script_observability_parse_u32( const char* value, uint32_t fallback, uint32_t min_value, uint32_t max_value ){
	script_observability_detail::unsigned_parse_result r = script_observability_detail::parse_unsigned_decimal( value );

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
inline bool script_observability_is_slow( uint64_t duration_ms, uint32_t threshold_ms ){
	return duration_ms >= static_cast<uint64_t>( threshold_ms );
}

/**
 * Add increment to current without wrapping. If the sum would exceed UINT64_MAX,
 * the result is UINT64_MAX.
 */
inline uint64_t script_observability_saturating_add( uint64_t current, uint64_t increment ){
	const uint64_t max = std::numeric_limits<uint64_t>::max();

	if( current > max - increment ){
		return max;
	}

	return current + increment;
}
/// Per-category and aggregate counter bucket for script execution slices.
/// All fields default to zero and must be updated through record_slice or
/// equivalent saturating helpers so long-running servers never wrap.
struct ScriptObservabilityCounters {
	uint64_t execution_slices_total = 0;
	uint64_t execution_failures_total = 0;
	uint64_t slow_execution_slices_total = 0;
	uint64_t execution_duration_last_ms = 0;
	uint64_t execution_duration_max_ms = 0;
	uint64_t commands_total = 0;
	uint64_t commands_max_per_slice = 0;

	void record_slice( uint64_t duration_ms, uint64_t commands, bool failed, uint32_t slow_threshold_ms ){
		execution_slices_total = script_observability_saturating_add( execution_slices_total, 1 );

		if( failed ){
			execution_failures_total = script_observability_saturating_add( execution_failures_total, 1 );
		}

		if( script_observability_is_slow( duration_ms, slow_threshold_ms ) ){
			slow_execution_slices_total = script_observability_saturating_add( slow_execution_slices_total, 1 );
		}

		execution_duration_last_ms = duration_ms;

		if( duration_ms > execution_duration_max_ms ){
			execution_duration_max_ms = duration_ms;
		}

		commands_total = script_observability_saturating_add( commands_total, commands );

		if( commands > commands_max_per_slice ){
			commands_max_per_slice = commands;
		}
	}
};

/// Fixed-storage snapshot for script execution observability. The per-category
/// array is indexed by ScriptObservabilityCategory; the aggregate holds totals
/// across all categories. No dynamic allocation occurs during recording.
struct ScriptObservabilitySnapshot {
	std::array<ScriptObservabilityCounters, SCRIPT_OBSERVABILITY_CATEGORY_COUNT> by_category;
	ScriptObservabilityCounters aggregate;

	void record_slice( ScriptObservabilityCategory category, uint64_t duration_ms, uint64_t commands, bool failed, uint32_t slow_threshold_ms ){
		const uint8_t index = static_cast<uint8_t>( category );
		ScriptObservabilityCounters& slot = ( index < by_category.size() ) ? by_category[index] : by_category[static_cast<size_t>( ScriptObservabilityCategory::Unknown )];

		slot.record_slice( duration_ms, commands, failed, slow_threshold_ms );
		aggregate.record_slice( duration_ms, commands, failed, slow_threshold_ms );
	}
};

namespace script_observability_detail {

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

inline void append_labeled_uint64_metric( std::string& out, const char* name, const char* label, uint64_t value ){
	out += name;
	out += "{category=\"";
	out += label;
	out += "\"} ";
	out += std::to_string( value );
	out += '\n';
}

} // namespace script_observability_detail

/**
 * Render a script observability snapshot as deterministic Prometheus textfile
 * exposition. Aggregate metrics are emitted first, followed by per-category
 * metrics in enum order. The result ends with a single trailing newline and
 * contains no blank lines.
 */
inline std::string script_observability_render_prometheus( const ScriptObservabilitySnapshot& snapshot ){
	using script_observability_detail::append_metric_header;
	using script_observability_detail::append_uint64_metric;
	using script_observability_detail::append_labeled_uint64_metric;

	std::string out;
	out.reserve( 4096 );

	append_metric_header( out, "rathena_script_execution_slices_total", "Total script execution slices.", "counter" );
	append_uint64_metric( out, "rathena_script_execution_slices_total", snapshot.aggregate.execution_slices_total );

	append_metric_header( out, "rathena_script_execution_failures_total", "Total script execution slices that ended in error or abort.", "counter" );
	append_uint64_metric( out, "rathena_script_execution_failures_total", snapshot.aggregate.execution_failures_total );

	append_metric_header( out, "rathena_script_slow_execution_slices_total", "Total script execution slices with duration >= slow threshold.", "counter" );
	append_uint64_metric( out, "rathena_script_slow_execution_slices_total", snapshot.aggregate.slow_execution_slices_total );

	append_metric_header( out, "rathena_script_execution_duration_last_milliseconds", "Duration of the most recent execution slice in milliseconds.", "gauge" );
	append_uint64_metric( out, "rathena_script_execution_duration_last_milliseconds", snapshot.aggregate.execution_duration_last_ms );

	append_metric_header( out, "rathena_script_execution_duration_max_milliseconds", "Maximum duration of any execution slice in milliseconds.", "gauge" );
	append_uint64_metric( out, "rathena_script_execution_duration_max_milliseconds", snapshot.aggregate.execution_duration_max_ms );

	append_metric_header( out, "rathena_script_commands_total", "Total commands executed across all slices.", "counter" );
	append_uint64_metric( out, "rathena_script_commands_total", snapshot.aggregate.commands_total );

	append_metric_header( out, "rathena_script_commands_max_per_slice", "Maximum commands executed in a single slice.", "gauge" );
	append_uint64_metric( out, "rathena_script_commands_max_per_slice", snapshot.aggregate.commands_max_per_slice );

	static_assert( SCRIPT_OBSERVABILITY_CATEGORY_COUNT == 8, "Update labeled metric loops when categories change." );

	const char* labels[SCRIPT_OBSERVABILITY_CATEGORY_COUNT] = {
		"npc",
		"event",
		"timer",
		"item",
		"skill",
		"quest",
		"instance",
		"unknown",
	};

	append_metric_header( out, "rathena_script_execution_slices_total", "Total script execution slices by category.", "counter" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_execution_slices_total", labels[i], snapshot.by_category[i].execution_slices_total );
	}

	append_metric_header( out, "rathena_script_execution_failures_total", "Total script execution failures by category.", "counter" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_execution_failures_total", labels[i], snapshot.by_category[i].execution_failures_total );
	}

	append_metric_header( out, "rathena_script_slow_execution_slices_total", "Total slow script execution slices by category.", "counter" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_slow_execution_slices_total", labels[i], snapshot.by_category[i].slow_execution_slices_total );
	}

	append_metric_header( out, "rathena_script_execution_duration_last_milliseconds", "Last script execution slice duration by category in milliseconds.", "gauge" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_execution_duration_last_milliseconds", labels[i], snapshot.by_category[i].execution_duration_last_ms );
	}

	append_metric_header( out, "rathena_script_execution_duration_max_milliseconds", "Maximum script execution slice duration by category in milliseconds.", "gauge" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_execution_duration_max_milliseconds", labels[i], snapshot.by_category[i].execution_duration_max_ms );
	}

	append_metric_header( out, "rathena_script_commands_total", "Total commands executed by category.", "counter" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_commands_total", labels[i], snapshot.by_category[i].commands_total );
	}

	append_metric_header( out, "rathena_script_commands_max_per_slice", "Maximum commands executed in a single slice by category.", "gauge" );
	for( size_t i = 0; i < SCRIPT_OBSERVABILITY_CATEGORY_COUNT; ++i ){
		append_labeled_uint64_metric( out, "rathena_script_commands_max_per_slice", labels[i], snapshot.by_category[i].commands_max_per_slice );
	}

	return out;
}

#endif // SCRIPT_OBSERVABILITY_PURE_HPP