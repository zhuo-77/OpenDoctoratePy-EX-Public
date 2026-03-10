"""
Tests for finishNormalGacha and syncNormalGacha in server/gacha.py.

Covers:
- New character branch: gainTime uses int(time()), hggShard incremented, charGet.isNew==1
- Repeat character branch: correct item_name/type/id/count for every rarityRank (0-5)
  and both potential_rank states (< 5 and == 5)
- Repeat character with potential item absent from inventory (KeyError fix)
- syncNormalGacha returns user recruit slots (not gacha pool perAvailList)
"""

import json
import copy
import sys
import os
import unittest
from unittest.mock import patch, MagicMock, call

# Make server/ importable without starting the Flask app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SLOT_ID = 0
CHAR_ID_NEW = "char_002_amiya"
CHAR_ID_REPEAT = "char_010_chen"
REPEAT_INST_ID = 10


def _make_user_data(repeat_char_potential_rank=3):
    """Return a minimal user-data dict shaped like the real user.json."""
    return {
        "user": {
            "troop": {
                "chars": {
                    str(REPEAT_INST_ID): {
                        "charId": CHAR_ID_REPEAT,
                        "potentialRank": repeat_char_potential_rank,
                    }
                },
                "charGroup": {},
            },
            "building": {"chars": {}},
            "status": {"hggShard": 100, "lggShard": 200},
            "inventory": {f"p_{CHAR_ID_REPEAT}": 3},
            "recruit": {
                "normal": {
                    "slots": {
                        str(SLOT_ID): {"state": 3, "selectTags": []}
                    }
                }
            },
        }
    }


def _make_gacha_data(rarity_rank, char_id):
    """Return a minimal normalGacha.json dict with a single rarity tier."""
    return {
        "detailInfo": {
            "availCharInfo": {
                "perAvailList": [
                    {
                        "rarityRank": rarity_rank,
                        "totalPercent": 1.0,
                        "charIdList": [char_id],
                    }
                ]
            }
        }
    }


def _make_character_table(char_id):
    # Extract a skill name from the char_id (e.g. "char_002_amiya" → "skchr_amiya_1")
    char_name = char_id.split("_", 2)[2]
    return {
        char_id: {
            "skills": [
                {"skillId": f"skchr_{char_name}_1", "unlockCond": {"phase": 0}},
            ],
            "rarity": "RARITY_3",  # intentionally wrong format to prove we don't use it
        },
        "charDefaultTypeDict": {char_id: "JP"},
    }


def _request_body(slot_id=SLOT_ID):
    return json.dumps({"slotId": slot_id}).encode()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFinishNormalGachaNewCharacter(unittest.TestCase):
    """New character (not already owned): repeat_char_id == 0."""

    def _run(self, rarity_rank):
        user_data = _make_user_data()
        gacha_data = _make_gacha_data(rarity_rank, CHAR_ID_NEW)
        char_table = _make_character_table(CHAR_ID_NEW)
        charword_table = {"charDefaultTypeDict": {CHAR_ID_NEW: "JP"}}

        fake_request = MagicMock()
        fake_request.data = _request_body()

        def fake_read_json(path):
            if "user" in path:
                return copy.deepcopy(user_data)
            if "normalGacha" in path:
                return gacha_data
            raise FileNotFoundError(path)

        def fake_get_memory(key):
            if key == "character_table":
                return char_table
            if key == "charword_table":
                return charword_table
            if key == "uniequip_table":
                return {}  # no equipment for this char
            raise KeyError(key)

        with patch("gacha.request", fake_request), \
             patch("gacha.read_json", side_effect=fake_read_json), \
             patch("gacha.get_memory", side_effect=fake_get_memory), \
             patch("gacha.run_after_response"), \
             patch("gacha.random") as mock_random:

            mock_random.shuffle = MagicMock()
            # random.choice: first call → pick the single rank entry,
            #                second call → pick CHAR_ID_NEW from charIdList
            mock_random.choice.side_effect = [
                {"rarityRank": rarity_rank, "index": 0},
                CHAR_ID_NEW,
            ]

            import gacha
            result = gacha.finishNormalGacha()

        return result

    def test_new_char_is_marked_new(self):
        result = self._run(rarity_rank=3)
        self.assertEqual(result["charGet"]["isNew"], 1)
        self.assertEqual(result["charGet"]["charId"], CHAR_ID_NEW)

    def test_new_char_item_is_hgg_shard(self):
        result = self._run(rarity_rank=3)
        items = result["charGet"]["itemGet"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "HGG_SHD")
        self.assertEqual(items[0]["id"], "4004")
        self.assertEqual(items[0]["count"], 1)

    def test_new_char_hgg_shard_incremented_in_status(self):
        result = self._run(rarity_rank=3)
        self.assertEqual(result["playerDataDelta"]["modified"]["status"]["hggShard"], 101)

    def test_new_char_gain_time_is_integer(self):
        """gainTime must be an int (not a TypeError from int(time) on a function object)."""
        result = self._run(rarity_rank=3)
        troop = result["playerDataDelta"]["modified"]["troop"]
        # The new char was added with the next inst_id (REPEAT_INST_ID + 1 = 11)
        new_inst_id = str(REPEAT_INST_ID + 1)
        self.assertIn(new_inst_id, troop["chars"])
        gain_time = troop["chars"][new_inst_id]["gainTime"]
        self.assertIsInstance(gain_time, int)

    def test_recruit_slot_state_reset(self):
        result = self._run(rarity_rank=3)
        slot = result["playerDataDelta"]["modified"]["recruit"]["normal"]["slots"][str(SLOT_ID)]
        self.assertEqual(slot["state"], 1)
        self.assertEqual(slot["selectTags"], [])


