// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder.

#ifndef SCRIPT_OBSERVABILITY_HPP
#define SCRIPT_OBSERVABILITY_HPP

#include "script_observability_pure.hpp"

#include <cstdint>
#include <string>

void script_observability_init();
void script_observability_final();
bool script_observability_enabled();
void script_observability_record_slice(
	ScriptObservabilityCategory category,
	uint64_t duration_ms,
	uint64_t commands,
	bool failed
);
std::string script_observability_render_prometheus();

#ifdef RATHENA_SCRIPT_OBSERVABILITY_TESTING
void script_observability_test_reset( bool enabled, uint32_t slow_ms );
ScriptObservabilitySnapshot script_observability_test_snapshot();
void script_observability_test_set_clock( uint64_t (*clock_fn)() );
#endif // RATHENA_SCRIPT_OBSERVABILITY_TESTING

#endif // SCRIPT_OBSERVABILITY_HPP