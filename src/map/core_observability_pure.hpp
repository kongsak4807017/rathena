// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef CORE_OBSERVABILITY_PURE_HPP
#define CORE_OBSERVABILITY_PURE_HPP

// Pure, dependency-free helpers for the map-server core observability
// instrumentation. Everything in this header is testable without a
// running map-server and must stay free of rAthena runtime dependencies.

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#include <sys/stat.h>

#ifdef _WIN32
	#ifndef WIN32_LEAN_AND_MEAN
		#define WIN32_LEAN_AND_MEAN
	#endif
	#ifndef NOMINMAX
		#define NOMINMAX
	#endif
	#include <windows.h>
	#include <direct.h>
#else
	#include <sys/types.h>
#endif

namespace core_observability {

inline constexpr int64_t default_interval_ms = 10000;
inline constexpr int64_t min_interval_ms = 1000;
// Sanity clamp for absurdly large intervals (1 hour).
inline constexpr int64_t max_interval_ms = 3600000;

/// Entity type slots. The order matches the type label values exported
/// in the Prometheus output and entity_slot in core_observability_internal.hpp.
enum entity_type_slot : size_t {
	SLOT_PLAYER = 0,
	SLOT_MOB,
	SLOT_NPC,
	SLOT_ITEM,
	SLOT_SKILL,
	SLOT_COUNT,
};

inline constexpr const char* entity_type_names[SLOT_COUNT] = {
	"player",
	"mob",
	"npc",
	"item",
	"skill",
};

/**
 * Parse the RATHENA_CORE_OBSERVABILITY toggle.
 * Only "1", "true", "on" and "yes" (case-insensitive) enable instrumentation.
 * Everything else, including nullptr, keeps it disabled.
 */
inline bool parse_enabled( const char* raw ){
	if( raw == nullptr || *raw == '\0' ){
		return false;
	}

	std::string value( raw );
	std::transform( value.begin(), value.end(), value.begin(), []( unsigned char c ){ return static_cast<char>( std::tolower( c ) ); } );

	return value == "1" || value == "true" || value == "on" || value == "yes";
}

struct interval_parse_result {
	int64_t value_ms;
	/// false when the user supplied a malformed value (caller should warn once)
	bool valid;
};

/**
 * Parse RATHENA_CORE_OBSERVABILITY_INTERVAL_MS.
 * Missing/empty input yields the default without being an error.
 * Malformed input yields the default and valid=false.
 * Numeric input is clamped to [min_interval_ms, max_interval_ms].
 */
inline interval_parse_result parse_interval_ms( const char* raw ){
	if( raw == nullptr ){
		return { default_interval_ms, true };
	}

	std::string value( raw );

	// trim surrounding whitespace
	const size_t first = value.find_first_not_of( " \t\r\n" );
	if( first == std::string::npos ){
		return { default_interval_ms, true };
	}
	const size_t last = value.find_last_not_of( " \t\r\n" );
	value = value.substr( first, last - first + 1 );

	errno = 0;
	char* end = nullptr;
	const long long parsed = std::strtoll( value.c_str(), &end, 10 );

	if( errno == ERANGE || end == value.c_str() || *end != '\0' ){
		return { default_interval_ms, false };
	}

	const int64_t clamped = std::max( min_interval_ms, std::min( static_cast<int64_t>( parsed ), max_interval_ms ) );

	return { clamped, true };
}

/**
 * Drift of a timer callback: scheduled tick vs. actual execution tick.
 * Negative drift (early execution) is clamped to zero.
 */
inline int64_t compute_timer_drift_ms( int64_t scheduled_tick, int64_t actual_tick ){
	const int64_t drift = actual_tick - scheduled_tick;
	return drift > 0 ? drift : 0;
}

/// Escape a Prometheus label value (backslash, double quote, newline).
inline std::string escape_label_value( const std::string& value ){
	std::string out;
	out.reserve( value.size() );

	for( const char c : value ){
		switch( c ){
			case '\\':
				out += "\\\\";
				break;
			case '"':
				out += "\\\"";
				break;
			case '\n':
				out += "\\n";
				break;
			default:
				out += c;
				break;
		}
	}

	return out;
}

struct map_entity_counts {
	std::string name;
	uint64_t entities[SLOT_COUNT]{};
};

struct core_metric_values {
	int64_t timer_drift_last_ms = 0;
	int64_t timer_drift_max_ms = 0;
	uint64_t snapshots_total = 0;
	int64_t snapshot_duration_last_ms = 0;
	int64_t snapshot_duration_max_ms = 0;
	uint64_t write_errors_total = 0;
};

namespace detail {

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

inline bool directory_exists( const std::string& path ){
	struct stat st;
	if( ::stat( path.c_str(), &st ) != 0 ){
		return false;
	}
	return ( st.st_mode & S_IFDIR ) != 0;
}

inline bool make_directory( const std::string& path ){
#ifdef _WIN32
	if( ::_mkdir( path.c_str() ) == 0 ){
#else
	if( ::mkdir( path.c_str(), 0755 ) == 0 ){
#endif
		return true;
	}
	return errno == EEXIST;
}

} // namespace detail

/// Render the complete Prometheus textfile exposition for one snapshot.
inline std::string render_metrics( const core_metric_values& values, const std::vector<map_entity_counts>& maps ){
	std::string out;
	out.reserve( 4096 );

	detail::append_metric_header( out, "rathena_core_timer_drift_last_milliseconds", "Drift of the most recent core observability timer callback in milliseconds.", "gauge" );
	out += "rathena_core_timer_drift_last_milliseconds " + std::to_string( values.timer_drift_last_ms ) + '\n';

	detail::append_metric_header( out, "rathena_core_timer_drift_max_milliseconds", "Maximum observed core observability timer callback drift in milliseconds.", "gauge" );
	out += "rathena_core_timer_drift_max_milliseconds " + std::to_string( values.timer_drift_max_ms ) + '\n';

	detail::append_metric_header( out, "rathena_core_snapshots_total", "Total number of core observability snapshots taken.", "counter" );
	out += "rathena_core_snapshots_total " + std::to_string( values.snapshots_total ) + '\n';

	detail::append_metric_header( out, "rathena_core_snapshot_duration_last_milliseconds", "Duration of the most recent core observability snapshot in milliseconds.", "gauge" );
	out += "rathena_core_snapshot_duration_last_milliseconds " + std::to_string( values.snapshot_duration_last_ms ) + '\n';

	detail::append_metric_header( out, "rathena_core_snapshot_duration_max_milliseconds", "Maximum observed core observability snapshot duration in milliseconds.", "gauge" );
	out += "rathena_core_snapshot_duration_max_milliseconds " + std::to_string( values.snapshot_duration_max_ms ) + '\n';

	detail::append_metric_header( out, "rathena_core_write_errors_total", "Total number of failed core observability metrics writes.", "counter" );
	out += "rathena_core_write_errors_total " + std::to_string( values.write_errors_total ) + '\n';

	uint64_t totals[SLOT_COUNT]{};

	detail::append_metric_header( out, "rathena_map_entities", "Entities per map by type.", "gauge" );
	for( const map_entity_counts& map : maps ){
		const std::string escaped = escape_label_value( map.name );

		for( size_t slot = 0; slot < SLOT_COUNT; slot++ ){
			out += "rathena_map_entities{map=\"" + escaped + "\",type=\"" + entity_type_names[slot] + "\"} " + std::to_string( map.entities[slot] ) + '\n';
			totals[slot] += map.entities[slot];
		}
	}

	detail::append_metric_header( out, "rathena_core_entities_total", "Total entities by type across all maps.", "gauge" );
	for( size_t slot = 0; slot < SLOT_COUNT; slot++ ){
		out += "rathena_core_entities_total{type=\"" + std::string( entity_type_names[slot] ) + "\"} " + std::to_string( totals[slot] ) + '\n';
	}

	return out;
}

/// Create the parent directory of path (recursively) if it does not exist yet.
inline bool ensure_parent_directory( const std::string& path, std::string* error ){
	std::string normalized( path );
	std::replace( normalized.begin(), normalized.end(), '\\', '/' );

	const size_t slash = normalized.find_last_of( '/' );
	if( slash == std::string::npos ){
		// file in the current working directory
		return true;
	}

	const std::string dir = normalized.substr( 0, slash );

	std::string current;
	size_t pos = 0;
	if( !dir.empty() && dir[0] == '/' ){
		current = "/";
		pos = 1;
	}

	while( pos <= dir.size() ){
		const size_t next = dir.find( '/', pos );
		const std::string component = dir.substr( pos, next == std::string::npos ? std::string::npos : next - pos );

		// skip empty components, "." and Windows drive letters ("C:")
		if( !component.empty() && component != "." && component.back() != ':' ){
			if( !current.empty() && current.back() != '/' ){
				current += '/';
			}
			current += component;

			if( !detail::directory_exists( current ) && !detail::make_directory( current ) ){
				if( error != nullptr ){
					*error = "cannot create directory '" + current + "'";
				}
				return false;
			}
		}

		if( next == std::string::npos ){
			break;
		}
		pos = next + 1;
	}

	return true;
}

/**
 * Atomically write content to path: the content is written to a temporary
 * sibling file first, flushed, closed and then atomically moved over path.
 * Exporters reading path therefore never see a partially written file.
 */
inline bool atomic_write_text_file( const std::string& path, const std::string& content, std::string* error ){
	const std::string tmp = path + ".tmp";

	{
		std::ofstream out( tmp, std::ios::binary | std::ios::trunc );
		if( !out ){
			if( error != nullptr ){
				*error = "cannot open temporary file '" + tmp + "'";
			}
			return false;
		}

		out.write( content.data(), static_cast<std::streamsize>( content.size() ) );
		out.flush();

		if( !out.good() ){
			out.close();
			std::remove( tmp.c_str() );
			if( error != nullptr ){
				*error = "failed writing temporary file '" + tmp + "'";
			}
			return false;
		}
	}

#ifdef _WIN32
	const bool replaced = ::MoveFileExA( tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH ) != 0;
#else
	// POSIX rename() atomically replaces an existing destination.
	const bool replaced = std::rename( tmp.c_str(), path.c_str() ) == 0;
#endif

	if( !replaced ){
		std::remove( tmp.c_str() );
		if( error != nullptr ){
			*error = "failed replacing '" + path + "'";
		}
		return false;
	}

	return true;
}

} // namespace core_observability

#endif // CORE_OBSERVABILITY_PURE_HPP