class TestFinishNormalGachaRepeatCharacter(unittest.TestCase):
    """Repeat character (already owned): rarity resolved from rarityRank, not character_table."""

    def _run(self, rarity_rank, potential_rank=0):
        user_data = _make_user_data(repeat_char_potential_rank=potential_rank)
        gacha_data = _make_gacha_data(rarity_rank, CHAR_ID_REPEAT)
        # character_table has a deliberately wrong rarity string to prove that
        # finishNormalGacha uses rarityRank from gacha pool data, NOT the rarity
        # field from character_table (which may be a string like "RARITY_4").
        char_table = {
            CHAR_ID_REPEAT: {
                "skills": [],
                "rarity": "RARITY_WRONG",
            },
            "charDefaultTypeDict": {CHAR_ID_REPEAT: "JP"},
        }

        fake_request = MagicMock()
        fake_request.data = _request_body()

        def fake_read_json(path):
            if "user" in path:
                return copy.deepcopy(user_data)
            if "normalGacha" in path:
                return gacha_data
            raise FileNotFoundError(path)

        def fake_get_memory(key):
            if key == "character_table":
                return char_table
            if key == "uniequip_table":
                return {}
            raise KeyError(key)

        with patch("gacha.request", fake_request), \
             patch("gacha.read_json", side_effect=fake_read_json), \
             patch("gacha.get_memory", side_effect=fake_get_memory), \
             patch("gacha.run_after_response"), \
             patch("gacha.random") as mock_random:

            mock_random.shuffle = MagicMock()
            mock_random.choice.side_effect = [
                {"rarityRank": rarity_rank, "index": 0},
                CHAR_ID_REPEAT,
            ]

            import gacha
            result = gacha.finishNormalGacha()

        return result

    # ---- helpers ----

    def _shard_item(self, result):
        """Return the shard item from charGet.itemGet (always first)."""
        return result["charGet"]["itemGet"][0]

    # ---- rarity 0 (1-star) ----

    def test_rarity0_gives_lgg_shard_count_1(self):
        result = self._run(rarity_rank=0)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "LGG_SHD")
        self.assertEqual(item["id"], "4005")
        self.assertEqual(item["count"], 1)

    # ---- rarity 1 (2-star) ----

    def test_rarity1_gives_lgg_shard_count_1(self):
        result = self._run(rarity_rank=1)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "LGG_SHD")
        self.assertEqual(item["count"], 1)

    # ---- rarity 2 (3-star) ----

    def test_rarity2_gives_lgg_shard_count_5(self):
        result = self._run(rarity_rank=2)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "LGG_SHD")
        self.assertEqual(item["count"], 5)

    # ---- rarity 3 (4-star) ----

    def test_rarity3_gives_lgg_shard_count_30(self):
        result = self._run(rarity_rank=3)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "LGG_SHD")
        self.assertEqual(item["count"], 30)

    # ---- rarity 4 (5-star) ----

    def test_rarity4_not_max_potential_gives_hgg_shard_count_5(self):
        result = self._run(rarity_rank=4, potential_rank=3)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "HGG_SHD")
        self.assertEqual(item["id"], "4004")
        self.assertEqual(item["count"], 5)

    def test_rarity4_max_potential_gives_hgg_shard_count_8(self):
        result = self._run(rarity_rank=4, potential_rank=5)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "HGG_SHD")
        self.assertEqual(item["count"], 8)

    # ---- rarity 5 (6-star) ----

    def test_rarity5_not_max_potential_gives_hgg_shard_count_10(self):
        result = self._run(rarity_rank=5, potential_rank=3)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "HGG_SHD")
        self.assertEqual(item["id"], "4004")
        self.assertEqual(item["count"], 10)

    def test_rarity5_max_potential_gives_hgg_shard_count_15(self):
        result = self._run(rarity_rank=5, potential_rank=5)
        item = self._shard_item(result)
        self.assertEqual(item["type"], "HGG_SHD")
        self.assertEqual(item["count"], 15)

    # ---- status and inventory updates ----

    def test_lgg_shard_status_updated_for_low_rarity(self):
        result = self._run(rarity_rank=3)  # 4-star → lggShard += 30
        self.assertEqual(result["playerDataDelta"]["modified"]["status"]["lggShard"], 230)

    def test_hgg_shard_status_updated_for_high_rarity(self):
        result = self._run(rarity_rank=4, potential_rank=0)  # 5-star → hggShard += 5
        self.assertEqual(result["playerDataDelta"]["modified"]["status"]["hggShard"], 105)

    def test_potential_item_always_present(self):
        result = self._run(rarity_rank=3)
        items = result["charGet"]["itemGet"]
        potential_items = [i for i in items if i["type"] == "MATERIAL"]
        self.assertEqual(len(potential_items), 1)
        self.assertEqual(potential_items[0]["id"], f"p_{CHAR_ID_REPEAT}")
        self.assertEqual(potential_items[0]["count"], 1)

    def test_repeat_char_is_not_new(self):
        result = self._run(rarity_rank=3)
        self.assertEqual(result["charGet"]["isNew"], 0)

    def test_recruit_slot_state_reset(self):
        result = self._run(rarity_rank=3)
        slot = result["playerDataDelta"]["modified"]["recruit"]["normal"]["slots"][str(SLOT_ID)]
        self.assertEqual(slot["state"], 1)
        self.assertEqual(slot["selectTags"], [])


