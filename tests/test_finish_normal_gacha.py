"""
Tests for finishNormalGacha in server/gacha.py.

Covers:
- New character branch: gainTime uses int(time()), hggShard incremented, charGet.isNew==1
- Repeat character branch: correct item_name/type/id/count for every rarityRank (0-5)
  and both potential_rank states (< 5 and == 5)
"""

import json
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
                import copy
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
                import copy
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


if __name__ == "__main__":
    unittest.main()
