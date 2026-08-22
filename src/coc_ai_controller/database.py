from __future__ import annotations

import json
from typing import TYPE_CHECKING
import sqlite3
from datetime import UTC, datetime
import threading
from contextlib import closing

from .models import AccountRow, TaskRecord, KnowledgeItem, RegistryEntry, AccountSnapshot
from .constants import DB_PATH, SCHEMA_VERSION, MASTER_DB_VERSION

if TYPE_CHECKING:
    from pathlib import Path


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _initialize(self) -> None:
        with self._lock, closing(self.connect()) as con, con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS id_registry(
                data_id INTEGER PRIMARY KEY, name TEXT NOT NULL, world TEXT NOT NULL,
                category TEXT NOT NULL, source_url TEXT, verification_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entity_levels(
                data_id INTEGER NOT NULL, level INTEGER NOT NULL, next_level INTEGER,
                upgrade_cost INTEGER, resource_type TEXT, upgrade_seconds INTEGER,
                requirement TEXT, source_url TEXT, game_version TEXT,
                retrieved_at TEXT, verification_status TEXT NOT NULL,
                PRIMARY KEY(data_id, level), FOREIGN KEY(data_id) REFERENCES id_registry(data_id)
            );
            CREATE TABLE IF NOT EXISTS account_snapshots(
                tag TEXT PRIMARY KEY, imported_at TEXT NOT NULL, raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_entities(
                tag TEXT NOT NULL, section TEXT NOT NULL, data_id INTEGER NOT NULL,
                level INTEGER, count INTEGER, raw_json TEXT NOT NULL,
                PRIMARY KEY(tag, section, data_id, level)
            );
            CREATE TABLE IF NOT EXISTS unknown_entities(
                data_id INTEGER NOT NULL, section TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                sample_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'UNKNOWN',
                PRIMARY KEY(data_id, section)
            );
            CREATE TABLE IF NOT EXISTS knowledge(
                id INTEGER PRIMARY KEY AUTOINCREMENT, emulator_id TEXT, frame_id TEXT,
                statement TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, instruction TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', progress TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """)
            con.executemany(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                [("schema_version", SCHEMA_VERSION), ("master_db_version", MASTER_DB_VERSION)],
            )
            seeds = [
                (1000055, "Crusher", "builder_base", "building"),
                (1000041, "Double Cannon", "builder_base", "building"),
                (1000056, "Roaster", "builder_base", "building"),
                (4000041, "Baby Dragon", "builder_base", "troop"),
                (28000003, "Battle Machine", "builder_base", "hero"),
                (28000005, "Battle Copter", "builder_base", "hero"),
            ]
            con.executemany(
                """
                INSERT OR IGNORE INTO id_registry
                (data_id,name,world,category,source_url,verification_status)
                VALUES(?,?,?,?,?,?)
            """,
                [
                    (
                        i,
                        n,
                        w,
                        c,
                        "https://gist.github.com/rahulkhatri137/a8449943df45100c5f1e1359cd9ec67a",
                        "SEED",
                    )
                    for i, n, w, c in seeds
                ],
            )

    def lookup(self, data_id: int) -> RegistryEntry | None:
        with closing(self.connect()) as con:
            row = con.execute("SELECT * FROM id_registry WHERE data_id=?", (data_id,)).fetchone()
        return RegistryEntry.model_validate(dict(row)) if row else None

    def save_account(self, snapshot: AccountSnapshot) -> None:
        imported = datetime.now(UTC).isoformat()
        with self._lock, closing(self.connect()) as con, con:
            con.execute(
                "INSERT OR REPLACE INTO account_snapshots VALUES(?,?,?)",
                (snapshot.tag, imported, json.dumps(snapshot.raw, ensure_ascii=False)),
            )
            con.execute("DELETE FROM account_entities WHERE tag=?", (snapshot.tag,))
            for entity in snapshot.entities:
                con.execute(
                    """INSERT OR REPLACE INTO account_entities
                    (tag,section,data_id,level,count,raw_json) VALUES(?,?,?,?,?,?)""",
                    (
                        snapshot.tag,
                        entity.section,
                        entity.data_id,
                        entity.level,
                        entity.count,
                        entity.model_dump_json(),
                    ),
                )
                if self.lookup(entity.data_id) is None:
                    con.execute(
                        """INSERT OR IGNORE INTO unknown_entities
                        (data_id,section,first_seen_at,sample_json,status) VALUES(?,?,?,?,?)""",
                        (
                            entity.data_id,
                            entity.section,
                            imported,
                            entity.model_dump_json(),
                            "UNKNOWN",
                        ),
                    )

    def account_rows(self, tag: str) -> list[AccountRow]:
        with closing(self.connect()) as con:
            rows = con.execute(
                """SELECT ae.*, ir.name, ir.world, ir.category,
                el.next_level, el.upgrade_cost, el.resource_type, el.upgrade_seconds, el.requirement
                FROM account_entities ae LEFT JOIN id_registry ir ON ir.data_id=ae.data_id
                LEFT JOIN entity_levels el ON el.data_id=ae.data_id AND el.level=ae.level
                WHERE ae.tag=? ORDER BY ae.section, COALESCE(ir.name, ae.data_id)""",
                (tag,),
            ).fetchall()
        return [AccountRow.model_validate(dict(row)) for row in rows]

    def add_knowledge(self, emulator_id: str, frame_id: str, statement: str, status: str) -> None:
        with closing(self.connect()) as con, con:
            con.execute(
                "INSERT INTO knowledge(emulator_id,frame_id,statement,status,created_at) VALUES(?,?,?,?,?)",
                (emulator_id, frame_id, statement, status, datetime.now(UTC).isoformat()),
            )

    def recent_knowledge(self, limit: int = 50) -> list[KnowledgeItem]:
        with closing(self.connect()) as con:
            rows = con.execute(
                "SELECT * FROM knowledge ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [KnowledgeItem.model_validate(dict(row)) for row in reversed(rows)]

    def add_task(self, instruction: str) -> int:
        now = datetime.now(UTC).isoformat()
        with closing(self.connect()) as con, con:
            cursor = con.execute(
                "INSERT INTO tasks(instruction,status,progress,created_at,updated_at) VALUES(?,?,?,?,?)",
                (instruction, "PENDING", "等待執行", now, now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Task insert did not return an identifier")
            return int(cursor.lastrowid)

    def pending_tasks(self) -> list[TaskRecord]:
        with closing(self.connect()) as con:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status IN ('PENDING','RUNNING') ORDER BY id"
            ).fetchall()
        return [TaskRecord.model_validate(dict(row)) for row in rows]

    def update_task(self, task_id: int, status: str, progress: str) -> None:
        now = datetime.now(UTC).isoformat()
        with closing(self.connect()) as con, con:
            con.execute(
                "UPDATE tasks SET status=?,progress=?,updated_at=? WHERE id=?",
                (status, progress, now, task_id),
            )
