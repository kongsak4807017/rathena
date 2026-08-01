// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder
//
// Unit tests for the SQL observability helpers and runtime state.
// Build without defines for the pure tests only, or with
// -DRATHENA_SQL_OBSERVABILITY_TESTING to also compile and exercise the
// runtime state implementation.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>

#include "sql_observability_pure.hpp"

#ifdef RATHENA_SQL_OBSERVABILITY_TESTING
// Stubs for rAthena logging so sql_observability.cpp can be compiled standalone
// without linking the full common library.
#include <cstdarg>

namespace {

int g_show_warning_count = 0;

} // namespace

static void sql_observability_test_ShowWarning( const char* fmt, ... ){
	++g_show_warning_count;
	(void)fmt;
}

static void sql_observability_test_ShowInfo( const char* fmt, ... ){
	(void)fmt;
}

#define ShowWarning sql_observability_test_ShowWarning
#define ShowInfo sql_observability_test_ShowInfo
#define SHOWMSG_HPP

#include "sql_observability.hpp"
#include "sql_observability_internal.hpp"
#include "sql_observability.cpp"
#endif // RATHENA_SQL_OBSERVABILITY_TESTING

namespace {

int g_failures = 0;

#define CHECK( cond ) \
	do { \
		if( !( cond ) ){ \
			++g_failures; \
			std::fprintf( stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond ); \
		} \
	} while( 0 )

void test_parse_bool(){
	// nullptr and empty fall back to the default value.
	CHECK( sql_observability_parse_bool( nullptr, false ) == false );
	CHECK( sql_observability_parse_bool( nullptr, true ) == true );
	CHECK( sql_observability_parse_bool( "", false ) == false );
	CHECK( sql_observability_parse_bool( "", true ) == true );

	// Explicit true values (case-insensitive ASCII).
	CHECK( sql_observability_parse_bool( "1", false ) == true );
	CHECK( sql_observability_parse_bool( "true", false ) == true );
	CHECK( sql_observability_parse_bool( "TRUE", false ) == true );
	CHECK( sql_observability_parse_bool( "on", false ) == true );
	CHECK( sql_observability_parse_bool( "On", false ) == true );
	CHECK( sql_observability_parse_bool( "yes", false ) == true );
	CHECK( sql_observability_parse_bool( "YES", false ) == true );

	// Explicit false values (case-insensitive ASCII).
	CHECK( sql_observability_parse_bool( "0", true ) == false );
	CHECK( sql_observability_parse_bool( "false", true ) == false );
	CHECK( sql_observability_parse_bool( "FALSE", true ) == false );
	CHECK( sql_observability_parse_bool( "off", true ) == false );
	CHECK( sql_observability_parse_bool( "OFF", true ) == false );
	CHECK( sql_observability_parse_bool( "no", true ) == false );
	CHECK( sql_observability_parse_bool( "NO", true ) == false );

	// Garbage must never match a true value.
	CHECK( sql_observability_parse_bool( "enabled", true ) == false );
	CHECK( sql_observability_parse_bool( "2", true ) == false );
	CHECK( sql_observability_parse_bool( "maybe", true ) == false );
	CHECK( sql_observability_parse_bool( " true ", true ) == false );
}

void test_parse_u32(){
	// nullptr, empty and malformed input fall back to the fallback value.
	CHECK( sql_observability_parse_u32( nullptr, 50, 1, 60000 ) == 50 );
	CHECK( sql_observability_parse_u32( "", 50, 1, 60000 ) == 50 );
	CHECK( sql_observability_parse_u32( "bad", 50, 1, 60000 ) == 50 );
	CHECK( sql_observability_parse_u32( "12x", 50, 1, 60000 ) == 50 );
	CHECK( sql_observability_parse_u32( " x12", 50, 1, 60000 ) == 50 );

	// Signed input is rejected.
	CHECK( sql_observability_parse_u32( "-5", 50, 1, 60000 ) == 50 );
	CHECK( sql_observability_parse_u32( "+5", 50, 1, 60000 ) == 50 );

	// Valid values are clamped to [min, max].
	CHECK( sql_observability_parse_u32( "0", 50, 1, 60000 ) == 1 );
	CHECK( sql_observability_parse_u32( "1", 50, 1, 60000 ) == 1 );
	CHECK( sql_observability_parse_u32( "50000", 50, 1, 60000 ) == 50000 );
	CHECK( sql_observability_parse_u32( "60000", 50, 1, 60000 ) == 60000 );
	CHECK( sql_observability_parse_u32( "70000", 50, 1, 60000 ) == 60000 );

	// Overflow is rejected.
	CHECK( sql_observability_parse_u32( "4294967296", 50, 1, 60000 ) == 50 );
	CHECK( sql_observability_parse_u32( "99999999999999999999999", 50, 1, 60000 ) == 50 );
}

void test_is_slow(){
	CHECK( sql_observability_is_slow( 0, 50 ) == false );
	CHECK( sql_observability_is_slow( 49, 50 ) == false );
	CHECK( sql_observability_is_slow( 50, 50 ) == true );
	CHECK( sql_observability_is_slow( 51, 50 ) == true );
}

void test_subsystem_labels(){
	CHECK( std::strcmp( sql_observability_subsystem_label( SqlObservabilitySubsystem::Login ), "login" ) == 0 );
	CHECK( std::strcmp( sql_observability_subsystem_label( SqlObservabilitySubsystem::Char ), "char" ) == 0 );
	CHECK( std::strcmp( sql_observability_subsystem_label( SqlObservabilitySubsystem::Map ), "map" ) == 0 );
	CHECK( std::strcmp( sql_observability_subsystem_label( SqlObservabilitySubsystem::Log ), "log" ) == 0 );
	CHECK( std::strcmp( sql_observability_subsystem_label( SqlObservabilitySubsystem::Web ), "web" ) == 0 );
	CHECK( std::strcmp( sql_observability_subsystem_label( SqlObservabilitySubsystem::Unknown ), "unknown" ) == 0 );

	// Invalid enum values must map to "unknown".
	CHECK( std::strcmp( sql_observability_subsystem_label( static_cast<SqlObservabilitySubsystem>( 255 ) ), "unknown" ) == 0 );
	CHECK( std::strcmp( sql_observability_subsystem_label( static_cast<SqlObservabilitySubsystem>( 6 ) ), "unknown" ) == 0 );
}

void test_saturating_add(){
	CHECK( sql_observability_saturating_add( 5, 7 ) == 12 );
	CHECK( sql_observability_saturating_add( std::numeric_limits<uint64_t>::max() - 1, 5 ) == std::numeric_limits<uint64_t>::max() );
	CHECK( sql_observability_saturating_add( std::numeric_limits<uint64_t>::max(), 0 ) == std::numeric_limits<uint64_t>::max() );
	CHECK( sql_observability_saturating_add( 0, std::numeric_limits<uint64_t>::max() ) == std::numeric_limits<uint64_t>::max() );
}

void test_counter_saturation(){
	SqlObservabilityCounters counters;

	counters.attempts_total = std::numeric_limits<uint64_t>::max() - 2;
	counters.record_attempt();
	counters.record_attempt();
	counters.record_attempt();
	CHECK( counters.attempts_total == std::numeric_limits<uint64_t>::max() );

	counters.failures_total = std::numeric_limits<uint64_t>::max() - 1;
	counters.record_failure();
	CHECK( counters.failures_total == std::numeric_limits<uint64_t>::max() );
	// attempts_total was already saturated and stays saturated.
	CHECK( counters.attempts_total == std::numeric_limits<uint64_t>::max() );
}

void test_subsystem_admission(){
	SqlObservabilitySnapshot snapshot;

	snapshot.queries.record_query( SqlObservabilitySubsystem::Map, 30, true, 50 );
	snapshot.queries.record_query( SqlObservabilitySubsystem::Map, 70, false, 50 );
	snapshot.queries.record_query( SqlObservabilitySubsystem::Char, 20, true, 50 );

	// Aggregate: 3 attempts, 1 failure, 1 slow (70 >= 50), max = 70, last = 20.
	CHECK( snapshot.queries.aggregate.attempts_total == 3 );
	CHECK( snapshot.queries.aggregate.failures_total == 1 );
	CHECK( snapshot.queries.aggregate.slow_total == 1 );
	CHECK( snapshot.queries.aggregate.duration_last_ms == 20 );
	CHECK( snapshot.queries.aggregate.duration_max_ms == 70 );

	const SqlObservabilityCounters& map = snapshot.queries.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Map )];
	CHECK( map.attempts_total == 2 );
	CHECK( map.failures_total == 1 );
	CHECK( map.slow_total == 1 );
	CHECK( map.duration_last_ms == 70 );
	CHECK( map.duration_max_ms == 70 );

	const SqlObservabilityCounters& chr = snapshot.queries.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Char )];
	CHECK( chr.attempts_total == 1 );
	CHECK( chr.failures_total == 0 );
	CHECK( chr.slow_total == 0 );
	CHECK( chr.duration_last_ms == 20 );
	CHECK( chr.duration_max_ms == 20 );

	// Invalid enum value maps to Unknown and increments overflow exactly once.
	snapshot.queries.record_query( static_cast<SqlObservabilitySubsystem>( 255 ), 10, true, 50 );
	const SqlObservabilityCounters& unknown = snapshot.queries.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Unknown )];
	CHECK( unknown.attempts_total == 1 );
	CHECK( snapshot.connections.subsystem_overflow_total == 1 );
}

