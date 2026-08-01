// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder.
//
// Unit tests for the script observability helpers.
// Build without defines for the pure tests only, or with
// -DRATHENA_SCRIPT_OBSERVABILITY_TESTING to also compile and exercise the
// runtime state implementation.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>

#include "script_observability_pure.hpp"

#ifdef RATHENA_SCRIPT_OBSERVABILITY_TESTING
// Stubs for rAthena logging so script_observability.cpp can be compiled standalone
// without linking the full common library.
#include <cstdarg>

namespace {

int g_show_warning_count = 0;

} // namespace

static void script_observability_test_ShowWarning( const char* fmt, ... ){
	++g_show_warning_count;
	(void)fmt;
}

static void script_observability_test_ShowInfo( const char* fmt, ... ){
	(void)fmt;
}

#define ShowWarning script_observability_test_ShowWarning
#define ShowInfo script_observability_test_ShowInfo
#define SHOWMSG_HPP

#include "script_observability.hpp"
#include "script_observability_internal.hpp"
#include "script_observability.cpp"
#endif // RATHENA_SCRIPT_OBSERVABILITY_TESTING

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
	CHECK( script_observability_parse_bool( nullptr, false ) == false );
	CHECK( script_observability_parse_bool( nullptr, true ) == true );
	CHECK( script_observability_parse_bool( "", false ) == false );
	CHECK( script_observability_parse_bool( "", true ) == true );

	// Explicit true values (case-insensitive ASCII).
	CHECK( script_observability_parse_bool( "1", false ) == true );
	CHECK( script_observability_parse_bool( "true", false ) == true );
	CHECK( script_observability_parse_bool( "TRUE", false ) == true );
	CHECK( script_observability_parse_bool( "on", false ) == true );
	CHECK( script_observability_parse_bool( "On", false ) == true );
	CHECK( script_observability_parse_bool( "yes", false ) == true );
	CHECK( script_observability_parse_bool( "YES", false ) == true );

	// Explicit false values (case-insensitive ASCII).
	CHECK( script_observability_parse_bool( "0", true ) == false );
	CHECK( script_observability_parse_bool( "false", true ) == false );
	CHECK( script_observability_parse_bool( "FALSE", true ) == false );
	CHECK( script_observability_parse_bool( "off", true ) == false );
	CHECK( script_observability_parse_bool( "OFF", true ) == false );
	CHECK( script_observability_parse_bool( "no", true ) == false );
	CHECK( script_observability_parse_bool( "NO", true ) == false );

	// Garbage must never match a true value.
	CHECK( script_observability_parse_bool( "enabled", true ) == false );
	CHECK( script_observability_parse_bool( "2", true ) == false );
	CHECK( script_observability_parse_bool( "maybe", true ) == false );
	CHECK( script_observability_parse_bool( " true ", true ) == false );
}

void test_parse_u32(){
	// nullptr, empty and malformed input fall back to the fallback value.
	CHECK( script_observability_parse_u32( nullptr, 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( "", 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( "bad", 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( "12x", 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( " x12", 25, 1, 60000 ) == 25 );

	// Signed input is rejected.
	CHECK( script_observability_parse_u32( "-5", 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( "+5", 25, 1, 60000 ) == 25 );

	// Valid values are clamped to [min, max].
	CHECK( script_observability_parse_u32( "0", 25, 1, 60000 ) == 1 );
	CHECK( script_observability_parse_u32( "1", 25, 1, 60000 ) == 1 );
	CHECK( script_observability_parse_u32( "25", 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( "50000", 25, 1, 60000 ) == 50000 );
	CHECK( script_observability_parse_u32( "60000", 25, 1, 60000 ) == 60000 );
	CHECK( script_observability_parse_u32( "70000", 25, 1, 60000 ) == 60000 );

	// Overflow beyond uint32_t is rejected.
	CHECK( script_observability_parse_u32( "4294967296", 25, 1, 60000 ) == 25 );
	CHECK( script_observability_parse_u32( "99999999999999999999999", 25, 1, 60000 ) == 25 );
}

void test_is_slow(){
	CHECK( script_observability_is_slow( 0, 25 ) == false );
	CHECK( script_observability_is_slow( 24, 25 ) == false );
	CHECK( script_observability_is_slow( 25, 25 ) == true );
	CHECK( script_observability_is_slow( 26, 25 ) == true );
}

void test_category_labels(){
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Npc ), "npc" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Event ), "event" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Timer ), "timer" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Item ), "item" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Skill ), "skill" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Quest ), "quest" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Instance ), "instance" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( ScriptObservabilityCategory::Unknown ), "unknown" ) == 0 );

	// Invalid enum values must map to "unknown".
	CHECK( std::strcmp( script_observability_category_label( static_cast<ScriptObservabilityCategory>( 255 ) ), "unknown" ) == 0 );
	CHECK( std::strcmp( script_observability_category_label( static_cast<ScriptObservabilityCategory>( 8 ) ), "unknown" ) == 0 );
}

void test_saturating_add(){
	CHECK( script_observability_saturating_add( 5, 7 ) == 12 );
	CHECK( script_observability_saturating_add( std::numeric_limits<uint64_t>::max() - 1, 5 ) == std::numeric_limits<uint64_t>::max() );
	CHECK( script_observability_saturating_add( std::numeric_limits<uint64_t>::max(), 0 ) == std::numeric_limits<uint64_t>::max() );
	CHECK( script_observability_saturating_add( 0, std::numeric_limits<uint64_t>::max() ) == std::numeric_limits<uint64_t>::max() );
}

