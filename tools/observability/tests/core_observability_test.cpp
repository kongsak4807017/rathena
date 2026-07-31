// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder
//
// Unit tests for the pure (dependency-free) core observability helpers.
// These tests must compile and run without a full map-server.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>

#ifdef _WIN32
#include <direct.h>
#else
#include <unistd.h>
#endif

#include "core_observability_pure.hpp"

namespace {

int g_failures = 0;

#define CHECK( cond ) \
	do { \
		if( !( cond ) ){ \
			++g_failures; \
			std::fprintf( stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond ); \
		} \
	} while( 0 )

std::string read_file( const std::string& path ){
	std::ifstream in( path, std::ios::binary );
	std::ostringstream ss;
	ss << in.rdbuf();
	return ss.str();
}

bool file_exists( const std::string& path ){
	std::ifstream in( path, std::ios::binary );
	return in.good();
}

void test_boolean_env_parsing(){
	using core_observability::parse_enabled;

	// unset / empty must be disabled (default off)
	CHECK( parse_enabled( nullptr ) == false );
	CHECK( parse_enabled( "" ) == false );

	// explicit true values (case-insensitive)
	CHECK( parse_enabled( "1" ) == true );
	CHECK( parse_enabled( "true" ) == true );
	CHECK( parse_enabled( "TRUE" ) == true );
	CHECK( parse_enabled( "on" ) == true );
	CHECK( parse_enabled( "On" ) == true );
	CHECK( parse_enabled( "yes" ) == true );
	CHECK( parse_enabled( "YES" ) == true );

	// explicit false values
	CHECK( parse_enabled( "0" ) == false );
	CHECK( parse_enabled( "false" ) == false );
	CHECK( parse_enabled( "off" ) == false );
	CHECK( parse_enabled( "no" ) == false );

	// garbage must never enable instrumentation
	CHECK( parse_enabled( "enabled" ) == false );
	CHECK( parse_enabled( "2" ) == false );
	CHECK( parse_enabled( "maybe" ) == false );
}

void test_interval_parsing(){
	using core_observability::parse_interval_ms;
	using core_observability::default_interval_ms;

	// unset / empty falls back to the default and is not a user error
	CHECK( parse_interval_ms( nullptr ).value_ms == default_interval_ms );
	CHECK( parse_interval_ms( nullptr ).valid == true );
	CHECK( parse_interval_ms( "" ).value_ms == default_interval_ms );
	CHECK( parse_interval_ms( "" ).valid == true );

	// regular values pass through
	core_observability::interval_parse_result r = parse_interval_ms( "5000" );
	CHECK( r.valid == true );
	CHECK( r.value_ms == 5000 );

	// surrounding whitespace is tolerated
	r = parse_interval_ms( "  2500  " );
	CHECK( r.valid == true );
	CHECK( r.value_ms == 2500 );
}

void test_interval_minimum_clamp(){
	using core_observability::parse_interval_ms;
	using core_observability::min_interval_ms;

	core_observability::interval_parse_result r = parse_interval_ms( "10" );
	CHECK( r.valid == true );
	CHECK( r.value_ms == min_interval_ms );

	r = parse_interval_ms( "0" );
	CHECK( r.valid == true );
	CHECK( r.value_ms == min_interval_ms );

	r = parse_interval_ms( "-50" );
	CHECK( r.valid == true );
	CHECK( r.value_ms == min_interval_ms );
}

void test_interval_maximum_clamp(){
	using core_observability::parse_interval_ms;
	using core_observability::max_interval_ms;

	core_observability::interval_parse_result r = parse_interval_ms( "99999999" );
	CHECK( r.valid == true );
	CHECK( r.value_ms == max_interval_ms );
}

void test_interval_invalid_fallback(){
	using core_observability::parse_interval_ms;
	using core_observability::default_interval_ms;

	core_observability::interval_parse_result r = parse_interval_ms( "abc" );
	CHECK( r.valid == false );
	CHECK( r.value_ms == default_interval_ms );

	// trailing garbage is not a number
	r = parse_interval_ms( "12x" );
	CHECK( r.valid == false );
	CHECK( r.value_ms == default_interval_ms );

	// overflow is reported as invalid so the caller warns once
	r = parse_interval_ms( "99999999999999999999999" );
	CHECK( r.valid == false );
	CHECK( r.value_ms == default_interval_ms );
}

void test_label_escaping(){
	using core_observability::escape_label_value;

	CHECK( escape_label_value( "prontera" ) == "prontera" );
	CHECK( escape_label_value( "pro\"ntera" ) == "pro\\\"ntera" );
	CHECK( escape_label_value( "pro\\ntera" ) == "pro\\\\ntera" );
	CHECK( escape_label_value( std::string( "a\nb" ) ) == "a\\nb" );
}

void test_drift_calculation(){
	using core_observability::compute_timer_drift_ms;

	CHECK( compute_timer_drift_ms( 1000, 1250 ) == 250 );
	CHECK( compute_timer_drift_ms( 1000, 1000 ) == 0 );
}

void test_drift_negative_clamp(){
	using core_observability::compute_timer_drift_ms;

	// early execution must never report negative drift
	CHECK( compute_timer_drift_ms( 1000, 900 ) == 0 );
	CHECK( compute_timer_drift_ms( 0, -5 ) == 0 );
}

void test_metrics_rendering(){
	using core_observability::core_metric_values;
	using core_observability::map_entity_counts;
	using core_observability::render_metrics;

	core_metric_values values;
	values.timer_drift_last_ms = 3;
	values.timer_drift_max_ms = 7;
	values.snapshots_total = 5;
	values.snapshot_duration_last_ms = 2;
	values.snapshot_duration_max_ms = 9;
	values.write_errors_total = 1;

	map_entity_counts map;
	map.name = "pro\"ntera";
	map.entities[core_observability::SLOT_PLAYER] = 120;
	map.entities[core_observability::SLOT_MOB] = 35;
	map.entities[core_observability::SLOT_NPC] = 84;
	map.entities[core_observability::SLOT_ITEM] = 12;
	map.entities[core_observability::SLOT_SKILL] = 7;

	std::string out = render_metrics( values, { map } );

	CHECK( out.find( "rathena_core_timer_drift_last_milliseconds 3\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_timer_drift_max_milliseconds 7\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_snapshots_total 5\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_snapshot_duration_last_milliseconds 2\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_snapshot_duration_max_milliseconds 9\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_write_errors_total 1\n" ) != std::string::npos );

	// map label must be escaped
	CHECK( out.find( "rathena_map_entities{map=\"pro\\\"ntera\",type=\"player\"} 120\n" ) != std::string::npos );
	CHECK( out.find( "rathena_map_entities{map=\"pro\\\"ntera\",type=\"mob\"} 35\n" ) != std::string::npos );
	CHECK( out.find( "rathena_map_entities{map=\"pro\\\"ntera\",type=\"npc\"} 84\n" ) != std::string::npos );
	CHECK( out.find( "rathena_map_entities{map=\"pro\\\"ntera\",type=\"item\"} 12\n" ) != std::string::npos );
	CHECK( out.find( "rathena_map_entities{map=\"pro\\\"ntera\",type=\"skill\"} 7\n" ) != std::string::npos );

	// totals aggregate across maps
	CHECK( out.find( "rathena_core_entities_total{type=\"player\"} 120\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"mob\"} 35\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"npc\"} 84\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"item\"} 12\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"skill\"} 7\n" ) != std::string::npos );

	// Prometheus exposition format ends with a newline
	CHECK( !out.empty() && out.back() == '\n' );
}

void test_empty_map_snapshot(){
	using core_observability::core_metric_values;
	using core_observability::render_metrics;

	core_metric_values values;
	std::string out = render_metrics( values, {} );

	// no per-map series without maps
	CHECK( out.find( "rathena_map_entities{" ) == std::string::npos );

	// totals are still exported with zero values
	CHECK( out.find( "rathena_core_entities_total{type=\"player\"} 0\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"mob\"} 0\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"npc\"} 0\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"item\"} 0\n" ) != std::string::npos );
	CHECK( out.find( "rathena_core_entities_total{type=\"skill\"} 0\n" ) != std::string::npos );
}

void test_output_path_sanitization(){
	using core_observability::default_output_path;
	using core_observability::metrics_root_directory;
	using core_observability::resolve_output_path;

	// unset / empty falls back to the default without being a user error
	core_observability::output_path_result r = resolve_output_path( nullptr );
	CHECK( r.valid == true );
	CHECK( r.path == default_output_path );

	r = resolve_output_path( "" );
	CHECK( r.valid == true );
	CHECK( r.path == default_output_path );

	// plain relative filename is placed below the metrics root
	r = resolve_output_path( "rathena_map.prom" );
	CHECK( r.valid == true );
	CHECK( r.path == "log/metrics/rathena_map.prom" );

	// relative subdirectory is allowed
	r = resolve_output_path( "shard/map.prom" );
	CHECK( r.valid == true );
	CHECK( r.path == "log/metrics/shard/map.prom" );

	// backslashes are normalized to forward slashes
	r = resolve_output_path( "shard\\map.prom" );
	CHECK( r.valid == true );
	CHECK( r.path == "log/metrics/shard/map.prom" );

	// every accepted path must stay below the metrics root
	const std::string root_prefix = std::string( metrics_root_directory ) + "/";
	CHECK( r.path.compare( 0, root_prefix.size(), root_prefix ) == 0 );

	// parent directory traversal is rejected
	r = resolve_output_path( "../secret.txt" );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	r = resolve_output_path( "foo/../../secret.txt" );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	// POSIX absolute paths are rejected
	r = resolve_output_path( "/absolute/path.prom" );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	// Windows drive paths are rejected (both separator styles)
	r = resolve_output_path( "C:\\absolute\\path.prom" );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	r = resolve_output_path( "C:/absolute/path.prom" );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	// UNC paths are rejected
	r = resolve_output_path( "\\\\server\\share\\file.prom" );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	// control characters are rejected
	r = resolve_output_path( std::string( "shard\x01map.prom" ).c_str() );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );

