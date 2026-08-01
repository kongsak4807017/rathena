// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef SQL_OBSERVABILITY_HPP
#define SQL_OBSERVABILITY_HPP

// Public runtime API for SQL observability instrumentation.
// Include this header from server code to record SQL events and render
// Prometheus metrics. The disabled path is designed to be extremely cheap:
// every record_* function returns immediately when instrumentation is off.

#include <string>

#include "sql_observability_pure.hpp"

void sql_observability_init();
void sql_observability_final();
bool sql_observability_enabled();
void sql_observability_set_subsystem( SqlObservabilitySubsystem subsystem );
SqlObservabilitySubsystem sql_observability_get_subsystem();
void sql_observability_record_query( uint64_t duration_ms, bool success );
void sql_observability_record_prepared( uint64_t duration_ms, bool success );
void sql_observability_record_connect( bool success );
void sql_observability_record_ping( bool success );
void sql_observability_record_reconnect();
std::string sql_observability_render_prometheus();

#ifdef RATHENA_SQL_OBSERVABILITY_TESTING
void sql_observability_test_reset( bool enabled, uint32_t slow_ms, size_t max_subsystems );
SqlObservabilitySnapshot sql_observability_test_snapshot();
#endif

#endif // SQL_OBSERVABILITY_HPP
