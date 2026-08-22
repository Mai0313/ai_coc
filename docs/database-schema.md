# Database Schema — VER 0.1.0

SQLite runtime database is stored under `~/.coc_ai\controller.sqlite3` and is not committed.

- `metadata`: schema/master versions.
- `id_registry`: shared data ID identity, world/category and provenance.
- `entity_levels`: sourced per-level cost/time/requirements (schema ready; seed release intentionally leaves unverified values empty).
- `account_snapshots`: player-tag-specific raw tolerant Village JSON snapshots.
- `account_entities`: normalized section/data ID/level/count rows.
- `unknown_entities`: unknown IDs queued as UNKNOWN for research.
- `knowledge`: agent/user statements with UNKNOWN/CANDIDATE/VERIFIED/USER_CONFIRMED status.

Account state and shared master data are deliberately separate.