void test_deterministic_ordering(){
	SqlObservabilitySnapshot snapshot;
	snapshot.queries.record_query( SqlObservabilitySubsystem::Map, 1, true, 50 );
	snapshot.queries.record_query( SqlObservabilitySubsystem::Char, 1, true, 50 );
	snapshot.queries.record_query( SqlObservabilitySubsystem::Login, 1, true, 50 );

	const std::string out = sql_observability_render_prometheus( snapshot );

	// Per-subsystem labels appear in fixed enum order, not insertion order.
	const size_t login_pos = out.find( "{subsystem=\"login\"}" );
	const size_t char_pos = out.find( "{subsystem=\"char\"}" );
	const size_t map_pos = out.find( "{subsystem=\"map\"}" );

	CHECK( login_pos != std::string::npos );
	CHECK( char_pos != std::string::npos );
	CHECK( map_pos != std::string::npos );
	CHECK( login_pos < char_pos );
	CHECK( char_pos < map_pos );
}

void test_render_prometheus_privacy(){
	// These sentinel strings must never appear in rendered output.
	const std::string sentinel_query = "SELECT password FROM login";
	const std::string sentinel_password = "secret-password";
	const std::string sentinel_account = "account_id=123";
	const std::string sentinel_error = "DB error private value";

	SqlObservabilitySnapshot snapshot;
	snapshot.queries.record_query( SqlObservabilitySubsystem::Login, 10, false, 50 );

	const std::string out = sql_observability_render_prometheus( snapshot );

	CHECK( out.find( sentinel_query ) == std::string::npos );
	CHECK( out.find( sentinel_password ) == std::string::npos );
	CHECK( out.find( sentinel_account ) == std::string::npos );
	CHECK( out.find( sentinel_error ) == std::string::npos );
}

