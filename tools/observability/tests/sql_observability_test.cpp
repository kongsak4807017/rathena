// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder
//
// Unit tests for the pure (dependency-free) SQL observability helpers.
// These tests must compile and run without a running server.

#include <cstdio>
#include <cstdint>
#include <cstring>

#include "sql_observability_pure.hpp"

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

} // namespace

int main(){
	test_parse_bool();
	test_parse_u32();
	test_is_slow();
	test_subsystem_labels();

	if( g_failures != 0 ){
		std::fprintf( stderr, "%d check(s) failed\n", g_failures );
		return 1;
	}

	std::printf( "all sql observability tests passed\n" );
	return 0;
}
