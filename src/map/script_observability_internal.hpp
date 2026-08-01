// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder.

#ifndef SCRIPT_OBSERVABILITY_INTERNAL_HPP
#define SCRIPT_OBSERVABILITY_INTERNAL_HPP

#include "script_observability_pure.hpp"

#include <cstdint>
#include <string>

namespace script_observability_internal {

/// Runtime state for script observability. All fields default to disabled/zero.
struct script_observability_state {
	bool enabled = false;
	uint32_t slow_ms = script_observability_default_slow_ms;
	ScriptObservabilitySnapshot snapshot;
};

extern script_observability_state state;

} // namespace script_observability_internal

/// Monotonic clock source used by run_script_main(). Defaults to gettick_nocache().
extern uint64_t (*script_observability_clock_fn)();

#endif // SCRIPT_OBSERVABILITY_INTERNAL_HPP