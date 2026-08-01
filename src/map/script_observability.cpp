// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder.

#include "script_observability.hpp"
#include "script_observability_internal.hpp"

#include <cstdlib>

#include <common/showmsg.hpp>
#include <common/timer.hpp>

namespace script_observability_internal {

script_observability_state state;

} // namespace script_observability_internal

namespace {

constexpr const char* ENV_ENABLE = "RATHENA_SCRIPT_OBSERVABILITY";
constexpr const char* ENV_SLOW_MS = "RATHENA_SCRIPT_OBSERVABILITY_SLOW_MS";

uint64_t script_observability_default_clock(){
	return static_cast<uint64_t>( gettick_nocache() );
}

} // namespace

uint64_t (*script_observability_clock_fn)() = script_observability_default_clock;

bool script_observability_enabled(){
	return script_observability_internal::state.enabled;
}

void script_observability_init(){
	using script_observability_internal::state;

	// Guard against duplicate initialization.
	if( state.enabled ){
		return;
	}

	const char* raw_enabled = std::getenv( ENV_ENABLE );
	state.enabled = script_observability_parse_bool( raw_enabled, false );

	bool slow_fallback = false;
	const char* raw_slow_ms = std::getenv( ENV_SLOW_MS );
	state.slow_ms = script_observability_parse_u32( raw_slow_ms, script_observability_default_slow_ms, script_observability_min_slow_ms, script_observability_max_slow_ms );

	if( !state.enabled ){
		return;
	}

	if( slow_fallback ){
		ShowWarning( "script_observability: invalid %s value '%s', falling back to the default of %u ms.\n", ENV_SLOW_MS, raw_slow_ms != nullptr ? raw_slow_ms : "", script_observability_default_slow_ms );
	}

	ShowInfo( "script_observability: instrumentation enabled (slow_ms=%u).\n", state.slow_ms );
}

void script_observability_final(){
	using script_observability_internal::state;

	state.enabled = false;
	state.slow_ms = script_observability_default_slow_ms;
	state.snapshot = ScriptObservabilitySnapshot();
	script_observability_clock_fn = script_observability_default_clock;
}

void script_observability_record_slice( ScriptObservabilityCategory category, uint64_t duration_ms, uint64_t commands, bool failed ){
	// Disabled-path contract: one enable check, then nothing.
	if( !script_observability_enabled() ){
		return;
	}

	script_observability_internal::state.snapshot.record_slice( category, duration_ms, commands, failed, script_observability_internal::state.slow_ms );
}

std::string script_observability_render_prometheus(){
	return ::script_observability_render_prometheus( script_observability_internal::state.snapshot );
}

#ifdef RATHENA_SCRIPT_OBSERVABILITY_TESTING

void script_observability_test_reset( bool enabled, uint32_t slow_ms ){
	using script_observability_internal::state;

	state = script_observability_internal::script_observability_state();
	state.enabled = enabled;
	state.slow_ms = slow_ms;
	script_observability_clock_fn = script_observability_default_clock;
}

ScriptObservabilitySnapshot script_observability_test_snapshot(){
	return script_observability_internal::state.snapshot;
}

void script_observability_test_set_clock( uint64_t (*clock_fn)() ){
	script_observability_clock_fn = clock_fn != nullptr ? clock_fn : script_observability_default_clock;
}

#endif // RATHENA_SCRIPT_OBSERVABILITY_TESTING