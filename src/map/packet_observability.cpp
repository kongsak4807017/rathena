// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#include "packet_observability.hpp"
#include "packet_observability_internal.hpp"

#include <cstdlib>
#include <utility>

#include <common/showmsg.hpp>
#include <common/timer.hpp>

namespace packet_observability_internal {

packet_observability_state state;

} // namespace packet_observability_internal

namespace {

constexpr const char* ENV_ENABLE = "RATHENA_PACKET_OBSERVABILITY";
constexpr const char* ENV_SLOW_MS = "RATHENA_PACKET_OBSERVABILITY_SLOW_MS";
constexpr const char* ENV_MAX_PACKET_IDS = "RATHENA_PACKET_OBSERVABILITY_MAX_PACKET_IDS";

using packet_observability_internal::state;

packet_observability_packet_metrics* admit_or_overflow( uint16_t packet_id ){
	packet_observability_packet_metrics* metrics = state.registry.admit( packet_id );

	if( metrics == nullptr ){
		state.packet_id_overflow_total = packet_observability_saturating_add( state.packet_id_overflow_total, 1 );
	}

	return metrics;
}

void impl_transport_receive( size_t bytes ){
	if( !packet_observability_enabled() ){
		return;
	}

	state.transport_received_bytes_total = packet_observability_saturating_add( state.transport_received_bytes_total, static_cast<uint64_t>( bytes ) );
}

void impl_transport_send( size_t bytes ){
	if( !packet_observability_enabled() ){
		return;
	}

	state.transport_sent_bytes_total = packet_observability_saturating_add( state.transport_sent_bytes_total, static_cast<uint64_t>( bytes ) );
}

} // namespace

bool packet_observability_enabled(){
	return packet_observability_internal::state.enabled;
}

void packet_observability_init(){
	using packet_observability_internal::state;

	// Guard against duplicate initialization.
	if( state.enabled ){
		return;
	}

	const char* raw_enabled = std::getenv( ENV_ENABLE );
	state.enabled = packet_observability_parse_enabled( raw_enabled );

	bool slow_fallback = false;
	const char* raw_slow_ms = std::getenv( ENV_SLOW_MS );
	state.slow_ms = packet_observability_parse_slow_ms( raw_slow_ms, slow_fallback );

	bool capacity_fallback = false;
	const char* raw_capacity = std::getenv( ENV_MAX_PACKET_IDS );
	state.capacity = packet_observability_parse_capacity( raw_capacity, capacity_fallback );

	if( !state.enabled ){
		return;
	}

	if( slow_fallback ){
		ShowWarning( "packet_observability: invalid %s value '%s', falling back to the default of %u ms.\n", ENV_SLOW_MS, raw_slow_ms != nullptr ? raw_slow_ms : "", packet_observability_default_slow_ms );
	}

	if( capacity_fallback ){
		ShowWarning( "packet_observability: invalid %s value '%s', falling back to the default of %zu.\n", ENV_MAX_PACKET_IDS, raw_capacity != nullptr ? raw_capacity : "", packet_observability_default_capacity );
	}

	// Allocate the bounded registry only when instrumentation is enabled.
	state.registry = packet_observability_bounded_registry( state.capacity );

	g_packet_observability_transport_receive_fn = impl_transport_receive;
	g_packet_observability_transport_send_fn = impl_transport_send;

	ShowInfo( "packet_observability: instrumentation enabled (slow_ms=%u, capacity=%zu).\n", state.slow_ms, state.capacity );
}

void packet_observability_final(){
	using packet_observability_internal::state;

	g_packet_observability_transport_receive_fn = nullptr;
	g_packet_observability_transport_send_fn = nullptr;

	state.enabled = false;
	state.registry = packet_observability_bounded_registry( 0 );
}

void packet_observability_record_receive( uint16_t packet_id, size_t bytes ){
	if( !packet_observability_enabled() ){
		return;
	}

	const uint64_t bytes64 = static_cast<uint64_t>( bytes );

	state.received_packets_total = packet_observability_saturating_add( state.received_packets_total, 1 );
	state.received_bytes_total = packet_observability_saturating_add( state.received_bytes_total, bytes64 );

	packet_observability_packet_metrics* metrics = admit_or_overflow( packet_id );

	if( metrics != nullptr ){
		metrics->received_total = packet_observability_saturating_add( metrics->received_total, 1 );
		metrics->received_bytes_total = packet_observability_saturating_add( metrics->received_bytes_total, bytes64 );
	}
}

void packet_observability_record_invalid(){
	if( !packet_observability_enabled() ){
		return;
	}

	state.invalid_packets_total = packet_observability_saturating_add( state.invalid_packets_total, 1 );
}

void packet_observability_record_unknown(){
	if( !packet_observability_enabled() ){
		return;
	}

	state.unknown_packets_total = packet_observability_saturating_add( state.unknown_packets_total, 1 );
}

void packet_observability_record_processing( uint16_t packet_id, uint64_t duration_ms ){
	if( !packet_observability_enabled() ){
		return;
	}

	packet_observability_packet_metrics* metrics = admit_or_overflow( packet_id );

	if( metrics != nullptr ){
		metrics->processing_duration_last_ms = duration_ms;

		if( duration_ms > metrics->processing_duration_max_ms ){
			metrics->processing_duration_max_ms = duration_ms;
		}

		if( packet_observability_is_slow( duration_ms, state.slow_ms ) ){
			metrics->processing_slow_total = packet_observability_saturating_add( metrics->processing_slow_total, 1 );
		}
	}
}