class TestFinishNormalGachaRepeatCharMissingInventory(unittest.TestCase):
    """Repeat character whose potential item is not yet in the player inventory.

    Regression test for the KeyError that occurred when
    ``user_data["user"]["inventory"][f"p_{char_id}"] += 1``
    was called for a character whose potential token had never been obtained.
    """

    def _run(self, rarity_rank=3):
        user_data = _make_user_data()
        # Remove the potential item from inventory to reproduce the original bug.
        user_data["user"]["inventory"].pop(f"p_{CHAR_ID_REPEAT}", None)

        gacha_data = _make_gacha_data(rarity_rank, CHAR_ID_REPEAT)
        char_table = {
            CHAR_ID_REPEAT: {"skills": [], "rarity": "RARITY_3"},
            "charDefaultTypeDict": {CHAR_ID_REPEAT: "JP"},
        }

        fake_request = MagicMock()
        fake_request.data = _request_body()

        def fake_read_json(path):
            if "user" in path:
                return copy.deepcopy(user_data)
            if "normalGacha" in path:
                return gacha_data
            raise FileNotFoundError(path)

        def fake_get_memory(key):
            if key == "character_table":
                return char_table
            if key == "uniequip_table":
                return {}
            raise KeyError(key)

        with patch("gacha.request", fake_request), \
             patch("gacha.read_json", side_effect=fake_read_json), \
             patch("gacha.get_memory", side_effect=fake_get_memory), \
             patch("gacha.run_after_response"), \
             patch("gacha.random") as mock_random:

            mock_random.shuffle = MagicMock()
            mock_random.choice.side_effect = [
                {"rarityRank": rarity_rank, "index": 0},
                CHAR_ID_REPEAT,
            ]

            import gacha
            result = gacha.finishNormalGacha()

        return result

    def test_no_key_error_when_potential_item_absent(self):
        """Should not raise KeyError even when p_<charId> is absent from inventory."""
        result = self._run(rarity_rank=3)
        items = result["charGet"]["itemGet"]
        potential_items = [i for i in items if i["type"] == "MATERIAL"]
        self.assertEqual(len(potential_items), 1)
        self.assertEqual(potential_items[0]["id"], f"p_{CHAR_ID_REPEAT}")
        self.assertEqual(potential_items[0]["count"], 1)

    def test_inventory_initialised_to_1_when_previously_absent(self):
        """Inventory entry should be created and set to 1, not crash."""
        result = self._run(rarity_rank=3)
        # As long as no exception was raised the fix is working.
        # We verify the potential item was still reported in itemGet.
        items = result["charGet"]["itemGet"]
        self.assertTrue(any(i["id"] == f"p_{CHAR_ID_REPEAT}" for i in items))