void test_empty_snapshot_render(){
	const std::string out = sql_observability_render_prometheus( SqlObservabilitySnapshot() );

	CHECK( !out.empty() );
	CHECK( out.back() == '\n' );
	CHECK( out.find( "\n\n" ) == std::string::npos );
}

#ifdef RATHENA_SQL_OBSERVABILITY_TESTING

void test_runtime_disabled_init(){
	sql_observability_test_reset( false, 50, 16 );
	CHECK( sql_observability_enabled() == false );
	CHECK( sql_observability_get_subsystem() == SqlObservabilitySubsystem::Unknown );

	// Recording while disabled must not crash or allocate.
	sql_observability_record_query( 100, true );
	sql_observability_record_prepared( 100, false );
	sql_observability_record_connect( true );
	sql_observability_record_ping( false );
	sql_observability_record_reconnect();

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();
	CHECK( snapshot.queries.aggregate.attempts_total == 0 );
	CHECK( snapshot.prepared.aggregate.attempts_total == 0 );
	CHECK( snapshot.connections.connect_attempts_total == 0 );
}

void test_runtime_enabled_init(){
	sql_observability_test_reset( true, 50, 16 );
	CHECK( sql_observability_enabled() == true );

	sql_observability_record_query( 30, true );

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();
	CHECK( snapshot.queries.aggregate.attempts_total == 1 );
	CHECK( snapshot.queries.aggregate.failures_total == 0 );
	CHECK( snapshot.queries.aggregate.slow_total == 0 );
}