void packet_observability_record_send( uint16_t packet_id, size_t bytes ){
	if( !packet_observability_enabled() ){
		return;
	}

	const uint64_t bytes64 = static_cast<uint64_t>( bytes );

	state.sent_packets_total = packet_observability_saturating_add( state.sent_packets_total, 1 );
	state.sent_bytes_total = packet_observability_saturating_add( state.sent_bytes_total, bytes64 );

	packet_observability_packet_metrics* metrics = admit_or_overflow( packet_id );

	if( metrics != nullptr ){
		metrics->sent_total = packet_observability_saturating_add( metrics->sent_total, 1 );
		metrics->sent_bytes_total = packet_observability_saturating_add( metrics->sent_bytes_total, bytes64 );
	}
}

void packet_observability_record_broadcast( size_t recipients ){
	if( !packet_observability_enabled() ){
		return;
	}

	const uint64_t recipients64 = static_cast<uint64_t>( recipients );

	state.broadcast_calls_total = packet_observability_saturating_add( state.broadcast_calls_total, 1 );
	state.broadcast_recipients_total = packet_observability_saturating_add( state.broadcast_recipients_total, recipients64 );
	state.broadcast_recipients_last = recipients64;

	if( recipients64 > state.broadcast_recipients_max ){
		state.broadcast_recipients_max = recipients64;
	}
}

std::string packet_observability_render_snapshot(){
	using packet_observability_internal::state;

	packet_observability_snapshot snapshot;

	snapshot.transport_received_bytes_total = state.transport_received_bytes_total;
	snapshot.transport_sent_bytes_total = state.transport_sent_bytes_total;
	snapshot.received_packets_total = state.received_packets_total;
	snapshot.received_bytes_total = state.received_bytes_total;
	snapshot.invalid_packets_total = state.invalid_packets_total;
	snapshot.unknown_packets_total = state.unknown_packets_total;
	snapshot.sent_packets_total = state.sent_packets_total;
	snapshot.sent_bytes_total = state.sent_bytes_total;
	snapshot.broadcast_calls_total = state.broadcast_calls_total;
	snapshot.broadcast_recipients_total = state.broadcast_recipients_total;
	snapshot.broadcast_recipients_last = state.broadcast_recipients_last;
	snapshot.broadcast_recipients_max = state.broadcast_recipients_max;
	snapshot.packet_id_overflow_total = state.packet_id_overflow_total;

	const std::vector<packet_observability_bounded_registry::entry>& storage = state.registry.storage();

	snapshot.packets.reserve( state.registry.size() );

	for( const packet_observability_bounded_registry::entry& entry : storage ){
		if( entry.occupied ){
			snapshot.packets.emplace_back( entry.packet_id, entry.metrics );
		}
	}

	return packet_observability_render_prometheus( snapshot );
}

#ifdef RATHENA_PACKET_OBSERVABILITY_TESTING

void packet_observability_test_reset( bool enabled, uint32_t slow_ms, size_t capacity ){
	using packet_observability_internal::state;

	state = packet_observability_internal::packet_observability_state();
	state.enabled = enabled;
	state.slow_ms = slow_ms;
	state.capacity = capacity;
	state.registry = packet_observability_bounded_registry( capacity );

	if( enabled ){
		g_packet_observability_transport_receive_fn = impl_transport_receive;
		g_packet_observability_transport_send_fn = impl_transport_send;
	}else{
		g_packet_observability_transport_receive_fn = nullptr;
		g_packet_observability_transport_send_fn = nullptr;
	}
}

packet_observability_snapshot packet_observability_test_snapshot(){
	using packet_observability_internal::state;

	packet_observability_snapshot snapshot;

	snapshot.transport_received_bytes_total = state.transport_received_bytes_total;
	snapshot.transport_sent_bytes_total = state.transport_sent_bytes_total;
	snapshot.received_packets_total = state.received_packets_total;
	snapshot.received_bytes_total = state.received_bytes_total;
	snapshot.invalid_packets_total = state.invalid_packets_total;
	snapshot.unknown_packets_total = state.unknown_packets_total;
	snapshot.sent_packets_total = state.sent_packets_total;
	snapshot.sent_bytes_total = state.sent_bytes_total;
	snapshot.broadcast_calls_total = state.broadcast_calls_total;
	snapshot.broadcast_recipients_total = state.broadcast_recipients_total;
	snapshot.broadcast_recipients_last = state.broadcast_recipients_last;
	snapshot.broadcast_recipients_max = state.broadcast_recipients_max;
	snapshot.packet_id_overflow_total = state.packet_id_overflow_total;

	const std::vector<packet_observability_bounded_registry::entry>& storage = state.registry.storage();

	snapshot.packets.reserve( state.registry.size() );

	for( const packet_observability_bounded_registry::entry& entry : storage ){
		if( entry.occupied ){
			snapshot.packets.emplace_back( entry.packet_id, entry.metrics );
		}
	}

	return snapshot;
}

#endif // RATHENA_PACKET_OBSERVABILITY_TESTING
