import json
from pathlib import Path
import tempfile
import unittest

from coc_ai_controller.battle import load_battle_script
from coc_ai_controller.village import parse_village
from coc_ai_controller.database import Database


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
            db = Database(root / "test.sqlite3")
            db.save_account(snapshot)
            rows = db.account_rows("#TEST")
            assert rows[0].name == "Crusher"

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
        assert script.requirements["troops"][0].data_id == 4000041


if __name__ == "__main__":
    unittest.main()
