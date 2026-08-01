// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef SQL_OBSERVABILITY_PURE_HPP
#define SQL_OBSERVABILITY_PURE_HPP

// Pure, dependency-free helpers for the SQL observability instrumentation.
// Everything in this header is testable without a running server and must
// stay free of rAthena runtime dependencies.

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>

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

/**
 * Add increment to current without wrapping. If the sum would exceed UINT64_MAX,
 * the result is UINT64_MAX.
 */
inline uint64_t sql_observability_saturating_add( uint64_t current, uint64_t increment ){
	const uint64_t max = std::numeric_limits<uint64_t>::max();

	if( current > max - increment ){
		return max;
	}

	return current + increment;
}

/// Counter bucket for query/prepared operations. All fields default to zero
/// and must be updated through the record_* helpers so long-running servers
/// never wrap around unexpectedly.
struct SqlObservabilityCounters {
	uint64_t attempts_total = 0;
	uint64_t failures_total = 0;
	uint64_t slow_total = 0;
	uint64_t duration_last_ms = 0;
	uint64_t duration_max_ms = 0;

	void record_attempt(){
		attempts_total = sql_observability_saturating_add( attempts_total, 1 );
	}

	/// Record a failed attempt: increments both attempts_total and failures_total.
	void record_failure(){
		record_attempt();
		failures_total = sql_observability_saturating_add( failures_total, 1 );
	}

	void record_duration( uint64_t duration_ms, uint32_t slow_threshold_ms ){
		duration_last_ms = duration_ms;
		duration_max_ms = std::max( duration_max_ms, duration_ms );

		if( sql_observability_is_slow( duration_ms, slow_threshold_ms ) ){
			slow_total = sql_observability_saturating_add( slow_total, 1 );
		}
	}
};

/// Per-subsystem counters indexed by the SqlObservabilitySubsystem enum.
/// The fixed array guarantees bounded memory and deterministic iteration.
struct SqlObservabilitySubsystemCounters {
	std::array<SqlObservabilityCounters, 6> by_subsystem;
	SqlObservabilityCounters aggregate;

	/// Optional pointer to the snapshot-level overflow counter. When set,
	/// invalid enum values increment this counter exactly once per admission.
	uint64_t* overflow_counter = nullptr;

	void record_query( SqlObservabilitySubsystem subsystem, uint64_t duration_ms, bool success, uint32_t slow_threshold_ms ){
		_record( subsystem, duration_ms, success, slow_threshold_ms );
	}

	void record_prepared( SqlObservabilitySubsystem subsystem, uint64_t duration_ms, bool success, uint32_t slow_threshold_ms ){
		_record( subsystem, duration_ms, success, slow_threshold_ms );
	}

private:
	SqlObservabilityCounters& _resolve( SqlObservabilitySubsystem subsystem, bool& overflow ){
		const uint8_t index = static_cast<uint8_t>( subsystem );

		if( index < by_subsystem.size() ){
			overflow = false;
			return by_subsystem[index];
		}

		overflow = true;
		return by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Unknown )];
	}

	void _record( SqlObservabilitySubsystem subsystem, uint64_t duration_ms, bool success, uint32_t slow_threshold_ms ){
		bool overflow = false;
		SqlObservabilityCounters& slot = _resolve( subsystem, overflow );

		if( success ){
			slot.record_attempt();
			aggregate.record_attempt();
		}else{
			slot.record_failure();
			aggregate.record_failure();
		}

		slot.record_duration( duration_ms, slow_threshold_ms );
		aggregate.record_duration( duration_ms, slow_threshold_ms );

		if( overflow && overflow_counter != nullptr ){
			*overflow_counter = sql_observability_saturating_add( *overflow_counter, 1 );
		}
	}
};

/// Connection health counters.
struct SqlObservabilityConnectionCounters {
	uint64_t connect_attempts_total = 0;
	uint64_t connect_failures_total = 0;
	uint64_t ping_total = 0;
	uint64_t ping_failures_total = 0;
	uint64_t reconnect_events_total = 0;
	uint64_t subsystem_overflow_total = 0;

	void record_connect( bool success ){
		connect_attempts_total = sql_observability_saturating_add( connect_attempts_total, 1 );

		if( !success ){
			connect_failures_total = sql_observability_saturating_add( connect_failures_total, 1 );
		}
	}

	void record_ping( bool success ){
		ping_total = sql_observability_saturating_add( ping_total, 1 );

		if( !success ){
			ping_failures_total = sql_observability_saturating_add( ping_failures_total, 1 );
		}
	}

