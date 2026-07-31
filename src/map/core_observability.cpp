// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#include "core_observability.hpp"

#include <cstdlib>
#include <utility>

#include <common/showmsg.hpp>
#include <common/timer.hpp>

#include "core_observability_internal.hpp"
#include "map.hpp"
#include "packet_observability.hpp"

// Included last on purpose: on Windows the rAthena common headers define
// their own <Windows.h> settings (common/winapi.hpp), this header must not
// include <windows.h> before them.
#include "core_observability_pure.hpp"

namespace core_observability_internal {

observability_state state;

namespace {

constexpr const char* ENV_ENABLE = "RATHENA_CORE_OBSERVABILITY";
constexpr const char* ENV_INTERVAL_MS = "RATHENA_CORE_OBSERVABILITY_INTERVAL_MS";
constexpr const char* ENV_OUTPUT = "RATHENA_CORE_OBSERVABILITY_OUTPUT";

// Mirrors the interval timer rescheduling of do_timer(): the schedule is
// advanced by the interval, unless the timer lagged more than one second,
// in which case do_timer() re-bases it on the current tick.
void advance_schedule( t_tick tick ){
	if( tick - state.next_tick > 1000 ){
		state.next_tick = tick + state.interval_ms;
	}else{
		state.next_tick += state.interval_ms;
	}
}

TIMER_FUNC( core_observability_timer ){
	const t_tick drift = static_cast<t_tick>( core_observability::compute_timer_drift_ms( state.next_tick, tick ) );

	state.last_drift_ms = drift;
	if( drift > state.max_drift_ms ){
		state.max_drift_ms = drift;
	}

	advance_schedule( tick );

	write_snapshot( tick );

	return 0;
}

} // namespace

int32 read_interval_ms(){
	const char* raw = std::getenv( ENV_INTERVAL_MS );
	const core_observability::interval_parse_result parsed = core_observability::parse_interval_ms( raw );

	if( !parsed.valid ){
		ShowWarning( "core_observability: invalid %s value '%s', falling back to the default of %lld ms.\n", ENV_INTERVAL_MS, raw, static_cast<long long>( core_observability::default_interval_ms ) );
	}

	return static_cast<int32>( parsed.value_ms );
}

std::vector<map_snapshot> collect_map_snapshots( std::array<uint64, ENTITY_COUNT>& totals ){
	std::vector<map_snapshot> snapshots;
	snapshots.reserve( map_num );

	for( int32 m = 0; m < map_num; m++ ){
		struct map_data* mapdata = map_getmapdata( static_cast<int16>( m ) );

		// Skip maps that are not initialized, not local to this
		// map-server or already in teardown.
		if( mapdata == nullptr || mapdata->cell == nullptr || mapdata->block == nullptr || mapdata->block_mob == nullptr ){
			continue;
		}

		map_snapshot snapshot;
		snapshot.name = mapdata->name;
		snapshot.entities[ENTITY_PLAYER] = mapdata->users > 0 ? static_cast<uint64>( mapdata->users ) : 0;
		snapshot.entities[ENTITY_NPC] = mapdata->npc_num > 0 ? static_cast<uint64>( mapdata->npc_num ) : 0;

		const int32 bsize = mapdata->bxs * mapdata->bys;

		for( int32 b = 0; b < bsize; b++ ){
			for( block_list* bl = mapdata->block[b]; bl != nullptr; bl = bl->next ){
				if( bl->type == BL_ITEM ){
					snapshot.entities[ENTITY_ITEM]++;
				}else if( bl->type == BL_SKILL ){
					snapshot.entities[ENTITY_SKILL]++;
				}
			}
			for( block_list* bl = mapdata->block_mob[b]; bl != nullptr; bl = bl->next ){
				if( bl->type == BL_MOB ){
					snapshot.entities[ENTITY_MOB]++;
				}
			}
		}

		for( size_t slot = 0; slot < ENTITY_COUNT; slot++ ){
			totals[slot] += snapshot.entities[slot];
		}

		snapshots.push_back( std::move( snapshot ) );
	}

	return snapshots;
}

bool write_snapshot( t_tick tick ){
	const t_tick start = gettick_nocache();

	std::array<uint64, ENTITY_COUNT> totals{};
	std::vector<map_snapshot> snapshots = collect_map_snapshots( totals );

	// Count this snapshot before rendering so the exported series moves forward.
	state.snapshots_total++;

	core_observability::core_metric_values values;
	values.timer_drift_last_ms = state.last_drift_ms;
	values.timer_drift_max_ms = state.max_drift_ms;
	values.snapshots_total = state.snapshots_total;
	values.snapshot_duration_last_ms = state.last_snapshot_duration_ms;
	values.snapshot_duration_max_ms = state.max_snapshot_duration_ms;
	values.write_errors_total = state.write_errors_total;

	std::vector<core_observability::map_entity_counts> maps;
	maps.reserve( snapshots.size() );

	for( const map_snapshot& snapshot : snapshots ){
		core_observability::map_entity_counts counts;
		counts.name = snapshot.name;

		for( size_t slot = 0; slot < ENTITY_COUNT; slot++ ){
			counts.entities[slot] = snapshot.entities[slot];
		}

		maps.push_back( std::move( counts ) );
	}

	std::string output = core_observability::render_metrics( values, maps );

	if( packet_observability_enabled() ){
		output += packet_observability_render_snapshot();
	}

	std::string error;
	bool written = core_observability::ensure_parent_directory( state.output_path, &error );

	if( written ){
		written = core_observability::atomic_write_text_file( state.output_path, output, &error );
	}

	const t_tick duration = gettick_nocache() - start;

	state.last_snapshot_duration_ms = duration;
	if( duration > state.max_snapshot_duration_ms ){
		state.max_snapshot_duration_ms = duration;
	}

	if( !written ){
		state.write_errors_total++;
		ShowWarning( "core_observability: failed to write metrics to '%s': %s\n", state.output_path.c_str(), error.c_str() );
	}

	return written;
}

} // namespace core_observability_internal