	r = resolve_output_path( std::string( "shard\tmap.prom" ).c_str() );
	CHECK( r.valid == false );
	CHECK( r.path == default_output_path );
}

void test_atomic_output(){
	using core_observability::atomic_write_text_file;
	using core_observability::ensure_parent_directory;

	const std::string dir = "core_observability_test_tmp/nested";
	const std::string path = dir + "/metrics.prom";
	const std::string tmp = path + ".tmp";

	std::string error;

	// parent directories are created on demand
	CHECK( ensure_parent_directory( path, &error ) == true );

	// first write
	CHECK( atomic_write_text_file( path, "first 1\n", &error ) == true );
	CHECK( read_file( path ) == "first 1\n" );
	// no temporary file may be left behind
	CHECK( file_exists( tmp ) == false );

	// second write atomically replaces the content
	CHECK( atomic_write_text_file( path, "second 2\n", &error ) == true );
	CHECK( read_file( path ) == "second 2\n" );
	CHECK( file_exists( tmp ) == false );

	// cleanup
	std::remove( path.c_str() );
#ifdef _WIN32
	_rmdir( dir.c_str() );
	_rmdir( "core_observability_test_tmp" );
#else
	rmdir( dir.c_str() );
	rmdir( "core_observability_test_tmp" );
#endif
}

} // namespace

int main(){
	test_boolean_env_parsing();
	test_interval_parsing();
	test_interval_minimum_clamp();
	test_interval_maximum_clamp();
	test_interval_invalid_fallback();
	test_label_escaping();
	test_drift_calculation();
	test_drift_negative_clamp();
	test_metrics_rendering();
	test_empty_map_snapshot();
	test_output_path_sanitization();
	test_atomic_output();

	if( g_failures != 0 ){
		std::fprintf( stderr, "%d check(s) failed\n", g_failures );
		return 1;
	}

	std::printf( "all core observability tests passed\n" );
	return 0;
}