	void record_reconnect(){
		reconnect_events_total = sql_observability_saturating_add( reconnect_events_total, 1 );
	}

	void record_overflow(){
		subsystem_overflow_total = sql_observability_saturating_add( subsystem_overflow_total, 1 );
	}
};

/// Full snapshot model exported to Prometheus.
struct SqlObservabilitySnapshot {
	SqlObservabilitySubsystemCounters queries;
	SqlObservabilitySubsystemCounters prepared;
	SqlObservabilityConnectionCounters connections;

	SqlObservabilitySnapshot(){
		queries.overflow_counter = &connections.subsystem_overflow_total;
		prepared.overflow_counter = &connections.subsystem_overflow_total;
	}
};

namespace sql_observability_detail {

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

inline void append_labeled_uint64_metric( std::string& out, const char* name, const char* label_value, uint64_t value ){
	out += name;
	out += "{subsystem=\"";
	out += label_value;
	out += "\"} ";
	out += std::to_string( value );
	out += '\n';
}

} // namespace sql_observability_detail

/**
 * Render a SQL observability snapshot as Prometheus textfile exposition.
 *
 * Output is deterministic: aggregate metrics are emitted first, followed by
 * per-subsystem metrics in fixed enum order (Login, Char, Map, Log, Web,
 * Unknown). The result ends with a single trailing newline and contains no
 * blank lines. No SQL text, parameters, or error details are ever emitted.
 */
