import json
import tempfile
import unittest
from pathlib import Path

from coc_ai_controller.battle import load_battle_script
from coc_ai_controller.database import Database
from coc_ai_controller.village import parse_village


class CoreTests(unittest.TestCase):
    def test_tolerant_village_and_join(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "village.json"
            source.write_text(json.dumps({"tag": "#TEST", "buildings2": [{"data": 1000055, "lvl": 8, "cnt": 2, "future": 1}], "future_section": {"x": 1}}), encoding="utf-8")
            snapshot = parse_village(source)
            self.assertEqual(snapshot.tag, "#TEST"); self.assertEqual(snapshot.entities[0]["count"], 2)
            db = Database(root / "test.sqlite3"); db.save_account(snapshot.tag, snapshot.raw, snapshot.entities)
            rows = db.account_rows("#TEST"); self.assertEqual(rows[0]["name"], "Crusher")

    def test_battle_script_requires_army(self):
        script = load_battle_script(Path(__file__).parents[1] / "battle_scripts" / "BH10_BABY_DRAGON_01.json")
        self.assertEqual(script.requirements["troops"][0]["data_id"], 4000041)


if __name__ == "__main__": unittest.main()
