"""Deterministic synthetic dataset planning and verification for A3.

Produces a fully deterministic, synthetic-only dataset plan (accounts,
characters, guilds, parties, and per-character profile tiers) from a single
integer seed, emits staging SQL plus a metadata sidecar, and verifies row
counts and relationship integrity.

Determinism rules (approved design): the only randomness source is
``random.Random(seed)``. No global random state, secrets, uuid4, timestamps,
hostname, PID, or unordered set iteration ever reaches serialized output.
"""

import dataclasses
import hashlib
import json
import os
import random
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from tools.performance.a3.io import sha256_file, write_json_atomic

DATASET_VERSION = 1
GENERATED_FROM = "A3 deterministic synthetic dataset planner"

ACCOUNT_TOTAL = 6000
CHARACTER_TOTAL = 12000
GUILD_TOTAL = 200
PARTY_TOTAL = 500
CHARACTERS_PER_ACCOUNT = 2

ACCOUNT_ID_BASE = 2000000
CHARACTER_ID_BASE = 3000000
GUILD_ID_BASE = 400000
PARTY_ID_BASE = 500000

GUILD_MIN_MEMBERS = 4
GUILD_MAX_MEMBERS = 40
PARTY_MIN_MEMBERS = 2
PARTY_MAX_MEMBERS = 12

# Fixed, clearly synthetic placeholder for staging databases only. It is NOT
# production-secure; authentication integration may replace it before real
# load runs.
PASSWORD_HASH_PLACEHOLDER = "A3_SYNTHETIC_PASSWORD_HASH_PLACEHOLDER"

DIMENSIONS = ("inventory", "storage", "quest")

COUNT_FIELDS = (
    "accounts",
    "characters",
    "guilds",
    "parties",
    "guild_memberships",
    "party_memberships",
)

EXPECTED_FOREIGN_KEY_RELATIONSHIPS = {
    "a3_stage_characters.account_id": "a3_stage_accounts.account_id",
    "a3_stage_guilds.master_char_id": "a3_stage_characters.char_id",
    "a3_stage_guild_members.char_id": "a3_stage_characters.char_id",
    "a3_stage_parties.leader_char_id": "a3_stage_characters.char_id",
    "a3_stage_party_members.char_id": "a3_stage_characters.char_id",
}


class ProfileTier(str, Enum):
    """Approved per-character content profile tiers."""

    EMPTY = "empty"
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"


# Exact approved weights per inventory/storage/quest dimension.
PROFILE_WEIGHTS: Tuple[Tuple[ProfileTier, float], ...] = (
    (ProfileTier.EMPTY, 0.10),
    (ProfileTier.LIGHT, 0.35),
    (ProfileTier.MEDIUM, 0.40),
    (ProfileTier.HEAVY, 0.15),
)


@dataclasses.dataclass(frozen=True)
class AccountPlan:
    account_id: int
    username: str


@dataclasses.dataclass(frozen=True)
class CharacterPlan:
    char_id: int
    name: str
    account_id: int
    slot: int
    inventory: ProfileTier
    storage: ProfileTier
    quest: ProfileTier


@dataclasses.dataclass(frozen=True)
class GuildPlan:
    guild_id: int
    name: str
    master_char_id: int
    member_count: int
    member_char_ids: Tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class PartyPlan:
    party_id: int
    name: str
    leader_char_id: int
    member_count: int
    member_char_ids: Tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class DatasetPlan:
    """Immutable, fully deterministic synthetic dataset plan."""

    seed: int
    accounts: Tuple[AccountPlan, ...]
    characters: Tuple[CharacterPlan, ...]
    guilds: Tuple[GuildPlan, ...]
    parties: Tuple[PartyPlan, ...]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _pick_profile(rng: random.Random) -> ProfileTier:
    """Deterministic weighted selection over the approved tiers."""
    draw = rng.random()
    cumulative = 0.0
    for tier, weight in PROFILE_WEIGHTS:
        cumulative += weight
        if draw < cumulative:
            return tier
    return ProfileTier.HEAVY


def _draw_size(rng: random.Random, minimum: int, maximum: int) -> int:
    return minimum + int(rng.random() * (maximum - minimum + 1))