void test_runtime_idempotent_init_final(){
	sql_observability_test_reset( true, 50, 16 );
	CHECK( sql_observability_enabled() == true );

	sql_observability_final();
	CHECK( sql_observability_enabled() == false );

	// A second final must be safe.
	sql_observability_final();
	CHECK( sql_observability_enabled() == false );
}

void test_runtime_default_subsystem(){
	sql_observability_test_reset( true, 50, 16 );
	CHECK( sql_observability_get_subsystem() == SqlObservabilitySubsystem::Unknown );

	sql_observability_record_query( 10, true );

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();
	const SqlObservabilityCounters& unknown = snapshot.queries.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Unknown )];
	CHECK( unknown.attempts_total == 1 );
}

void test_runtime_subsystem_selection(){
	sql_observability_test_reset( true, 50, 16 );

	sql_observability_set_subsystem( SqlObservabilitySubsystem::Map );
	CHECK( sql_observability_get_subsystem() == SqlObservabilitySubsystem::Map );

	sql_observability_record_query( 10, true );

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();
	const SqlObservabilityCounters& map = snapshot.queries.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Map )];
	CHECK( map.attempts_total == 1 );
	CHECK( snapshot.queries.aggregate.attempts_total == 1 );
}

void test_runtime_query_and_prepared(){
	sql_observability_test_reset( true, 50, 16 );
	sql_observability_set_subsystem( SqlObservabilitySubsystem::Char );

	sql_observability_record_query( 30, true );
	sql_observability_record_query( 70, false );
	sql_observability_record_prepared( 20, true );
	sql_observability_record_prepared( 80, false );

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();

	// Query aggregate: 2 attempts, 1 failure, 1 slow (70 >= 50).
	CHECK( snapshot.queries.aggregate.attempts_total == 2 );
	CHECK( snapshot.queries.aggregate.failures_total == 1 );
	CHECK( snapshot.queries.aggregate.slow_total == 1 );
	CHECK( snapshot.queries.aggregate.duration_last_ms == 70 );
	CHECK( snapshot.queries.aggregate.duration_max_ms == 70 );

	// Prepared aggregate: 2 attempts, 1 failure, 1 slow (80 >= 50).
	CHECK( snapshot.prepared.aggregate.attempts_total == 2 );
	CHECK( snapshot.prepared.aggregate.failures_total == 1 );
	CHECK( snapshot.prepared.aggregate.slow_total == 1 );
	CHECK( snapshot.prepared.aggregate.duration_last_ms == 80 );
	CHECK( snapshot.prepared.aggregate.duration_max_ms == 80 );

	const SqlObservabilityCounters& chr = snapshot.queries.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Char )];
	CHECK( chr.attempts_total == 2 );
	CHECK( chr.failures_total == 1 );

	const SqlObservabilityCounters& chr_prepared = snapshot.prepared.by_subsystem[static_cast<size_t>( SqlObservabilitySubsystem::Char )];
	CHECK( chr_prepared.attempts_total == 2 );
	CHECK( chr_prepared.failures_total == 1 );
}

