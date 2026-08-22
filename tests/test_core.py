import json
from pathlib import Path
import tempfile
import unittest

import pytest

from coc_ai_controller.parsers.battle import load_battle_script
from coc_ai_controller.parsers.village import parse_village
from coc_ai_controller.adapters.database import Database


class CoreTests(unittest.TestCase):
    def test_tolerant_village_and_join(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "village.json"
            source.write_text(
                json.dumps({
                    "tag": "#TEST",
                    "buildings2": [{"data": 1000055, "lvl": 8, "cnt": 2, "future": 1}],
                    "future_section": {"x": 1},
                }),
                encoding="utf-8",
            )
            snapshot = parse_village(source)
            assert snapshot.tag == "#TEST"
            assert snapshot.entities[0].count == 2
            db = Database(path=root / "test.sqlite3")
            db.save_account(snapshot)
            rows = db.account_rows("#TEST")
            assert rows[0].name == "Crusher"

    def test_village_entry_keeps_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "village.json"
            source.write_text(
                json.dumps({
                    "player_tag": "#ALIAS",
                    "units": [
                        {"id": 4000041, "level": "3", "count": None, "unreleased": True},
                        {"name": "no data id"},
                    ],
                }),
                encoding="utf-8",
            )
            snapshot = parse_village(source)
            assert snapshot.tag == "#ALIAS"
            # The row without a data_id is dropped; the other keeps its unknown field.
            assert len(snapshot.entities) == 1
            entity = snapshot.entities[0]
            assert (entity.data_id, entity.level, entity.count) == (4000041, 3, 1)
            assert (entity.model_extra or {})["unreleased"] is True

    def test_saved_snapshot_can_be_reimported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "village.json"
            source.write_text(
                json.dumps({"tag": "#TEST", "buildings2": [{"data": 1000055, "lvl": 8}]}),
                encoding="utf-8",
            )
            saved = Path(td) / "saved.json"
            saved.write_text(parse_village(source).model_dump_json(), encoding="utf-8")
            assert parse_village(saved).entities[0].data_id == 1000055

    def test_battle_script_requires_army(self) -> None:
        script = load_battle_script(
            Path(__file__).parents[1] / "battle_scripts" / "BH10_BABY_DRAGON_01.json"
        )
        assert script.army_requirements.troops[0].data_id == 4000041
        assert script.battle_controller.kind == "RESERVED_RL"
        assert [name for name, _ in script.army_requirements.categories()] == [
            "troops",
            "heroes",
            "spells",
            "siege",
            "reinforcements",
        ]

    def test_battle_script_without_requirements_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broken = Path(td) / "broken.json"
            broken.write_text(json.dumps({"script_id": "X"}), encoding="utf-8")
            with pytest.raises(ValueError, match="army_requirements"):
                load_battle_script(broken)


if __name__ == "__main__":
    unittest.main()
