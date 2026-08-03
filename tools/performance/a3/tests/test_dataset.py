"""Tests for the deterministic A3 synthetic dataset planner."""

import copy
import dataclasses
import json
import unittest
import tempfile
from pathlib import Path

from tools.performance.a3.dataset import (
    AccountPlan,
    CharacterPlan,
    DatasetPlan,
    GuildPlan,
    PartyPlan,
    ProfileTier,
    build_dataset_plan,
    dataset_plan_sha256,
    emit_dataset_sql,
    serialize_dataset_plan,
    verify_dataset_counts,
    verify_dataset_relationships,
)
from tools.performance.a3.io import read_json

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
PLAN_FIXTURE = FIXTURE_DIR / "dataset-plan-seed-20260802.json"
COUNTS_FIXTURE = FIXTURE_DIR / "dataset-counts-valid.json"

SEED = 20260802

EXPECTED_TOTALS = {"accounts": 6000, "characters": 12000, "guilds": 200, "parties": 500}

PROFILE_WEIGHTS = {"empty": 0.10, "light": 0.35, "medium": 0.40, "heavy": 0.15}


def load_plan_fixture() -> dict:
    with open(PLAN_FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


_PLAN_CACHE: dict = {}


def cached_plan(seed: int = SEED) -> DatasetPlan:
    if seed not in _PLAN_CACHE:
        _PLAN_CACHE[seed] = build_dataset_plan(seed)
    return _PLAN_CACHE[seed]


def profile_counts(plan: DatasetPlan) -> dict:
    counts = {
        dimension: {tier.value: 0 for tier in ProfileTier}
        for dimension in ("inventory", "storage", "quest")
    }
    for character in plan.characters:
        counts["inventory"][character.inventory.value] += 1
        counts["storage"][character.storage.value] += 1
        counts["quest"][character.quest.value] += 1
    return counts


class DeterministicPlanningTests(unittest.TestCase):
    def test_same_seed_produces_equal_plan(self):
        self.assertEqual(build_dataset_plan(SEED), build_dataset_plan(SEED))

    def test_same_seed_canonical_serialization_identical(self):
        first = json.dumps(
            serialize_dataset_plan(build_dataset_plan(SEED)),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        second = json.dumps(
            serialize_dataset_plan(build_dataset_plan(SEED)),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.assertEqual(first, second)

    def test_same_seed_plan_sha_identical(self):
        self.assertEqual(
            dataset_plan_sha256(build_dataset_plan(SEED)),
            dataset_plan_sha256(build_dataset_plan(SEED)),
        )

    def test_different_seed_changes_assignments(self):
        base = build_dataset_plan(SEED)
        other = build_dataset_plan(SEED + 1)
        self.assertNotEqual(
            [c.inventory for c in base.characters],
            [c.inventory for c in other.characters],
        )
        self.assertNotEqual(
            [g.member_char_ids for g in base.guilds],
            [g.member_char_ids for g in other.guilds],
        )
        self.assertNotEqual(dataset_plan_sha256(base), dataset_plan_sha256(other))

    def test_different_seed_preserves_totals(self):
        other = build_dataset_plan(SEED + 1)
        self.assertEqual(len(other.accounts), 6000)
        self.assertEqual(len(other.characters), 12000)
        self.assertEqual(len(other.guilds), 200)
        self.assertEqual(len(other.parties), 500)


class ExactTotalsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def test_account_total(self):
        self.assertEqual(len(self.plan.accounts), 6000)

    def test_character_total(self):
        self.assertEqual(len(self.plan.characters), 12000)

    def test_guild_total(self):
        self.assertEqual(len(self.plan.guilds), 200)

    def test_party_total(self):
        self.assertEqual(len(self.plan.parties), 500)

    def test_exactly_two_characters_per_account(self):
        counts = {}
        for character in self.plan.characters:
            counts[character.account_id] = counts.get(character.account_id, 0) + 1
        self.assertEqual(len(counts), 6000)
        self.assertTrue(all(count == 2 for count in counts.values()))

    def test_memberships_do_not_exceed_characters(self):
        guild_members = sum(len(g.member_char_ids) for g in self.plan.guilds)
        party_members = sum(len(p.member_char_ids) for p in self.plan.parties)
        self.assertLessEqual(guild_members + party_members, 12000)

    def test_guild_sizes_within_bounds(self):
        for guild in self.plan.guilds:
            self.assertGreaterEqual(len(guild.member_char_ids), 4)
            self.assertLessEqual(len(guild.member_char_ids), 40)

    def test_party_sizes_within_bounds(self):
        for party in self.plan.parties:
            self.assertGreaterEqual(len(party.member_char_ids), 2)
            self.assertLessEqual(len(party.member_char_ids), 12)


class StableIdentifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def test_account_ids_and_usernames(self):
        self.assertEqual(self.plan.accounts[0].account_id, 2000001)
        self.assertEqual(self.plan.accounts[0].username, "a3_account_000001")
        self.assertEqual(self.plan.accounts[-1].account_id, 2006000)
        self.assertEqual(self.plan.accounts[-1].username, "a3_account_006000")

    def test_character_ids_names_and_slots(self):
        first, second = self.plan.characters[0], self.plan.characters[1]
        self.assertEqual((first.char_id, first.name, first.slot), (3000001, "A3Char000001", 0))
        self.assertEqual((second.char_id, second.name, second.slot), (3000002, "A3Char000002", 1))
        self.assertEqual(first.account_id, second.account_id)
        last = self.plan.characters[-1]
        self.assertEqual((last.char_id, last.name, last.slot), (3012000, "A3Char012000", 1))
        self.assertEqual(last.account_id, 2006000)

    def test_guild_ids_and_names(self):
        self.assertEqual(self.plan.guilds[0].guild_id, 400001)
        self.assertEqual(self.plan.guilds[0].name, "A3Guild001")
        self.assertEqual(self.plan.guilds[-1].guild_id, 400200)
        self.assertEqual(self.plan.guilds[-1].name, "A3Guild200")

    def test_party_ids_and_names(self):
        self.assertEqual(self.plan.parties[0].party_id, 500001)
        self.assertEqual(self.plan.parties[0].name, "A3Party001")
        self.assertEqual(self.plan.parties[-1].party_id, 500500)
        self.assertEqual(self.plan.parties[-1].name, "A3Party500")


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def test_only_approved_tiers(self):
        valid = set(ProfileTier)
        for character in self.plan.characters:
            self.assertIn(character.inventory, valid)
            self.assertIn(character.storage, valid)
            self.assertIn(character.quest, valid)

    def test_each_dimension_has_12000_assignments(self):
        counts = profile_counts(self.plan)
        for dimension in ("inventory", "storage", "quest"):
            self.assertEqual(sum(counts[dimension].values()), 12000)

    def test_all_four_tiers_present_per_dimension(self):
        counts = profile_counts(self.plan)
        for dimension in ("inventory", "storage", "quest"):
            for tier in ProfileTier:
                self.assertGreater(counts[dimension][tier.value], 0)

    def test_seed_20260802_profile_counts_locked_by_fixture(self):
        fixture = load_plan_fixture()
        self.assertEqual(profile_counts(self.plan), fixture["profile_counts"])

    def test_profile_counts_deterministic(self):
        self.assertEqual(
            profile_counts(build_dataset_plan(SEED)),
            profile_counts(build_dataset_plan(SEED)),
        )

    def test_fixture_locks_plan_sha_and_totals(self):
        fixture = load_plan_fixture()
        self.assertEqual(fixture["seed"], SEED)
        self.assertEqual(dataset_plan_sha256(self.plan), fixture["plan_sha256"])
        self.assertEqual(
            fixture["row_counts"],
            {
                "accounts": 6000,
                "characters": 12000,
                "guilds": 200,
                "parties": 500,
            },
        )


class RelationshipIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def test_valid_plan_has_no_relationship_errors(self):
        self.assertEqual(verify_dataset_relationships(self.plan), [])

    def _corrupt_characters(self, index, **changes):
        characters = list(self.plan.characters)
        characters[index] = dataclasses.replace(characters[index], **changes)
        return dataclasses.replace(self.plan, characters=tuple(characters))

    def _corrupt_accounts(self, index, **changes):
        accounts = list(self.plan.accounts)
        accounts[index] = dataclasses.replace(accounts[index], **changes)
        return dataclasses.replace(self.plan, accounts=tuple(accounts))

    def _corrupt_guilds(self, index, **changes):
        guilds = list(self.plan.guilds)
        guilds[index] = dataclasses.replace(guilds[index], **changes)
        return dataclasses.replace(self.plan, guilds=tuple(guilds))

    def _corrupt_parties(self, index, **changes):
        parties = list(self.plan.parties)
        parties[index] = dataclasses.replace(parties[index], **changes)
        return dataclasses.replace(self.plan, parties=tuple(parties))

    def test_duplicate_account_id_detected(self):
        plan = self._corrupt_accounts(1, account_id=self.plan.accounts[0].account_id)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate account_id" in e for e in errors), errors)

    def test_duplicate_username_detected(self):
        plan = self._corrupt_accounts(1, username=self.plan.accounts[0].username)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate username" in e for e in errors), errors)

    def test_duplicate_character_id_detected(self):
        plan = self._corrupt_characters(1, char_id=self.plan.characters[0].char_id)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate char_id" in e for e in errors), errors)

    def test_duplicate_character_name_detected(self):
        plan = self._corrupt_characters(1, name=self.plan.characters[0].name)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate character name" in e for e in errors), errors)

    def test_nonexistent_account_reference_detected(self):
        plan = self._corrupt_characters(0, account_id=2999999)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("missing account" in e for e in errors), errors)

    def test_wrong_characters_per_account_detected(self):
        other_account = self.plan.characters[2].account_id
        plan = self._corrupt_characters(2, account_id=self.plan.characters[0].account_id)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(
            any("characters expected 2" in e for e in errors), (other_account, errors)
        )

    def test_duplicate_slot_detected(self):
        plan = self._corrupt_characters(1, slot=0)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate slot" in e for e in errors), errors)

    def test_nonexistent_guild_master_detected(self):
        plan = self._corrupt_guilds(0, master_char_id=3999999)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("master" in e and "missing character" in e for e in errors), errors)

    def test_duplicate_guild_master_detected(self):
        plan = self._corrupt_guilds(1, master_char_id=self.plan.guilds[0].master_char_id)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate guild master" in e for e in errors), errors)

    def test_nonexistent_guild_member_detected(self):
        members = list(self.plan.guilds[0].member_char_ids)
        members[1] = 3999999
        plan = self._corrupt_guilds(0, member_char_ids=tuple(members))
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("member" in e and "missing character" in e for e in errors), errors)

    def test_character_in_multiple_guilds_detected(self):
        members = list(self.plan.guilds[1].member_char_ids)
        members[1] = self.plan.guilds[0].member_char_ids[1]
        plan = self._corrupt_guilds(1, member_char_ids=tuple(members))
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("multiple guilds" in e for e in errors), errors)

    def test_guild_member_count_mismatch_detected(self):
        guild = self.plan.guilds[0]
        plan = self._corrupt_guilds(0, member_count=len(guild.member_char_ids) + 1)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("member_count" in e for e in errors), errors)

    def test_nonexistent_party_leader_detected(self):
        plan = self._corrupt_parties(0, leader_char_id=3999999)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("leader" in e and "missing character" in e for e in errors), errors)

    def test_duplicate_party_leader_detected(self):
        plan = self._corrupt_parties(1, leader_char_id=self.plan.parties[0].leader_char_id)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("duplicate party leader" in e for e in errors), errors)

    def test_nonexistent_party_member_detected(self):
        members = list(self.plan.parties[0].member_char_ids)
        members[1] = 3999999
        plan = self._corrupt_parties(0, member_char_ids=tuple(members))
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("member" in e and "missing character" in e for e in errors), errors)

    def test_character_in_multiple_parties_detected(self):
        members = list(self.plan.parties[1].member_char_ids)
        members[1] = self.plan.parties[0].member_char_ids[1]
        plan = self._corrupt_parties(1, member_char_ids=tuple(members))
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("multiple parties" in e for e in errors), errors)

    def test_party_member_count_mismatch_detected(self):
        party = self.plan.parties[0]
        plan = self._corrupt_parties(0, member_count=len(party.member_char_ids) + 1)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("member_count" in e for e in errors), errors)

    def test_unsupported_profile_tier_detected(self):
        plan = self._corrupt_characters(0, inventory="ultra")
        errors = verify_dataset_relationships(plan)
        self.assertTrue(any("unsupported" in e and "inventory" in e for e in errors), errors)

    def test_missing_profile_tier_detected(self):
        characters = tuple(
            dataclasses.replace(c, quest=ProfileTier.EMPTY) for c in self.plan.characters
        )
        plan = dataclasses.replace(self.plan, characters=characters)
        errors = verify_dataset_relationships(plan)
        self.assertTrue(
            any("quest" in e and "missing profile tier" in e for e in errors), errors
        )


class CountVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()
        cls.valid = read_json(COUNTS_FIXTURE)

    def test_fixture_counts_pass(self):
        self.assertEqual(verify_dataset_counts(self.valid, self.plan), [])

    def test_fixture_has_exactly_required_keys(self):
        self.assertEqual(
            set(self.valid),
            {
                "accounts",
                "characters",
                "guilds",
                "parties",
                "guild_memberships",
                "party_memberships",
            },
        )

    def test_mismatch_messages(self):
        actual = dict(self.valid, accounts=5999, characters=11998, guilds=201)
        self.assertEqual(
            verify_dataset_counts(actual, self.plan),
            [
                "accounts expected 6000 got 5999",
                "characters expected 12000 got 11998",
                "guilds expected 200 got 201",
            ],
        )

    def test_missing_key_message(self):
        actual = dict(self.valid)
        del actual["parties"]
        self.assertEqual(verify_dataset_counts(actual, self.plan), ["parties missing"])

    def test_unexpected_key_message(self):
        actual = dict(self.valid, npcs=10)
        self.assertEqual(verify_dataset_counts(actual, self.plan), ["npcs unexpected"])

    def test_boolean_counts_rejected(self):
        actual = dict(self.valid, accounts=True)
        errors = verify_dataset_counts(actual, self.plan)
        self.assertEqual(len(errors), 1)
        self.assertIn("accounts", errors[0])
        self.assertIn("invalid", errors[0])

    def test_negative_counts_rejected(self):
        actual = dict(self.valid, parties=-1)
        errors = verify_dataset_counts(actual, self.plan)
        self.assertEqual(len(errors), 1)
        self.assertIn("parties", errors[0])
        self.assertIn("invalid", errors[0])

    def test_messages_sorted(self):
        actual = dict(self.valid, guilds=0)
        del actual["accounts"]
        actual["zzz"] = 1
        errors = verify_dataset_counts(actual, self.plan)
        self.assertEqual(errors, sorted(errors))
        self.assertEqual(len(errors), 3)


class SqlEmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.output = self.tmp_dir / "a3-dataset.sql"

    def _emit(self, plan=None):
        return emit_dataset_sql(plan or self.plan, self.output)

    def test_same_plan_produces_byte_identical_sql(self):
        self._emit()
        first = self.output.read_bytes()
        other = self.tmp_dir / "second.sql"
        emit_dataset_sql(self.plan, other)
        self.assertEqual(first, other.read_bytes())

    def test_required_header_present(self):
        self._emit()
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("-- A3 SYNTHETIC PERFORMANCE DATASET", text)
        self.assertIn("-- NOT FOR PRODUCTION PLAYER DATA", text)
        self.assertIn(f"-- Generated deterministically from seed: {SEED}", text)

    def test_section_order_stable(self):
        self._emit()
        text = self.output.read_text(encoding="utf-8")
        markers = [
            "START TRANSACTION;",
            "a3_stage_accounts",
            "a3_stage_characters",
            "a3_stage_guilds",
            "a3_stage_guild_members",
            "a3_stage_parties",
            "a3_stage_party_members",
            "a3_stage_profile_metadata",
            "COMMIT;",
        ]
        positions = []
        for marker in markers:
            position = text.find(marker)
            self.assertGreaterEqual(position, 0, marker)
            positions.append(position)
        self.assertEqual(positions, sorted(positions))

    def test_inserts_use_explicit_columns(self):
        self._emit()
        text = self.output.read_text(encoding="utf-8")
        self.assertIn(
            "INSERT INTO a3_stage_accounts (account_id, username, password_hash) VALUES",
            text,
        )
        self.assertIn("INSERT INTO a3_stage_characters (char_id, name, account_id, slot)", text)

    def test_sql_quote_escaping(self):
        characters = list(self.plan.characters)
        characters[0] = dataclasses.replace(characters[0], name="O'Brien")
        plan = dataclasses.replace(self.plan, characters=tuple(characters))
        emit_dataset_sql(plan, self.output)
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("'O''Brien'", text)

    def test_nul_byte_rejected_and_temp_files_removed(self):
        characters = list(self.plan.characters)
        characters[0] = dataclasses.replace(characters[0], name="bad\x00name")
        plan = dataclasses.replace(self.plan, characters=tuple(characters))
        with self.assertRaises(ValueError):
            emit_dataset_sql(plan, self.output)
        self.assertEqual(list(self.tmp_dir.iterdir()), [])

    def test_sidecar_generated_and_matches_return(self):
        metadata = self._emit()
        sidecar = Path(str(self.output) + ".metadata.json")
        self.assertTrue(sidecar.is_file())
        self.assertEqual(read_json(sidecar), metadata)

    def test_metadata_contents(self):
        metadata = self._emit()
        self.assertEqual(metadata["dataset_version"], 1)
        self.assertEqual(metadata["seed"], SEED)
        self.assertEqual(
            metadata["generated_from"], "A3 deterministic synthetic dataset planner"
        )
        self.assertIs(metadata["contains_real_player_data"], False)
        self.assertEqual(metadata["row_counts"]["accounts"], 6000)
        self.assertEqual(metadata["row_counts"]["characters"], 12000)
        self.assertEqual(metadata["row_counts"]["guilds"], 200)
        self.assertEqual(metadata["row_counts"]["parties"], 500)
        self.assertEqual(
            metadata["row_counts"]["guild_memberships"],
            sum(len(g.member_char_ids) for g in self.plan.guilds),
        )
        self.assertEqual(
            metadata["row_counts"]["party_memberships"],
            sum(len(p.member_char_ids) for p in self.plan.parties),
        )
        self.assertEqual(metadata["profile_counts"], profile_counts(self.plan))
        self.assertEqual(
            set(metadata["expected_foreign_key_relationships"]),
            {
                "a3_stage_characters.account_id",
                "a3_stage_guilds.master_char_id",
                "a3_stage_guild_members.char_id",
                "a3_stage_parties.leader_char_id",
                "a3_stage_party_members.char_id",
            },
        )

    def test_sql_checksum_correct(self):
        from tools.performance.a3.io import sha256_file

        metadata = self._emit()
        self.assertEqual(metadata["sql_sha256"], sha256_file(self.output))

    def test_plan_checksum_correct(self):
        metadata = self._emit()
        self.assertEqual(metadata["plan_sha256"], dataset_plan_sha256(self.plan))


class SqlSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output = Path(self._tmp.name) / "a3-dataset.sql"
        emit_dataset_sql(self.plan, self.output)
        self.text = self.output.read_text(encoding="utf-8")

    def test_no_email_like_strings(self):
        self.assertNotIn("@", self.text)

    def test_no_obvious_credential_keys(self):
        lowered = self.text.lower()
        for marker in ("password=", "secret=", "api_key", "private_key", "BEGIN PRIVATE"):
            self.assertNotIn(marker, lowered)

    def test_password_placeholder_is_clearly_synthetic(self):
        self.assertIn("A3_SYNTHETIC_PASSWORD_HASH_PLACEHOLDER", self.text)
        self.assertIn("not production-secure", self.text)

    def test_no_timestamp_or_host_specific_values(self):
        import re

        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", self.text))

    def test_no_real_data_input_interface(self):
        import inspect

        import tools.performance.a3.dataset as dataset_module

        source = inspect.getsource(dataset_module)
        self.assertNotIn("pymysql", source)
        self.assertNotIn("socket", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("requests", source)


class ImmutabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = cached_plan()

    def test_plan_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.plan.seed = 1

    def test_records_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.plan.accounts[0].username = "x"

    def test_public_collections_are_tuples(self):
        self.assertIsInstance(self.plan.accounts, tuple)
        self.assertIsInstance(self.plan.characters, tuple)
        self.assertIsInstance(self.plan.guilds, tuple)
        self.assertIsInstance(self.plan.parties, tuple)
        self.assertIsInstance(self.plan.guilds[0].member_char_ids, tuple)
        self.assertIsInstance(self.plan.parties[0].member_char_ids, tuple)

    def test_plan_types_exposed(self):
        self.assertIsInstance(self.plan.accounts[0], AccountPlan)
        self.assertIsInstance(self.plan.characters[0], CharacterPlan)
        self.assertIsInstance(self.plan.guilds[0], GuildPlan)
        self.assertIsInstance(self.plan.parties[0], PartyPlan)
        self.assertIsInstance(self.plan.characters[0].inventory, ProfileTier)


if __name__ == "__main__":
    unittest.main()
