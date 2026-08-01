// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#include "sql_observability.hpp"

#include <cstdlib>

#include <common/showmsg.hpp>

#include "sql_observability_internal.hpp"
#include "sql_observability_pure.hpp"

namespace {

constexpr const char* ENV_ENABLE = "RATHENA_SQL_OBSERVABILITY";
constexpr const char* ENV_SLOW_MS = "RATHENA_SQL_OBSERVABILITY_SLOW_MS";
constexpr const char* ENV_MAX_SUBSYSTEMS = "RATHENA_SQL_OBSERVABILITY_MAX_SUBSYSTEMS";

constexpr uint32_t DEFAULT_SLOW_MS = 50;
constexpr uint32_t MIN_SLOW_MS = 1;
constexpr uint32_t MAX_SLOW_MS = 60000;

constexpr uint32_t DEFAULT_MAX_SUBSYSTEMS = 16;
constexpr uint32_t MIN_MAX_SUBSYSTEMS = 4;
constexpr uint32_t MAX_MAX_SUBSYSTEMS = 64;

} // namespace

sql_observability_state g_sql_observability_state;

void sql_observability_init(){
	// Guard against duplicate initialization (e.g. after script reloads).
	if( g_sql_observability_state.initialized ){
		return;
	}

	const char* raw_enable = std::getenv( ENV_ENABLE );
	const char* raw_slow_ms = std::getenv( ENV_SLOW_MS );
	const char* raw_max_subsystems = std::getenv( ENV_MAX_SUBSYSTEMS );

	const bool enable = sql_observability_parse_bool( raw_enable, false );

	if( !is_valid_bool_setting( raw_enable ) ){
		ShowWarning( "sql_observability: invalid %s value, instrumentation stays disabled.\n", ENV_ENABLE );
	}

	const uint32_t slow_ms = sql_observability_parse_u32( raw_slow_ms, DEFAULT_SLOW_MS, MIN_SLOW_MS, MAX_SLOW_MS );

	if( !is_valid_u32_setting( raw_slow_ms ) ){
		ShowWarning( "sql_observability: invalid %s value, falling back to the default.\n", ENV_SLOW_MS );
	}

	const uint32_t max_subsystems = sql_observability_parse_u32( raw_max_subsystems, DEFAULT_MAX_SUBSYSTEMS, MIN_MAX_SUBSYSTEMS, MAX_MAX_SUBSYSTEMS );

	if( !is_valid_u32_setting( raw_max_subsystems ) ){
		ShowWarning( "sql_observability: invalid %s value, falling back to the default.\n", ENV_MAX_SUBSYSTEMS );
	}

	g_sql_observability_state.enabled = enable;
	g_sql_observability_state.slow_threshold_ms = slow_ms;
	g_sql_observability_state.max_subsystems = static_cast<size_t>( max_subsystems );
	g_sql_observability_state.current_subsystem = SqlObservabilitySubsystem::Unknown;
	g_sql_observability_state.initialized = true;

	if( enable ){
		ShowInfo( "sql_observability: instrumentation enabled (slow_threshold=%u ms, max_subsystems=%u).\n", slow_ms, max_subsystems );
	}
}

void sql_observability_final(){
	g_sql_observability_state.enabled = false;
	g_sql_observability_state.initialized = false;
	g_sql_observability_state.current_subsystem = SqlObservabilitySubsystem::Unknown;
}

bool sql_observability_enabled(){
	return g_sql_observability_state.enabled;
}

void sql_observability_set_subsystem( SqlObservabilitySubsystem subsystem ){
	g_sql_observability_state.current_subsystem = subsystem;
}

SqlObservabilitySubsystem sql_observability_get_subsystem(){
	return g_sql_observability_state.current_subsystem;
}

void sql_observability_record_query( uint64_t duration_ms, bool success ){
	if( !sql_observability_enabled() ){
		return;
	}

	g_sql_observability_state.snapshot.queries.record_query( g_sql_observability_state.current_subsystem, duration_ms, success, g_sql_observability_state.slow_threshold_ms );
}

void sql_observability_record_prepared( uint64_t duration_ms, bool success ){
	if( !sql_observability_enabled() ){
		return;
	}

	g_sql_observability_state.snapshot.prepared.record_prepared( g_sql_observability_state.current_subsystem, duration_ms, success, g_sql_observability_state.slow_threshold_ms );
}

void sql_observability_record_connect( bool success ){
	if( !sql_observability_enabled() ){
		return;
	}

	g_sql_observability_state.snapshot.connections.record_connect( success );
}

void sql_observability_record_ping( bool success ){
	if( !sql_observability_enabled() ){
		return;
	}

	g_sql_observability_state.snapshot.connections.record_ping( success );
}

void sql_observability_record_reconnect(){
	if( !sql_observability_enabled() ){
		return;
	}

	g_sql_observability_state.snapshot.connections.record_reconnect();
}

std::string sql_observability_render_prometheus(){
	return sql_observability_render_prometheus( g_sql_observability_state.snapshot );
}

#ifdef RATHENA_SQL_OBSERVABILITY_TESTING

void sql_observability_test_reset( bool enabled, uint32_t slow_ms, size_t max_subsystems ){
	g_sql_observability_state.enabled = enabled;
	g_sql_observability_state.initialized = false;
	g_sql_observability_state.slow_threshold_ms = slow_ms;
	g_sql_observability_state.max_subsystems = max_subsystems;
	g_sql_observability_state.current_subsystem = SqlObservabilitySubsystem::Unknown;
	g_sql_observability_state.snapshot = SqlObservabilitySnapshot();
}

SqlObservabilitySnapshot sql_observability_test_snapshot(){
	return g_sql_observability_state.snapshot;
}

#endif // RATHENA_SQL_OBSERVABILITY_TESTING
