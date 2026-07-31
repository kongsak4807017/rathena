// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder
//
// Unit tests for the pure (dependency-free) packet observability helpers.
// These tests must compile and run without a full map-server.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <string>

#include "packet_observability_pure.hpp"

namespace {

int g_failures = 0;

#define CHECK( cond ) \
	do { \
		if( !( cond ) ){ \
			++g_failures; \
			std::fprintf( stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond ); \
		} \
	} while( 0 )

void test_parse_enabled(){
	CHECK( packet_observability_parse_enabled( "1" ) );
	CHECK( packet_observability_parse_enabled( "TRUE" ) );
	CHECK( packet_observability_parse_enabled( "On" ) );
	CHECK( packet_observability_parse_enabled( "yes" ) );
	CHECK( !packet_observability_parse_enabled( nullptr ) );
	CHECK( !packet_observability_parse_enabled( "0" ) );
	CHECK( !packet_observability_parse_enabled( "enabled" ) );
}

void test_parse_slow_ms(){
	bool fallback = false;

	CHECK( packet_observability_parse_slow_ms( "25", fallback ) == 25 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "0", fallback ) == 1 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "999999", fallback ) == 60000 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "bad", fallback ) == 25 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( nullptr, fallback ) == 25 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "", fallback ) == 25 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "-5", fallback ) == 25 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "1", fallback ) == 1 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_slow_ms( "60000", fallback ) == 60000 );
	CHECK( !fallback );
}

void test_parse_capacity(){
	bool fallback = false;

	CHECK( packet_observability_parse_capacity( "8", fallback ) == 16 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( "512", fallback ) == 512 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( "99999", fallback ) == 4096 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( "bad", fallback ) == 512 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( nullptr, fallback ) == 512 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( "", fallback ) == 512 );
	CHECK( fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( "16", fallback ) == 16 );
	CHECK( !fallback );

	fallback = false;
	CHECK( packet_observability_parse_capacity( "4096", fallback ) == 4096 );
	CHECK( !fallback );
}

void test_saturating_add(){
	CHECK( packet_observability_saturating_add( 5, 7 ) == 12 );
	CHECK( packet_observability_saturating_add( UINT64_MAX - 1, 5 ) == UINT64_MAX );
	CHECK( packet_observability_saturating_add( UINT64_MAX, 0 ) == UINT64_MAX );
	CHECK( packet_observability_saturating_add( 0, UINT64_MAX ) == UINT64_MAX );
}

void test_is_slow(){
	CHECK( !packet_observability_is_slow( 24, 25 ) );
	CHECK( packet_observability_is_slow( 25, 25 ) );
	CHECK( packet_observability_is_slow( 26, 25 ) );
	CHECK( !packet_observability_is_slow( 0, 1 ) );
}

void test_format_packet_id(){
	CHECK( packet_observability_format_packet_id( 0x64 ) == "0x0064" );
	CHECK( packet_observability_format_packet_id( 0xffff ) == "0xffff" );
	CHECK( packet_observability_format_packet_id( 0x0 ) == "0x0000" );
	CHECK( packet_observability_format_packet_id( 0x1 ) == "0x0001" );
}

} // namespace

int main(){
	test_parse_enabled();
	test_parse_slow_ms();
	test_parse_capacity();
	test_saturating_add();
	test_is_slow();
	test_format_packet_id();

	if( g_failures != 0 ){
		std::fprintf( stderr, "%d check(s) failed\n", g_failures );
		return 1;
	}

	std::printf( "all packet observability tests passed\n" );
	return 0;
}