inline std::string sql_observability_render_prometheus( const SqlObservabilitySnapshot& snapshot ){
	using sql_observability_detail::append_metric_header;
	using sql_observability_detail::append_uint64_metric;
	using sql_observability_detail::append_labeled_uint64_metric;

	std::string out;
	out.reserve( 8192 );

	// Aggregate query metrics.
	append_metric_header( out, "rathena_sql_queries_total", "Total SQL queries executed.", "counter" );
	append_uint64_metric( out, "rathena_sql_queries_total", snapshot.queries.aggregate.attempts_total );

	append_metric_header( out, "rathena_sql_query_failures_total", "Total SQL query failures.", "counter" );
	append_uint64_metric( out, "rathena_sql_query_failures_total", snapshot.queries.aggregate.failures_total );

	append_metric_header( out, "rathena_sql_slow_queries_total", "Total slow SQL queries.", "counter" );
	append_uint64_metric( out, "rathena_sql_slow_queries_total", snapshot.queries.aggregate.slow_total );

	append_metric_header( out, "rathena_sql_query_duration_last_milliseconds", "Duration of the last SQL query in milliseconds.", "gauge" );
	append_uint64_metric( out, "rathena_sql_query_duration_last_milliseconds", snapshot.queries.aggregate.duration_last_ms );

	append_metric_header( out, "rathena_sql_query_duration_max_milliseconds", "Maximum observed SQL query duration in milliseconds.", "gauge" );
	append_uint64_metric( out, "rathena_sql_query_duration_max_milliseconds", snapshot.queries.aggregate.duration_max_ms );

	// Aggregate prepared statement metrics.
	append_metric_header( out, "rathena_sql_prepared_executions_total", "Total prepared statement executions.", "counter" );
	append_uint64_metric( out, "rathena_sql_prepared_executions_total", snapshot.prepared.aggregate.attempts_total );

	append_metric_header( out, "rathena_sql_prepared_failures_total", "Total prepared statement execution failures.", "counter" );
	append_uint64_metric( out, "rathena_sql_prepared_failures_total", snapshot.prepared.aggregate.failures_total );

	append_metric_header( out, "rathena_sql_prepared_slow_total", "Total slow prepared statement executions.", "counter" );
	append_uint64_metric( out, "rathena_sql_prepared_slow_total", snapshot.prepared.aggregate.slow_total );

	append_metric_header( out, "rathena_sql_prepared_duration_last_milliseconds", "Duration of the last prepared statement execution in milliseconds.", "gauge" );
	append_uint64_metric( out, "rathena_sql_prepared_duration_last_milliseconds", snapshot.prepared.aggregate.duration_last_ms );

	append_metric_header( out, "rathena_sql_prepared_duration_max_milliseconds", "Maximum observed prepared statement execution duration in milliseconds.", "gauge" );
	append_uint64_metric( out, "rathena_sql_prepared_duration_max_milliseconds", snapshot.prepared.aggregate.duration_max_ms );

	// Connection health metrics.
	append_metric_header( out, "rathena_sql_connect_attempts_total", "Total SQL connection attempts.", "counter" );
	append_uint64_metric( out, "rathena_sql_connect_attempts_total", snapshot.connections.connect_attempts_total );

	append_metric_header( out, "rathena_sql_connect_failures_total", "Total SQL connection failures.", "counter" );
	append_uint64_metric( out, "rathena_sql_connect_failures_total", snapshot.connections.connect_failures_total );

	append_metric_header( out, "rathena_sql_ping_total", "Total SQL ping operations.", "counter" );
	append_uint64_metric( out, "rathena_sql_ping_total", snapshot.connections.ping_total );

	append_metric_header( out, "rathena_sql_ping_failures_total", "Total SQL ping failures.", "counter" );
	append_uint64_metric( out, "rathena_sql_ping_failures_total", snapshot.connections.ping_failures_total );

	append_metric_header( out, "rathena_sql_reconnect_events_total", "Total SQL reconnect events.", "counter" );
	append_uint64_metric( out, "rathena_sql_reconnect_events_total", snapshot.connections.reconnect_events_total );

	append_metric_header( out, "rathena_sql_subsystem_overflow_total", "Total SQL observability subsystem label overflows.", "counter" );
	append_uint64_metric( out, "rathena_sql_subsystem_overflow_total", snapshot.connections.subsystem_overflow_total );

	// Per-subsystem metrics in fixed enum order.
	for( size_t i = 0; i < snapshot.queries.by_subsystem.size(); ++i ){
		const SqlObservabilitySubsystem subsystem = static_cast<SqlObservabilitySubsystem>( i );
		const char* label = sql_observability_subsystem_label( subsystem );
		const SqlObservabilityCounters& query = snapshot.queries.by_subsystem[i];
		const SqlObservabilityCounters& prepared = snapshot.prepared.by_subsystem[i];

		append_metric_header( out, "rathena_sql_queries_total", "Total SQL queries executed by subsystem.", "counter" );
		append_labeled_uint64_metric( out, "rathena_sql_queries_total", label, query.attempts_total );

		append_metric_header( out, "rathena_sql_query_failures_total", "Total SQL query failures by subsystem.", "counter" );
		append_labeled_uint64_metric( out, "rathena_sql_query_failures_total", label, query.failures_total );

		append_metric_header( out, "rathena_sql_slow_queries_total", "Total slow SQL queries by subsystem.", "counter" );
		append_labeled_uint64_metric( out, "rathena_sql_slow_queries_total", label, query.slow_total );

		append_metric_header( out, "rathena_sql_query_duration_last_milliseconds", "Duration of the last SQL query by subsystem in milliseconds.", "gauge" );
		append_labeled_uint64_metric( out, "rathena_sql_query_duration_last_milliseconds", label, query.duration_last_ms );

		append_metric_header( out, "rathena_sql_query_duration_max_milliseconds", "Maximum observed SQL query duration by subsystem in milliseconds.", "gauge" );
		append_labeled_uint64_metric( out, "rathena_sql_query_duration_max_milliseconds", label, query.duration_max_ms );

		append_metric_header( out, "rathena_sql_prepared_executions_total", "Total prepared statement executions by subsystem.", "counter" );
		append_labeled_uint64_metric( out, "rathena_sql_prepared_executions_total", label, prepared.attempts_total );

		append_metric_header( out, "rathena_sql_prepared_failures_total", "Total prepared statement execution failures by subsystem.", "counter" );
		append_labeled_uint64_metric( out, "rathena_sql_prepared_failures_total", label, prepared.failures_total );

		append_metric_header( out, "rathena_sql_prepared_slow_total", "Total slow prepared statement executions by subsystem.", "counter" );
		append_labeled_uint64_metric( out, "rathena_sql_prepared_slow_total", label, prepared.slow_total );

		append_metric_header( out, "rathena_sql_prepared_duration_last_milliseconds", "Duration of the last prepared statement execution by subsystem in milliseconds.", "gauge" );
		append_labeled_uint64_metric( out, "rathena_sql_prepared_duration_last_milliseconds", label, prepared.duration_last_ms );

		append_metric_header( out, "rathena_sql_prepared_duration_max_milliseconds", "Maximum observed prepared statement execution duration by subsystem in milliseconds.", "gauge" );
		append_labeled_uint64_metric( out, "rathena_sql_prepared_duration_max_milliseconds", label, prepared.duration_max_ms );
	}

	return out;
}

#endif // SQL_OBSERVABILITY_PURE_HPP