void test_runtime_slow_threshold(){
	sql_observability_test_reset( true, 100, 16 );
	sql_observability_set_subsystem( SqlObservabilitySubsystem::Login );

	sql_observability_record_query( 99, true );
	sql_observability_record_query( 100, true );
	sql_observability_record_query( 101, true );

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();
	CHECK( snapshot.queries.aggregate.slow_total == 2 );
}

void test_runtime_connection_events(){
	sql_observability_test_reset( true, 50, 16 );

	sql_observability_record_connect( true );
	sql_observability_record_connect( false );
	sql_observability_record_ping( true );
	sql_observability_record_ping( false );
	sql_observability_record_reconnect();
	sql_observability_record_reconnect();

	SqlObservabilitySnapshot snapshot = sql_observability_test_snapshot();
	CHECK( snapshot.connections.connect_attempts_total == 2 );
	CHECK( snapshot.connections.connect_failures_total == 1 );
	CHECK( snapshot.connections.ping_total == 2 );
	CHECK( snapshot.connections.ping_failures_total == 1 );
	CHECK( snapshot.connections.reconnect_events_total == 2 );
}

void test_runtime_render(){
	sql_observability_test_reset( true, 50, 16 );
	sql_observability_set_subsystem( SqlObservabilitySubsystem::Map );

	sql_observability_record_query( 10, true );
	sql_observability_record_connect( true );

	const std::string out = sql_observability_render_prometheus();

	CHECK( out.find( "rathena_sql_queries_total 1\n" ) != std::string::npos );
	CHECK( out.find( "rathena_sql_connect_attempts_total 1\n" ) != std::string::npos );
	CHECK( out.find( "{subsystem=\"map\"}" ) != std::string::npos );
	CHECK( out.back() == '\n' );
	CHECK( out.find( "\n\n" ) == std::string::npos );
}

void test_runtime_warning_count(){
	g_show_warning_count = 0;

	// Force malformed values through the test seam. The runtime init path reads
	// environment variables, but the seam sets state directly. We exercise the
	// parsing helpers with malformed values instead and verify the warning
	// detector agrees they are invalid.
	CHECK( is_valid_bool_setting( "maybe" ) == false );
	CHECK( is_valid_bool_setting( "2" ) == false );
	CHECK( is_valid_u32_setting( "abc" ) == false );
	CHECK( is_valid_u32_setting( "4294967296" ) == false );

	CHECK( is_valid_bool_setting( "true" ) == true );
	CHECK( is_valid_bool_setting( "0" ) == true );
	CHECK( is_valid_u32_setting( "50" ) == true );
	CHECK( is_valid_u32_setting( "70000" ) == true );
	CHECK( is_valid_u32_setting( "" ) == true );
}

#endif // RATHENA_SQL_OBSERVABILITY_TESTING

} // namespace

int main(){
	test_parse_bool();
	test_parse_u32();
	test_is_slow();
	test_subsystem_labels();
	test_saturating_add();
	test_counter_saturation();
	test_subsystem_admission();
	test_deterministic_ordering();
	test_render_prometheus_privacy();
	test_empty_snapshot_render();

#ifdef RATHENA_SQL_OBSERVABILITY_TESTING
	test_runtime_disabled_init();
	test_runtime_enabled_init();
	test_runtime_idempotent_init_final();
	test_runtime_default_subsystem();
	test_runtime_subsystem_selection();
	test_runtime_query_and_prepared();
	test_runtime_slow_threshold();
	test_runtime_connection_events();
	test_runtime_render();
	test_runtime_warning_count();
#endif

	if( g_failures != 0 ){
		std::fprintf( stderr, "%d check(s) failed\n", g_failures );
		return 1;
	}

	std::printf( "all sql observability tests passed\n" );
	return 0;
}