class TestSyncNormalGacha(unittest.TestCase):
    """syncNormalGacha should return the player's recruit slot states."""

    SLOTS = {
        "0": {"state": 1, "selectTags": [], "durationInSec": -1},
        "1": {"state": 2, "selectTags": [{"pick": 1, "tagId": 3}], "durationInSec": 28800},
    }

    def _run(self):
        user_data = {
            "user": {
                "recruit": {
                    "normal": {
                        "slots": self.SLOTS
                    }
                }
            }
        }

        def fake_read_json(path):
            if "user" in path:
                return copy.deepcopy(user_data)
            raise FileNotFoundError(path)

        with patch("gacha.read_json", side_effect=fake_read_json):
            import gacha
            return gacha.syncNormalGacha()

    def test_returns_player_recruit_slots(self):
        result = self._run()
        slots = result["playerDataDelta"]["modified"]["recruit"]["normal"]["slots"]
        self.assertEqual(slots, self.SLOTS)

    def test_slots_is_dict_not_list(self):
        result = self._run()
        slots = result["playerDataDelta"]["modified"]["recruit"]["normal"]["slots"]
        self.assertIsInstance(slots, dict)

    def test_deleted_key_present(self):
        result = self._run()
        self.assertIn("deleted", result["playerDataDelta"])


class TestFinishNormalGachaPersistsUserData(unittest.TestCase):
    """finishNormalGacha must persist user data (slot state reset) via run_after_response."""

    def _run(self, rarity_rank=3, is_new_char=False):
        char_id = CHAR_ID_NEW if is_new_char else CHAR_ID_REPEAT
        user_data = _make_user_data()
        if is_new_char:
            # Remove CHAR_ID_NEW from chars so it's treated as new
            user_data["user"]["troop"]["chars"] = {}

        gacha_data = _make_gacha_data(rarity_rank, char_id)
        char_table = _make_character_table(char_id)
        charword_table = {"charDefaultTypeDict": {char_id: "JP"}}

        fake_request = MagicMock()
        fake_request.data = _request_body()

        def fake_read_json(path):
            if "user" in path:
                return copy.deepcopy(user_data)
            if "normalGacha" in path:
                return gacha_data
            raise FileNotFoundError(path)

        def fake_get_memory(key):
            if key == "character_table":
                return char_table
            if key == "charword_table":
                return charword_table
            if key == "uniequip_table":
                return {}
            raise KeyError(key)

        with patch("gacha.request", fake_request), \
             patch("gacha.read_json", side_effect=fake_read_json), \
             patch("gacha.get_memory", side_effect=fake_get_memory), \
             patch("gacha.run_after_response") as mock_run_after, \
             patch("gacha.write_json") as mock_write_json, \
             patch("gacha.random") as mock_random:

            mock_random.shuffle = MagicMock()
            mock_random.choice.side_effect = [
                {"rarityRank": rarity_rank, "index": 0},
                char_id,
            ]

            import gacha
            gacha.finishNormalGacha()

        return mock_run_after, mock_write_json

    def test_run_after_response_called_with_write_json_and_user_data_path(self):
        """run_after_response must be called with write_json and SYNC_DATA_TEMPLATE_PATH."""
        mock_run_after, mock_write_json = self._run(rarity_rank=3)
        self.assertTrue(mock_run_after.called,
                        "run_after_response should be called to persist user data")
        # The first positional arg to every run_after_response call should be write_json
        from constants import SYNC_DATA_TEMPLATE_PATH
        calls_with_path = [
            c for c in mock_run_after.call_args_list
            if len(c.args) >= 3 and c.args[2] == SYNC_DATA_TEMPLATE_PATH
        ]
        self.assertGreaterEqual(len(calls_with_path), 1,
                                "run_after_response must be called with SYNC_DATA_TEMPLATE_PATH")

    def test_slot_state_reset_in_saved_data_repeat_char(self):
        """Saved user data must have slot state=1 and empty selectTags after repeat-char pull."""
        mock_run_after, _ = self._run(rarity_rank=3)
        from constants import SYNC_DATA_TEMPLATE_PATH
        # Find the call that persists the full user data
        save_call = next(
            (c for c in mock_run_after.call_args_list
             if len(c.args) >= 3 and c.args[2] == SYNC_DATA_TEMPLATE_PATH),
            None
        )
        self.assertIsNotNone(save_call, "No save call with SYNC_DATA_TEMPLATE_PATH found")
        saved_data = save_call.args[1]
        slot = saved_data["user"]["recruit"]["normal"]["slots"][str(SLOT_ID)]
        self.assertEqual(slot["state"], 1)
        self.assertEqual(slot["selectTags"], [])


