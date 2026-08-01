// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef PACKET_OBSERVABILITY_HPP
#define PACKET_OBSERVABILITY_HPP

#include <cstddef>
#include <cstdint>
#include <string>

#include "packet_observability_pure.hpp"

/// Boundary-safe transport hooks. These are called from common/socket.cpp,
/// which is also linked into login-server and char-server. The inline wrappers
/// dereference function pointers that default to nullptr, so non-map binaries
/// do not need to link the runtime implementation.
inline void (*g_packet_observability_transport_receive_fn)(size_t) = nullptr;
inline void (*g_packet_observability_transport_send_fn)(size_t) = nullptr;

inline void packet_observability_record_transport_receive( size_t bytes ){
	if( g_packet_observability_transport_receive_fn != nullptr ){
		g_packet_observability_transport_receive_fn( bytes );
	}
}

inline void packet_observability_record_transport_send( size_t bytes ){
	if( g_packet_observability_transport_send_fn != nullptr ){
		g_packet_observability_transport_send_fn( bytes );
	}
}

void packet_observability_init();
void packet_observability_final();
bool packet_observability_enabled();

void packet_observability_record_receive( uint16_t packet_id, size_t bytes );
void packet_observability_record_invalid();
void packet_observability_record_unknown();
void packet_observability_record_processing( uint16_t packet_id, uint64_t duration_ms );
void packet_observability_record_send( uint16_t packet_id, size_t bytes );
void packet_observability_record_broadcast( size_t recipients );

std::string packet_observability_render_snapshot();

#ifdef RATHENA_PACKET_OBSERVABILITY_TESTING
void packet_observability_test_reset( bool enabled, uint32_t slow_ms, size_t capacity );
packet_observability_snapshot packet_observability_test_snapshot();
#endif

#endif // PACKET_OBSERVABILITY_HPP
