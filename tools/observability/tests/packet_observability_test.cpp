// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder
//
// Unit tests for the pure (dependency-free) packet observability helpers.
// These tests must compile and run without a full map-server.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

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

void test_bounded_registry_admission(){
	packet_observability_bounded_registry registry( 2 );

	CHECK( registry.capacity() == 2 );
	CHECK( registry.size() == 0 );

	packet_observability_packet_metrics* first = registry.admit( 0x0064 );
	CHECK( first != nullptr );
	CHECK( registry.size() == 1 );

	packet_observability_packet_metrics* second = registry.admit( 0x0085 );
	CHECK( second != nullptr );
	CHECK( registry.size() == 2 );
	CHECK( second != first );

	packet_observability_packet_metrics* repeat = registry.admit( 0x0064 );
	CHECK( repeat == first );
	CHECK( registry.size() == 2 );

	packet_observability_packet_metrics* overflow = registry.admit( 0x0090 );
	CHECK( overflow == nullptr );
	CHECK( registry.size() == 2 );
}

void test_render_prometheus(){
	packet_observability_snapshot snapshot;
	snapshot.transport_received_bytes_total = 1000;
	snapshot.transport_sent_bytes_total = 2000;
	snapshot.received_packets_total = 3;
	snapshot.received_bytes_total = 300;
	snapshot.invalid_packets_total = 1;
	snapshot.unknown_packets_total = 2;
	snapshot.sent_packets_total = 4;
	snapshot.sent_bytes_total = 400;
	snapshot.broadcast_calls_total = 5;
	snapshot.broadcast_recipients_total = 50;
	snapshot.broadcast_recipients_last = 10;
	snapshot.broadcast_recipients_max = 20;
	snapshot.packet_id_overflow_total = 1;

	// Insert out of order to verify deterministic sorting by packet_id.
	packet_observability_packet_metrics metrics_85;
	metrics_85.received_total = 1;
	metrics_85.received_bytes_total = 50;
	metrics_85.sent_total = 3;
	metrics_85.sent_bytes_total = 150;
	metrics_85.processing_duration_last_ms = 3;
	metrics_85.processing_duration_max_ms = 5;
	metrics_85.processing_slow_total = 0;

	packet_observability_packet_metrics metrics_64;
	metrics_64.received_total = 2;
	metrics_64.received_bytes_total = 100;
	metrics_64.sent_total = 4;
	metrics_64.sent_bytes_total = 200;
	metrics_64.processing_duration_last_ms = 6;
	metrics_64.processing_duration_max_ms = 7;
	metrics_64.processing_slow_total = 1;

	snapshot.packets.emplace_back( static_cast<uint16_t>( 0x0085 ), metrics_85 );
	snapshot.packets.emplace_back( static_cast<uint16_t>( 0x0064 ), metrics_64 );

	const std::string out = packet_observability_render_prometheus( snapshot );

	CHECK( out.find( "rathena_packet_transport_received_bytes_total 1000\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_transport_sent_bytes_total 2000\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_received_packets_total 3\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_received_bytes_total 300\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_invalid_packets_total 1\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_unknown_packets_total 2\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_sent_packets_total 4\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_sent_bytes_total 400\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_broadcast_calls_total 5\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_broadcast_recipients_total 50\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_broadcast_recipients_last 10\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_broadcast_recipients_max 20\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_id_overflow_total 1\n" ) != std::string::npos );

	CHECK( out.find( "rathena_packet_received_total{packet=\"0x0064\"} 2\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_received_bytes_total{packet=\"0x0064\"} 100\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_sent_total{packet=\"0x0064\"} 4\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_sent_bytes_total{packet=\"0x0064\"} 200\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_processing_duration_last_milliseconds{packet=\"0x0064\"} 6\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_processing_duration_max_milliseconds{packet=\"0x0064\"} 7\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_processing_slow_total{packet=\"0x0064\"} 1\n" ) != std::string::npos );

	CHECK( out.find( "rathena_packet_received_total{packet=\"0x0085\"} 1\n" ) != std::string::npos );
	CHECK( out.find( "rathena_packet_received_bytes_total{packet=\"0x0085\"} 50\n" ) != std::string::npos );

	// Deterministic ordering: 0x0064 must appear before 0x0085.
	CHECK( out.find( "packet=\"0x0064\"" ) < out.find( "packet=\"0x0085\"" ) );

	// Single trailing newline, no blank lines at the end.
	CHECK( !out.empty() );
	CHECK( out.back() == '\n' );
	CHECK( out.find( "\n\n" ) == std::string::npos );
}

void test_render_prometheus_privacy(){
	// These test-local strings must never appear in rendered output.
	const std::string private_player_name = "PRIVATE_PLAYER_NAME";
	const std::string private_ip = "192.0.2.15";
	const std::string private_chat = "secret-chat-text";

	packet_observability_snapshot snapshot;
	snapshot.received_packets_total = 1;
	snapshot.packets.emplace_back( static_cast<uint16_t>( 0x0064 ), packet_observability_packet_metrics() );

	const std::string out = packet_observability_render_prometheus( snapshot );

	CHECK( out.find( private_player_name ) == std::string::npos );
	CHECK( out.find( private_ip ) == std::string::npos );
	CHECK( out.find( private_chat ) == std::string::npos );
}

void test_counter_saturation(){
	packet_observability_bounded_registry registry( 1 );
	packet_observability_packet_metrics* metrics = registry.admit( 0x0064 );
	CHECK( metrics != nullptr );

	metrics->received_total = std::numeric_limits<uint64_t>::max() - 2;
	metrics->received_total = packet_observability_saturating_add( metrics->received_total, 5 );

	CHECK( metrics->received_total == std::numeric_limits<uint64_t>::max() );
}

} // namespace

int main(){
	test_parse_enabled();
	test_parse_slow_ms();
	test_parse_capacity();
	test_saturating_add();
	test_is_slow();
	test_format_packet_id();
	test_bounded_registry_admission();
	test_render_prometheus();
	test_render_prometheus_privacy();
	test_counter_saturation();

	if( g_failures != 0 ){
		std::fprintf( stderr, "%d check(s) failed\n", g_failures );
		return 1;
	}

	std::printf( "all packet observability tests passed\n" );
	return 0;
}