def _trim_party_sizes(party_sizes: List[int], guild_total: int) -> None:
    """Deterministically shrink parties so memberships fit the characters."""
    overflow = guild_total + sum(party_sizes) - CHARACTER_TOTAL
    if overflow <= 0:
        return
    order = sorted(range(len(party_sizes)), key=lambda i: (-party_sizes[i], i))
    for index in order:
        if overflow <= 0:
            break
        reducible = party_sizes[index] - PARTY_MIN_MEMBERS
        take = min(reducible, overflow)
        party_sizes[index] -= take
        overflow -= take


def build_dataset_plan(seed: int) -> DatasetPlan:
    """Build the complete deterministic synthetic dataset plan for ``seed``."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    rng = random.Random(seed)

    accounts = tuple(
        AccountPlan(
            account_id=ACCOUNT_ID_BASE + index + 1,
            username=f"a3_account_{index + 1:06d}",
        )
        for index in range(ACCOUNT_TOTAL)
    )

    characters = tuple(
        CharacterPlan(
            char_id=CHARACTER_ID_BASE + index + 1,
            name=f"A3Char{index + 1:06d}",
            account_id=ACCOUNT_ID_BASE + index // CHARACTERS_PER_ACCOUNT + 1,
            slot=index % CHARACTERS_PER_ACCOUNT,
            inventory=_pick_profile(rng),
            storage=_pick_profile(rng),
            quest=_pick_profile(rng),
        )
        for index in range(CHARACTER_TOTAL)
    )

    guild_sizes = [
        _draw_size(rng, GUILD_MIN_MEMBERS, GUILD_MAX_MEMBERS)
        for _ in range(GUILD_TOTAL)
    ]
    party_sizes = [
        _draw_size(rng, PARTY_MIN_MEMBERS, PARTY_MAX_MEMBERS)
        for _ in range(PARTY_TOTAL)
    ]
    _trim_party_sizes(party_sizes, sum(guild_sizes))

    # Stable shuffled assignment guarantees unique memberships per character.
    char_ids = [character.char_id for character in characters]
    rng.shuffle(char_ids)

    guilds: List[GuildPlan] = []
    parties: List[PartyPlan] = []
    cursor = 0
    for index, size in enumerate(guild_sizes):
        members = tuple(char_ids[cursor : cursor + size])
        cursor += size
        guilds.append(
            GuildPlan(
                guild_id=GUILD_ID_BASE + index + 1,
                name=f"A3Guild{index + 1:03d}",
                master_char_id=members[0],
                member_count=len(members),
                member_char_ids=members,
            )
        )
    for index, size in enumerate(party_sizes):
        members = tuple(char_ids[cursor : cursor + size])
        cursor += size
        parties.append(
            PartyPlan(
                party_id=PARTY_ID_BASE + index + 1,
                name=f"A3Party{index + 1:03d}",
                leader_char_id=members[0],
                member_count=len(members),
                member_char_ids=members,
            )
        )

    return DatasetPlan(
        seed=seed,
        accounts=accounts,
        characters=characters,
        guilds=tuple(guilds),
        parties=tuple(parties),
    )


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------


def _tier_value(tier: Any) -> str:
    return tier.value if isinstance(tier, ProfileTier) else str(tier)


def serialize_dataset_plan(plan: DatasetPlan) -> Dict[str, Any]:
    """Serialize ``plan`` to a plain dict with explicit stable ordering."""
    return {
        "dataset_version": DATASET_VERSION,
        "seed": plan.seed,
        "row_counts": {
            "accounts": len(plan.accounts),
            "characters": len(plan.characters),
            "guilds": len(plan.guilds),
            "parties": len(plan.parties),
        },
        "accounts": [
            {"account_id": account.account_id, "username": account.username}
            for account in plan.accounts
        ],
        "characters": [
            {
                "char_id": character.char_id,
                "name": character.name,
                "account_id": character.account_id,
                "slot": character.slot,
                "inventory": _tier_value(character.inventory),
                "storage": _tier_value(character.storage),
                "quest": _tier_value(character.quest),
            }
            for character in plan.characters
        ],
        "guilds": [
            {
                "guild_id": guild.guild_id,
                "name": guild.name,
                "master_char_id": guild.master_char_id,
                "member_count": guild.member_count,
                "member_char_ids": list(guild.member_char_ids),
            }
            for guild in plan.guilds
        ],
        "parties": [
            {
                "party_id": party.party_id,
                "name": party.name,
                "leader_char_id": party.leader_char_id,
                "member_count": party.member_count,
                "member_char_ids": list(party.member_char_ids),
            }
            for party in plan.parties
        ],
    }


def dataset_plan_sha256(plan: DatasetPlan) -> str:
    """SHA-256 over the canonical JSON of the serialized plan."""
    payload = json.dumps(
        serialize_dataset_plan(plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def profile_tier_counts(plan: DatasetPlan) -> Dict[str, Dict[str, int]]:
    """Per-dimension profile tier counts in stable tier order."""
    counts = {
        dimension: {tier.value: 0 for tier in ProfileTier}
        for dimension in DIMENSIONS
    }
    for character in plan.characters:
        for dimension in DIMENSIONS:
            counts[dimension][_tier_value(getattr(character, dimension))] += 1
    return counts


def relationship_counts(plan: DatasetPlan) -> Dict[str, int]:
    return {
        "characters_per_account": CHARACTERS_PER_ACCOUNT,
        "guild_memberships": sum(len(guild.member_char_ids) for guild in plan.guilds),
        "party_memberships": sum(len(party.member_char_ids) for party in plan.parties),
    }


# ---------------------------------------------------------------------------
# SQL emission (staging only)
# ---------------------------------------------------------------------------


def _sql_literal(value: Any) -> str:
    """Render a SQL literal with deterministic single-quote escaping.

    Raises :class:`ValueError` on NUL bytes and :class:`TypeError` on
    unsupported types (booleans included).
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"unsupported SQL literal type: {type(value).__name__}")
    if isinstance(value, int):
        return str(value)
    if "\x00" in value:
        raise ValueError("NUL byte is not allowed in a SQL literal")
    return "'" + value.replace("'", "''") + "'"