void core_observability_init(){
	using namespace core_observability_internal;

	// Initialize packet observability first so its configuration is parsed
	// regardless of whether core observability itself is enabled. Packet
	// counters can accumulate even when the core timer/writer is disabled.
	packet_observability_init();

	// Guard against duplicate initialization (e.g. after script reloads).
	if( state.enabled ){
		return;
	}

	// Disabled by default: no timer, no map scans, no file output.
	if( !core_observability::parse_enabled( std::getenv( ENV_ENABLE ) ) ){
		return;
	}

	state.interval_ms = read_interval_ms();

	// The metrics root directory (log/metrics) cannot be overridden; unsafe
	// values fall back to the default with a single warning.
	const core_observability::output_path_result output = core_observability::resolve_output_path( std::getenv( ENV_OUTPUT ) );

	if( !output.valid ){
		ShowWarning( "core_observability: unsafe %s value '%s', falling back to the default '%s'.\n", ENV_OUTPUT, std::getenv( ENV_OUTPUT ), core_observability::default_output_path );
	}

	state.output_path = output.path;

	state.enabled = true;

	add_timer_func_list( core_observability_timer, "core_observability_timer" );

	state.next_tick = gettick() + state.interval_ms;
	state.timer_id = add_timer_interval( state.next_tick, core_observability_timer, 0, 0, state.interval_ms );

	if( state.timer_id == INVALID_TIMER ){
		ShowWarning( "core_observability: failed to create the observability timer, instrumentation stays disabled.\n" );
		state.enabled = false;
		return;
	}

	ShowInfo( "core_observability: instrumentation enabled (interval=%d ms, output='%s').\n", state.interval_ms, state.output_path.c_str() );
}

void core_observability_final(){
	using namespace core_observability_internal;

	if( state.timer_id != INVALID_TIMER ){
		delete_timer( state.timer_id, core_observability_timer );
		state.timer_id = INVALID_TIMER;
	}

	state.enabled = false;

	// Tear down packet observability after the core timer has been cleaned up.
	packet_observability_final();
}
