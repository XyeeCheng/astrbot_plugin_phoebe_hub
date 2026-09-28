import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .persona import classify, normalize


def scope_key(platform, bot, persona, umo, user):
    return hashlib.sha256(json.dumps([platform, bot, persona, umo, user], ensure_ascii=False).encode()).hexdigest()


def day(now):
    return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()


class Store:
    def __init__(self, path, settings, clock=time.time):
        self.path, self.settings, self.clock = Path(path), settings, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS relations(
          scope TEXT PRIMARY KEY, score INTEGER NOT NULL, date TEXT NOT NULL,
          gained INTEGER NOT NULL DEFAULT 0, lost INTEGER NOT NULL DEFAULT 0,
          last_gain REAL NOT NULL DEFAULT 0, angry_until REAL NOT NULL DEFAULT 0,
          remaining INTEGER NOT NULL DEFAULT 0, last_trigger REAL NOT NULL DEFAULT 0,
          mother_window REAL NOT NULL DEFAULT 0, mother_count INTEGER NOT NULL DEFAULT 0,
          generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS turns(
          scope TEXT NOT NULL, eid TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL,
          body TEXT NOT NULL, digest TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'claimed', generation INTEGER NOT NULL,
          PRIMARY KEY(scope,eid));
        CREATE TABLE IF NOT EXISTS memories(
          scope TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,text));
        CREATE TABLE IF NOT EXISTS ledger(
          scope TEXT NOT NULL, eid TEXT NOT NULL, at REAL NOT NULL, delta INTEGER NOT NULL,
          reason TEXT NOT NULL, UNIQUE(scope,eid,reason));
        CREATE TABLE IF NOT EXISTS confirmations(scope TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tombstones(scope TEXT PRIMARY KEY, deleted REAL NOT NULL);
        PRAGMA user_version=1;
        """)
        # Crashed/uncertain in-flight turns never get auto-replayed after reload.
        with self.db:
            self.db.execute("UPDATE turns SET status='unknown' WHERE status IN ('claimed','prepared')")
        self.prune()

    def close(self):
        self.db.close()

    def _relation(self, scope, now):
        self.db.execute("INSERT OR IGNORE INTO relations(scope,score,date) VALUES(?,?,?)",
                        (scope, self.settings.initial_affinity, day(now)))
        row = dict(self.db.execute("SELECT * FROM relations WHERE scope=?", (scope,)).fetchone())
        if row["date"] != day(now):
            row.update(date=day(now), gained=0, lost=0)
        if row["angry_until"] <= now:
            row.update(angry_until=0, remaining=0)
        return row

    def _save(self, row):
        names = [k for k in row if k != "scope"]
        self.db.execute("UPDATE relations SET " + ",".join(k + "=?" for k in names) + " WHERE scope=?",
                        [row[k] for k in names] + [row["scope"]])

    def _delta(self, row, eid, amount, reason, now):
        amount = max(0, min(100, row["score"] + amount)) - row["score"]
        if amount:
            self.db.execute("INSERT INTO ledger VALUES(?,?,?,?,?)", (row["scope"], eid, now, amount, reason))
            row["score"] += amount

    def begin(self, scope, eid, text):
        now, s = self.clock(), self.settings
        text = normalize(text)[:6000]
        kind = classify(text, s)
        digest = hashlib.sha256(text.casefold().encode()).hexdigest()
        with self.db:
            if self.db.execute("SELECT 1 FROM turns WHERE scope=? AND eid=?", (scope, eid)).fetchone():
                return None
            row = self._relation(scope, now)
            self.db.execute("INSERT INTO turns(scope,eid,created,kind,body,digest,generation) VALUES(?,?,?,?,?,?,?)",
                            (scope, eid, now, kind, text, digest, row["generation"]))
            trigger = False
            if kind in ("calm", "serious"):
                row.update(angry_until=0, remaining=0)
            elif kind == "mother":
                if now - row["mother_window"] >= 600:
                    row.update(mother_window=now, mother_count=0)
                row["mother_count"] += 1
                if row["mother_count"] >= 3 and row["lost"] < 3:
                    self._delta(row, eid, -1, "repeated_mother", now)
                    row["lost"] += 1
                if (not row["last_trigger"] or now - row["last_trigger"] >= s.trigger_cooldown) and not row["remaining"]:
                    row.update(angry_until=now + s.anger_seconds, remaining=s.anger_replies, last_trigger=now)
                    trigger = True
            self._save(row)
            return {**row, "kind": kind, "trigger": trigger,
                    "mood": "serious" if kind in ("calm", "serious") else
                    ("angry" if row["remaining"] else "normal")}

    def status(self, scope):
        with self.db:
            row = self._relation(scope, self.clock())
            self._save(row)
            return {**row, "mood": "angry" if row["remaining"] else "normal"}

    def prepare(self, scope, eid, reply):
        with self.db:
            self.db.execute("UPDATE turns SET reply=?,status='prepared' WHERE scope=? AND eid=? AND status='claimed'",
                            (reply, scope, eid))

    def finish(self, scope, eid, status="sent"):
        if status not in ("sent", "unknown", "failed"):
            raise ValueError("invalid delivery status")
        now, s = self.clock(), self.settings
        with self.db:
            turn = self.db.execute("SELECT * FROM turns WHERE scope=? AND eid=?", (scope, eid)).fetchone()
            if not turn or turn["status"] not in ("claimed", "prepared"):
                return
            self.db.execute("UPDATE turns SET status=? WHERE scope=? AND eid=?", (status, scope, eid))
            row = self._relation(scope, now)
            if status == "failed" or row["generation"] != turn["generation"]:
                return
            if row["remaining"]:
                row["remaining"] -= 1
                if not row["remaining"]:
                    row["angry_until"] = 0
            duplicate_text = self.db.execute(
                "SELECT 1 FROM turns WHERE scope=? AND digest=? AND eid!=? AND created>? LIMIT 1",
                (scope, turn["digest"], eid, now - 86400)).fetchone()
            eligible = (turn["kind"] == "normal" and len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", turn["body"])) >= 4 and not duplicate_text
                        and turn["body"].strip() not in ("你好呀", "晚上好呀", "早上好呀")
                        and row["gained"] < s.daily_gain
                        and (not row["last_gain"] or now - row["last_gain"] >= s.gain_cooldown))
            if eligible:
                self._delta(row, eid, 1, "conversation", now)
                row["gained"] += 1
                row["last_gain"] = now
            self._save(row)

    def history(self, scope):
        rows = self.db.execute("SELECT body,reply FROM turns WHERE scope=? AND status IN ('sent','unknown') "
                               "AND reply!='' ORDER BY created DESC,rowid DESC LIMIT ?",
                               (scope, self.settings.history_turns)).fetchall()
        return [message for row in reversed(rows) for message in (
            {"role": "user", "content": row["body"]}, {"role": "assistant", "content": row["reply"]})]

    def remember(self, scope, text):
        text = normalize(text)
        if not 2 <= len(text) <= 120 or any(w in text.casefold() for w in ("api_key", "apikey", "密码", "token", "密钥", "sk-")):
            return False
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?)", (scope, text, self.clock()))
            self.db.execute("DELETE FROM memories WHERE scope=? AND rowid NOT IN "
                            "(SELECT rowid FROM memories WHERE scope=? ORDER BY created DESC LIMIT 20)", (scope, scope))
        return True

    def memories(self, scope, limit=6):
        return [r[0] for r in self.db.execute("SELECT text FROM memories WHERE scope=? ORDER BY created DESC LIMIT ?", (scope, limit))]

    def forget_item(self, scope, text):
        with self.db:
            return self.db.execute("DELETE FROM memories WHERE scope=? AND text=?", (scope, normalize(text))).rowcount

    def ask_forget(self, scope):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO confirmations VALUES(?,?)", (scope, self.clock() + 60))

    def forget(self, scope):
        now = self.clock()
        with self.db:
            confirmation = self.db.execute("SELECT expires FROM confirmations WHERE scope=?", (scope,)).fetchone()
            if not confirmation or confirmation[0] < now:
                return False
            row = self._relation(scope, now)
            generation = row["generation"] + 1
            for table in ("turns", "memories", "ledger", "confirmations", "relations"):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
            self.db.execute("INSERT INTO relations(scope,score,date,generation) VALUES(?,?,?,?)",
                            (scope, self.settings.initial_affinity, day(now), generation))
            self.db.execute("INSERT OR REPLACE INTO tombstones VALUES(?,?)", (scope, now))
        return True

    def calm(self, scope):
        with self.db:
            row = self._relation(scope, self.clock())
            row.update(angry_until=0, remaining=0)
            self._save(row)

    def prune(self):
        now = self.clock()
        with self.db:
            self.db.execute("DELETE FROM turns WHERE created<?", (now - self.settings.retention_days * 86400,))
            self.db.execute("DELETE FROM ledger WHERE at<?", (now - 30 * 86400,))
            self.db.execute("DELETE FROM confirmations WHERE expires<?", (now,))

    def backup(self):
        target = self.path.with_name("hub-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3")
        with closing(sqlite3.connect(target)) as other:
            self.db.backup(other)
        return target