def _write_insert(handle, table: str, columns: Tuple[str, ...], rows) -> None:
    handle.write(f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n")
    first = True
    for row in rows:
        literals = ", ".join(_sql_literal(value) for value in row)
        handle.write(("  (" if first else ", (") + literals + ")\n")
        first = False
    handle.write(";\n\n")


def _write_sql(plan: DatasetPlan, handle) -> None:
    handle.write("-- A3 SYNTHETIC PERFORMANCE DATASET\n")
    handle.write("-- NOT FOR PRODUCTION PLAYER DATA\n")
    handle.write(f"-- Generated deterministically from seed: {plan.seed}\n")
    handle.write(
        "-- Password hash placeholder is synthetic-only and not production-secure;\n"
    )
    handle.write(
        "-- authentication integration may replace it before real load runs.\n\n"
    )

    handle.write("START TRANSACTION;\n\n")

    handle.write("-- Section: accounts\n")
    _write_insert(
        handle,
        "a3_stage_accounts",
        ("account_id", "username", "password_hash"),
        (
            (account.account_id, account.username, PASSWORD_HASH_PLACEHOLDER)
            for account in plan.accounts
        ),
    )

    handle.write("-- Section: characters\n")
    _write_insert(
        handle,
        "a3_stage_characters",
        ("char_id", "name", "account_id", "slot"),
        (
            (c.char_id, c.name, c.account_id, c.slot) for c in plan.characters
        ),
    )

    handle.write("-- Section: guilds\n")
    _write_insert(
        handle,
        "a3_stage_guilds",
        ("guild_id", "name", "master_char_id", "member_count"),
        (
            (g.guild_id, g.name, g.master_char_id, g.member_count)
            for g in plan.guilds
        ),
    )

    handle.write("-- Section: guild memberships\n")
    _write_insert(
        handle,
        "a3_stage_guild_members",
        ("guild_id", "char_id", "position"),
        (
            (guild.guild_id, char_id, position)
            for guild in plan.guilds
            for position, char_id in enumerate(guild.member_char_ids)
        ),
    )

    handle.write("-- Section: parties\n")
    _write_insert(
        handle,
        "a3_stage_parties",
        ("party_id", "name", "leader_char_id", "member_count"),
        (
            (p.party_id, p.name, p.leader_char_id, p.member_count)
            for p in plan.parties
        ),
    )

    handle.write("-- Section: party memberships\n")
    _write_insert(
        handle,
        "a3_stage_party_members",
        ("party_id", "char_id", "position"),
        (
            (party.party_id, char_id, position)
            for party in plan.parties
            for position, char_id in enumerate(party.member_char_ids)
        ),
    )

    handle.write("-- Section: profile metadata\n")
    _write_insert(
        handle,
        "a3_stage_profile_metadata",
        ("char_id", "dimension", "tier"),
        (
            (character.char_id, dimension, _tier_value(getattr(character, dimension)))
            for character in plan.characters
            for dimension in DIMENSIONS
        ),
    )

    handle.write("COMMIT;\n")


def emit_dataset_sql(plan: DatasetPlan, output_path: Path) -> Dict[str, Any]:
    """Write deterministic staging SQL and a metadata sidecar.

    The SQL is streamed through a sibling temporary file, flushed, fsynced,
    and moved into place with :func:`os.replace`; temporary files are removed
    on failure. Returns the sidecar metadata dict (also written to
    ``<output_path>.metadata.json`` via :func:`write_json_atomic`).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            _write_sql(plan, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    relationships = relationship_counts(plan)
    metadata: Dict[str, Any] = {
        "dataset_version": DATASET_VERSION,
        "seed": plan.seed,
        "generated_from": GENERATED_FROM,
        "row_counts": {
            "accounts": len(plan.accounts),
            "characters": len(plan.characters),
            "guilds": len(plan.guilds),
            "parties": len(plan.parties),
            "guild_memberships": relationships["guild_memberships"],
            "party_memberships": relationships["party_memberships"],
        },
        "profile_counts": profile_tier_counts(plan),
        "relationship_counts": relationships,
        "sql_sha256": sha256_file(output_path),
        "plan_sha256": dataset_plan_sha256(plan),
        "expected_foreign_key_relationships": dict(
            EXPECTED_FOREIGN_KEY_RELATIONSHIPS
        ),
        "contains_real_player_data": False,
    }
    write_json_atomic(Path(str(output_path) + ".metadata.json"), metadata)
    return metadata


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_dataset_counts(actual: Mapping[str, Any], plan: DatasetPlan) -> List[str]:
    """Compare observed row counts against ``plan``.

    Returns one sorted deterministic message per discrepancy:
    ``<field> missing``, ``<field> unexpected``,
    ``<field> invalid <value>`` (boolean, non-integer, or negative), or
    ``<field> expected <n> got <m>``.
    """
    relationships = relationship_counts(plan)
    expected = {
        "accounts": len(plan.accounts),
        "characters": len(plan.characters),
        "guilds": len(plan.guilds),
        "parties": len(plan.parties),
        "guild_memberships": relationships["guild_memberships"],
        "party_memberships": relationships["party_memberships"],
    }
    messages: List[str] = []
    for field in COUNT_FIELDS:
        if field not in actual:
            messages.append(f"{field} missing")
            continue
        value = actual[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            messages.append(f"{field} invalid {value!r}")
            continue
        if value != expected[field]:
            messages.append(f"{field} expected {expected[field]} got {value}")
    for field in actual:
        if field not in expected:
            messages.append(f"{field} unexpected")
    return sorted(messages)


def verify_dataset_relationships(plan: DatasetPlan) -> List[str]:
    """Verify referential integrity of ``plan`` with sorted messages."""
    messages: List[str] = []

    # Accounts: unique IDs and usernames.
    account_ids: set = set()
    usernames: set = set()
    for account in plan.accounts:
        if account.account_id in account_ids:
            messages.append(f"duplicate account_id {account.account_id}")
        account_ids.add(account.account_id)
        if account.username in usernames:
            messages.append(f"duplicate username {account.username}")
        usernames.add(account.username)

    # Characters: unique IDs/names, valid accounts, slots, valid tiers.
    char_ids: set = set()
    char_names: set = set()
    per_account: Dict[int, int] = {}
    seen_slots: set = set()
    valid_tiers = {tier.value for tier in ProfileTier}
    dimension_tiers: Dict[str, set] = {dimension: set() for dimension in DIMENSIONS}
    for character in plan.characters:
        if character.char_id in char_ids:
            messages.append(f"duplicate char_id {character.char_id}")
        char_ids.add(character.char_id)
        if character.name in char_names:
            messages.append(f"duplicate character name {character.name}")
        char_names.add(character.name)
        if character.account_id not in account_ids:
            messages.append(
                f"character {character.char_id} references missing account "
                f"{character.account_id}"
            )
        else:
            per_account[character.account_id] = (
                per_account.get(character.account_id, 0) + 1
            )
            slot_key = (character.account_id, character.slot)
            if slot_key in seen_slots:
                messages.append(
                    f"account {character.account_id} duplicate slot {character.slot}"
                )
            seen_slots.add(slot_key)
        for dimension in DIMENSIONS:
            value = _tier_value(getattr(character, dimension))
            if value not in valid_tiers:
                messages.append(
                    f"character {character.char_id} unsupported {dimension} "
                    f"profile tier {value}"
                )
            else:
                dimension_tiers[dimension].add(value)

    for account_id in sorted(per_account):
        count = per_account[account_id]
        if count != CHARACTERS_PER_ACCOUNT:
            messages.append(
                f"account {account_id} has {count} characters expected "
                f"{CHARACTERS_PER_ACCOUNT}"
            )
    for account in plan.accounts:
        if account.account_id not in per_account:
            messages.append(
                f"account {account.account_id} has 0 characters expected "
                f"{CHARACTERS_PER_ACCOUNT}"
            )

    for dimension in DIMENSIONS:
        for tier in sorted(valid_tiers - dimension_tiers[dimension]):
            messages.append(f"{dimension} missing profile tier {tier}")

    # Guilds.
    masters: set = set()
    guild_membership: Dict[int, int] = {}
    for guild in plan.guilds:
        if guild.master_char_id not in char_ids:
            messages.append(
                f"guild {guild.guild_id} master {guild.master_char_id} "
                f"missing character"
            )
        if guild.master_char_id in masters:
            messages.append(f"duplicate guild master {guild.master_char_id}")
        masters.add(guild.master_char_id)
        if guild.member_count != len(guild.member_char_ids):
            messages.append(
                f"guild {guild.guild_id} member_count {guild.member_count} "
                f"does not match membership {len(guild.member_char_ids)}"
            )
        for char_id in guild.member_char_ids:
            if char_id not in char_ids:
                messages.append(
                    f"guild {guild.guild_id} member {char_id} missing character"
                )
            guild_membership[char_id] = guild_membership.get(char_id, 0) + 1
    for char_id in sorted(guild_membership):
        if guild_membership[char_id] > 1:
            messages.append(f"character {char_id} in multiple guilds")

    # Parties.
    leaders: set = set()
    party_membership: Dict[int, int] = {}
    for party in plan.parties:
        if party.leader_char_id not in char_ids:
            messages.append(
                f"party {party.party_id} leader {party.leader_char_id} "
                f"missing character"
            )
        if party.leader_char_id in leaders:
            messages.append(f"duplicate party leader {party.leader_char_id}")
        leaders.add(party.leader_char_id)
        if party.member_count != len(party.member_char_ids):
            messages.append(
                f"party {party.party_id} member_count {party.member_count} "
                f"does not match membership {len(party.member_char_ids)}"
            )
        for char_id in party.member_char_ids:
            if char_id not in char_ids:
                messages.append(
                    f"party {party.party_id} member {char_id} missing character"
                )
            party_membership[char_id] = party_membership.get(char_id, 0) + 1
    for char_id in sorted(party_membership):
        if party_membership[char_id] > 1:
            messages.append(f"character {char_id} in multiple parties")

    return sorted(messages)
