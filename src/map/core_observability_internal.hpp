// Copyright (c) rAthena Dev Teams - Licensed under GNU GPL
// For more information, see LICENCE in the main folder

#ifndef CORE_OBSERVABILITY_INTERNAL_HPP
#define CORE_OBSERVABILITY_INTERNAL_HPP

#include <array>
#include <string>
#include <vector>

#include <common/cbasetypes.hpp>
#include <common/timer.hpp>

namespace core_observability_internal {

enum entity_slot : size_t {
	ENTITY_PLAYER = 0,
	ENTITY_MOB,
	ENTITY_NPC,
	ENTITY_ITEM,
	ENTITY_SKILL,
	ENTITY_COUNT,
};

struct map_snapshot {
	std::string name;
	std::array<uint64, ENTITY_COUNT> entities{};
};

struct observability_state {
	bool enabled = false;
	int32 timer_id = INVALID_TIMER;
	int32 interval_ms = 10000;
	t_tick next_tick = 0;
	std::string output_path;
	t_tick last_drift_ms = 0;
	t_tick max_drift_ms = 0;
	t_tick last_snapshot_duration_ms = 0;
	t_tick max_snapshot_duration_ms = 0;
	uint64 snapshots_total = 0;
	uint64 write_errors_total = 0;
};

extern observability_state state;

std::vector<map_snapshot> collect_map_snapshots(std::array<uint64, ENTITY_COUNT>& totals);
bool write_snapshot(t_tick tick);
int32 read_interval_ms();

} // namespace core_observability_internal

#endif // CORE_OBSERVABILITY_INTERNAL_HPP