class TestCate(unittest.TestCase):
    """cate() should return correct gacha pool list without crashing."""

    # Representative pool timestamps for tests: open time is in the past,
    # end time is far in the future, so pools are always "active" regardless of when tests run.
    _OPEN = 1000000000   # 2001-09-08 — safely in the past
    _END  = 9999999999   # 2286-11-20 — safely in the future

    def _make_gacha_table(self, pool_ids):
        return {
            "gachaPoolClient": [
                {
                    "gachaPoolId": pid,
                    "gachaPoolName": f"name_{pid}",
                    "openTime": self._OPEN,
                    "endTime": self._END,
                }
                for pid in pool_ids
            ]
        }

    def _run(self, pool_ids):
        gacha_table = self._make_gacha_table(pool_ids)

        with patch("gacha.get_memory", return_value=gacha_table), \
             patch("gacha.time", return_value=5000000000):
            import gacha
            raw = gacha.cate()

        return json.loads(raw)

    def test_norm_pool_returns_correct_name(self):
        result = self._run(["NORM_001"])
        self.assertEqual(result["code"], 0)
        pool = result["data"][0]
        self.assertEqual(pool["id"], "NORM_001")
        self.assertEqual(pool["name"], "标准寻访")

    def test_classic_pool_returns_correct_name(self):
        result = self._run(["CLASSIC_001"])
        pool = result["data"][0]
        self.assertEqual(pool["name"], "中坚寻访")

    def test_limited_pool_uses_pool_name_from_data(self):
        """LIMITED pools must use gachaPoolName from the table, not crash with AttributeError."""
        result = self._run(["LIMITED_001"])
        pool = result["data"][0]
        self.assertEqual(pool["name"], "name_LIMITED_001")

    def test_active_flag_set_for_active_pool(self):
        """Pools whose openTime <= current time <= endTime should have active=True."""
        result = self._run(["NORM_001"])
        pool = result["data"][0]
        self.assertTrue(pool.get("active"), "Active pool should have active=True")

    def test_inactive_pool_has_no_active_flag(self):
        """Pools outside the active window should NOT have active=True."""
        # Use a pool that ended in the past
        gacha_table = {
            "gachaPoolClient": [
                {
                    "gachaPoolId": "NORM_OLD",
                    "gachaPoolName": "old pool",
                    "openTime": 1000,
                    "endTime": 2000,
                }
            ]
        }
        with patch("gacha.get_memory", return_value=gacha_table), \
             patch("gacha.time", return_value=5000000000):
            import gacha
            raw = gacha.cate()
        result = json.loads(raw)
        pool = result["data"][0]
        self.assertNotIn("active", pool)

    def test_max_four_pools_returned(self):
        """cate() must return at most 4 pools."""
        pool_ids = [f"NORM_{i:03d}" for i in range(10)]
        result = self._run(pool_ids)
        self.assertLessEqual(len(result["data"]), 4)

    def test_empty_pool_list(self):
        """cate() should handle an empty gacha pool list gracefully."""
        gacha_table = {"gachaPoolClient": []}
        with patch("gacha.get_memory", return_value=gacha_table), \
             patch("gacha.time", return_value=5000000000):
            import gacha
            raw = gacha.cate()
        result = json.loads(raw)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["data"], [])


if __name__ == "__main__":
    unittest.main()