void test_record_slice_aggregate(){
	ScriptObservabilitySnapshot snapshot;

	snapshot.record_slice( ScriptObservabilityCategory::Npc, 10, 5, false, 25 );
	snapshot.record_slice( ScriptObservabilityCategory::Event, 25, 12, false, 25 );
	snapshot.record_slice( ScriptObservabilityCategory::Timer, 30, 20, true, 25 );

	CHECK( snapshot.aggregate.execution_slices_total == 3 );
	CHECK( snapshot.aggregate.execution_failures_total == 1 );
	CHECK( snapshot.aggregate.slow_execution_slices_total == 2 );
	CHECK( snapshot.aggregate.execution_duration_last_ms == 30 );
	CHECK( snapshot.aggregate.execution_duration_max_ms == 30 );
	CHECK( snapshot.aggregate.commands_total == 37 );
	CHECK( snapshot.aggregate.commands_max_per_slice == 20 );
}

void test_record_slice_per_category(){
	ScriptObservabilitySnapshot snapshot;

	snapshot.record_slice( ScriptObservabilityCategory::Item, 15, 8, false, 25 );
	snapshot.record_slice( ScriptObservabilityCategory::Item, 35, 10, true, 25 );

	const ScriptObservabilityCounters& item = snapshot.by_category[static_cast<size_t>( ScriptObservabilityCategory::Item )];
	CHECK( item.execution_slices_total == 2 );
	CHECK( item.execution_failures_total == 1 );
	CHECK( item.slow_execution_slices_total == 1 );
	CHECK( item.execution_duration_last_ms == 35 );
	CHECK( item.execution_duration_max_ms == 35 );
	CHECK( item.commands_total == 18 );
	CHECK( item.commands_max_per_slice == 10 );
}

void test_invalid_category_fallback(){
	ScriptObservabilitySnapshot snapshot;

	snapshot.record_slice( static_cast<ScriptObservabilityCategory>( 255 ), 10, 1, false, 25 );

	CHECK( snapshot.by_category[static_cast<size_t>( ScriptObservabilityCategory::Unknown )].execution_slices_total == 1 );
	CHECK( snapshot.aggregate.execution_slices_total == 1 );
}

void test_counter_saturation(){
	ScriptObservabilityCounters counters;

	counters.execution_slices_total = std::numeric_limits<uint64_t>::max() - 1;
	counters.record_slice( 10, 1, false, 25 );
	counters.record_slice( 10, 1, false, 25 );
	CHECK( counters.execution_slices_total == std::numeric_limits<uint64_t>::max() );

	counters.commands_total = std::numeric_limits<uint64_t>::max() - 5;
	counters.record_slice( 10, 10, false, 25 );
	CHECK( counters.commands_total == std::numeric_limits<uint64_t>::max() );
}

void test_rendering_deterministic_and_complete(){
	ScriptObservabilitySnapshot snapshot;

	snapshot.record_slice( ScriptObservabilityCategory::Npc, 25, 17, false, 25 );
	snapshot.record_slice( ScriptObservabilityCategory::Event, 24, 3, true, 25 );
	snapshot.record_slice( ScriptObservabilityCategory::Timer, 26, 9, false, 25 );

	const std::string out = script_observability_render_prometheus( snapshot );

	CHECK( out.find( "rathena_script_execution_slices_total " ) != std::string::npos );
	CHECK( out.find( "rathena_script_execution_failures_total " ) != std::string::npos );
	CHECK( out.find( "rathena_script_slow_execution_slices_total " ) != std::string::npos );
	CHECK( out.find( "rathena_script_execution_duration_last_milliseconds " ) != std::string::npos );
	CHECK( out.find( "rathena_script_execution_duration_max_milliseconds " ) != std::string::npos );
	CHECK( out.find( "rathena_script_commands_total " ) != std::string::npos );
	CHECK( out.find( "rathena_script_commands_max_per_slice " ) != std::string::npos );

	CHECK( out.find( "rathena_script_execution_slices_total{category=\"npc\"}" ) != std::string::npos );
	CHECK( out.find( "rathena_script_execution_failures_total{category=\"event\"}" ) != std::string::npos );
	CHECK( out.find( "rathena_script_slow_execution_slices_total{category=\"timer\"}" ) != std::string::npos );
	CHECK( out.find( "rathena_script_commands_total{category=\"npc\"}" ) != std::string::npos );
	CHECK( out.find( "rathena_script_execution_duration_max_milliseconds{category=\"timer\"}" ) != std::string::npos );

	// Deterministic: render twice and compare.
	CHECK( script_observability_render_prometheus( snapshot ) == out );
}

void test_privacy_sentinel_scan(){
	ScriptObservabilitySnapshot snapshot;

	snapshot.record_slice( ScriptObservabilityCategory::Npc, 10, 1, false, 25 );

	const std::string out = script_observability_render_prometheus( snapshot );

	CHECK( out.find( "Prontera" ) == std::string::npos );
	CHECK( out.find( "npc_name" ) == std::string::npos );
	CHECK( out.find( "OnLabel" ) == std::string::npos );
	CHECK( out.find( ".txt" ) == std::string::npos );
	CHECK( out.find( "mes " ) == std::string::npos );
	CHECK( out.find( "account_id" ) == std::string::npos );
}

} // namespace

int main(){
	test_parse_bool();
	test_parse_u32();
	test_is_slow();
	test_category_labels();
	test_saturating_add();
	test_record_slice_aggregate();
	test_record_slice_per_category();
	test_invalid_category_fallback();
	test_counter_saturation();
	test_rendering_deterministic_and_complete();
	test_privacy_sentinel_scan();

	if( g_failures > 0 ){
		std::fprintf( stderr, "%d test(s) failed.\n", g_failures );
		return 1;
	}

	std::fprintf( stdout, "All script observability pure tests passed.\n" );
	return 0;
}