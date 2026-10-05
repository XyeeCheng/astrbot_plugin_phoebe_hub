"""Make an online SQLite backup and rehearse migration on the copy only."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub.config import Settings
from hub.store import Store


def rehearse(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file() or destination.exists() or source == destination:
        raise ValueError("Source must exist; destination must be a new file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)
    db = sqlite3.connect(destination)
    before = dict(db.execute("SELECT scope,score FROM relations"))
    db.close()
    store = Store(destination, Settings())
    try:
        after = dict(store.db.execute("SELECT scope,score FROM relations"))
        result = {
            "mode": "dry_run_copy",
            "relations": len(after),
            "scores_preserved": before == after,
            "special_scores": [v for v in after.values() if v > 100],
            "historical_anomalies": store.db.execute(
                "SELECT COUNT(*) FROM ledger_notes"
            ).fetchone()[0],
            "schema": store.db.execute("PRAGMA user_version").fetchone()[0],
            "integrity": store.db.execute("PRAGMA integrity_check").fetchone()[0],
            "identity_binding": "lazy: only transport-verified old keys merge on that member's next message",
        }
        if not result["scores_preserved"] or result["integrity"] != "ok":
            raise RuntimeError("Migration verification failed")
        return result
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(
        json.dumps(
            rehearse(args.source, args.destination), ensure_ascii=False, indent=2
        )
    )
