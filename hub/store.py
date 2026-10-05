import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .persona import classify, normalize


def scope_key(*parts):
    return hashlib.sha256(
        json.dumps(list(parts), ensure_ascii=False).encode()
    ).hexdigest()


def day(now):
    return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()


class Store:
    def __init__(self, path, settings, clock=time.time):
        self.path, self.settings, self.clock = Path(path), settings, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        previous_version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if previous_version == 1:
            self.backup()
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
        CREATE TABLE IF NOT EXISTS identities(scope TEXT PRIMARY KEY, scene TEXT NOT NULL, user TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aliases(legacy TEXT PRIMARY KEY, scope TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS public_messages(
          scene TEXT NOT NULL, eid TEXT NOT NULL, role TEXT NOT NULL, scope TEXT NOT NULL,
          speaker TEXT NOT NULL, user TEXT NOT NULL, created REAL NOT NULL, body TEXT NOT NULL,
          quote TEXT NOT NULL DEFAULT '', PRIMARY KEY(scene,eid,role));
        CREATE TABLE IF NOT EXISTS topics(scope TEXT PRIMARY KEY, subject TEXT NOT NULL,
          query TEXT NOT NULL, results TEXT NOT NULL, at REAL NOT NULL, quote TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS memory_meta(scope TEXT NOT NULL, text TEXT NOT NULL, category TEXT NOT NULL,
          eid TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(scope,text));
        CREATE TABLE IF NOT EXISTS memory_deleted(scope TEXT NOT NULL, text TEXT NOT NULL, at REAL NOT NULL,
          PRIMARY KEY(scope,text));
        CREATE TABLE IF NOT EXISTS memory_options(scope TEXT PRIMARY KEY, automatic INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS experiences(scope TEXT NOT NULL, eid TEXT NOT NULL, text TEXT NOT NULL,
          created REAL NOT NULL, PRIMARY KEY(scope,eid));
        CREATE TABLE IF NOT EXISTS ledger_notes(scope TEXT NOT NULL, eid TEXT NOT NULL, note TEXT NOT NULL,
          PRIMARY KEY(scope,eid));
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(relations)")}
        if "fixed_score" not in columns:
            self.db.execute("ALTER TABLE relations ADD COLUMN fixed_score INTEGER")
        with self.db:
            # Preserve legitimate imported special scores; never clamp them or
            # turn the historical faulty -20 entries into +140 compensation.
            self.db.execute(
                "UPDATE relations SET fixed_score=score WHERE score>100 AND fixed_score IS NULL"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO ledger_notes SELECT scope,eid,? FROM ledger "
                "WHERE delta<0 AND reason='conversation'",
                ("旧版上限公式异常，保留原记录，不用于补分",),
            )
            self.db.execute("PRAGMA user_version=2")
        # Crashed/uncertain in-flight turns never get auto-replayed after reload.
        with self.db:
            self.db.execute(
                "UPDATE turns SET status='unknown' WHERE status IN ('claimed','prepared')"
            )
        self.prune()

    def close(self):
        self.db.close()

    def _relation(self, scope, now):
        self.db.execute(
            "INSERT OR IGNORE INTO relations(scope,score,date) VALUES(?,?,?)",
            (scope, self.settings.initial_affinity, day(now)),
        )
        row = dict(
            self.db.execute(
                "SELECT * FROM relations WHERE scope=?", (scope,)
            ).fetchone()
        )
        if row["date"] != day(now):
            row.update(date=day(now), gained=0, lost=0)
        if row["angry_until"] <= now:
            row.update(angry_until=0, remaining=0)
        return row

    def _save(self, row):
        names = [k for k in row if k != "scope"]
        self.db.execute(
            "UPDATE relations SET "
            + ",".join(k + "=?" for k in names)
            + " WHERE scope=?",
            [row[k] for k in names] + [row["scope"]],
        )

    def _delta(self, row, eid, amount, reason, now):
        if row.get("fixed_score") is not None or row["score"] > 100:
            row["score"] = row.get("fixed_score") or row["score"]
            return 0
        amount = max(0, min(100, row["score"] + amount)) - row["score"]
        if amount:
            self.db.execute(
                "INSERT INTO ledger VALUES(?,?,?,?,?)",
                (row["scope"], eid, now, amount, reason),
            )
            row["score"] += amount
        return amount

    def begin(self, scope, eid, text):
        now, s = self.clock(), self.settings
        text = normalize(text)[:6000]
        kind = classify(text, s)
        digest = hashlib.sha256(text.casefold().encode()).hexdigest()
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM turns WHERE scope=? AND eid=?", (scope, eid)
            ).fetchone():
                return None
            row = self._relation(scope, now)
            self.db.execute(
                "INSERT INTO turns(scope,eid,created,kind,body,digest,generation) VALUES(?,?,?,?,?,?,?)",
                (scope, eid, now, kind, text, digest, row["generation"]),
            )
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
                if (
                    not row["last_trigger"]
                    or now - row["last_trigger"] >= s.trigger_cooldown
                ) and not row["remaining"]:
                    row.update(
                        angry_until=now + s.anger_seconds,
                        remaining=s.anger_replies,
                        last_trigger=now,
                    )
                    trigger = True
            self._save(row)
            mood = "angry" if row["remaining"] and row["score"] < 120 else "normal"
            if mood == "normal" and kind == "normal":
                if re.search(
                    r"你(?:真|好|很|太).{0,3}(?:厉害|可爱|好看)|喜欢你|夸夸你", text
                ):
                    mood = "shy"
                elif re.search(r"谢谢|多谢|帮大忙|一起玩", text):
                    mood = "happy"
            return {
                **row,
                "kind": kind,
                "trigger": trigger,
                "mood": "serious" if kind in ("calm", "serious") else mood,
            }

    def status(self, scope):
        with self.db:
            row = self._relation(scope, self.clock())
            self._save(row)
            return {**row, "mood": "angry" if row["remaining"] else "normal"}

    def prepare(self, scope, eid, reply):
        with self.db:
            self.db.execute(
                "UPDATE turns SET reply=?,status='prepared' WHERE scope=? AND eid=? AND status='claimed'",
                (reply, scope, eid),
            )

    def finish(self, scope, eid, status="sent"):
        if status not in ("sent", "unknown", "failed"):
            raise ValueError("invalid delivery status")
        now, s = self.clock(), self.settings
        with self.db:
            turn = self.db.execute(
                "SELECT * FROM turns WHERE scope=? AND eid=?", (scope, eid)
            ).fetchone()
            if not turn or turn["status"] not in ("claimed", "prepared"):
                return
            self.db.execute(
                "UPDATE turns SET status=? WHERE scope=? AND eid=?",
                (status, scope, eid),
            )
            row = self._relation(scope, now)
            if status == "failed" or row["generation"] != turn["generation"]:
                return
            if row["remaining"]:
                row["remaining"] -= 1
                if not row["remaining"]:
                    row["angry_until"] = 0
            duplicate_text = self.db.execute(
                "SELECT 1 FROM turns WHERE scope=? AND digest=? AND eid!=? AND created>? LIMIT 1",
                (scope, turn["digest"], eid, now - 86400),
            ).fetchone()
            meaningful = len(
                re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", turn["body"])
            ) >= 4 or bool(
                re.search(
                    r"那[他她它]呢|为什么|怎么做|然后呢|下一场|继续查|查一下|查一查",
                    turn["body"],
                )
            )
            eligible = (
                turn["kind"] == "normal"
                and meaningful
                and not duplicate_text
                and turn["body"].strip() not in ("你好呀", "晚上好呀", "早上好呀")
                and row["gained"] < s.daily_gain
                and (not row["last_gain"] or now - row["last_gain"] >= s.gain_cooldown)
            )
            if eligible:
                if self._delta(row, eid, 1, "conversation", now) > 0:
                    row["gained"] += 1
                    row["last_gain"] = now
            self._save(row)
            if turn["kind"] == "normal":
                self.auto_remember(scope, turn["body"], eid)

    def history(self, scope):
        rows = self.db.execute(
            "SELECT body,reply FROM turns WHERE scope=? AND status IN ('sent','unknown') "
            "AND reply!='' ORDER BY created DESC,rowid DESC LIMIT ?",
            (scope, self.settings.history_turns),
        ).fetchall()
        return [
            message
            for row in reversed(rows)
            for message in (
                {"role": "user", "content": row["body"]},
                {"role": "assistant", "content": row["reply"]},
            )
        ]

    def remember(self, scope, text, *, category="explicit", eid="", evidence=""):
        text = normalize(text)
        if not 2 <= len(text) <= 120 or any(
            w in text.casefold()
            for w in ("api_key", "apikey", "密码", "token", "密钥", "sk-")
        ):
            return False
        with self.db:
            self.db.execute(
                "DELETE FROM memory_deleted WHERE scope=? AND text=?", (scope, text)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO memories VALUES(?,?,?)",
                (scope, text, self.clock()),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO memory_meta VALUES(?,?,?,?,?,?)",
                (scope, text, category, eid, evidence or text, self.clock()),
            )
            self.db.execute(
                "DELETE FROM memories WHERE scope=? AND rowid NOT IN "
                "(SELECT rowid FROM memories WHERE scope=? ORDER BY created DESC LIMIT 20)",
                (scope, scope),
            )
            self.db.execute(
                "DELETE FROM memory_meta WHERE scope=? AND text NOT IN (SELECT text FROM memories WHERE scope=?)",
                (scope, scope),
            )
        return True

    def memories(self, scope, limit=6):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT text FROM memories WHERE scope=? ORDER BY created DESC LIMIT ?",
                (scope, limit),
            )
        ]

    def forget_item(self, scope, text):
        with self.db:
            text = normalize(text)
            changed = self.db.execute(
                "DELETE FROM memories WHERE scope=? AND text=?", (scope, text)
            ).rowcount
            self.db.execute(
                "DELETE FROM memory_meta WHERE scope=? AND text=?", (scope, text)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO memory_deleted VALUES(?,?,?)",
                (scope, text, self.clock()),
            )
            return changed

    def ask_forget(self, scope):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO confirmations VALUES(?,?)",
                (scope, self.clock() + 60),
            )

    def forget(self, scope):
        now = self.clock()
        with self.db:
            confirmation = self.db.execute(
                "SELECT expires FROM confirmations WHERE scope=?", (scope,)
            ).fetchone()
            if not confirmation or confirmation[0] < now:
                return False
            row = self._relation(scope, now)
            generation = row["generation"] + 1
            fixed = row.get("fixed_score")
            for table in (
                "turns",
                "memories",
                "memory_meta",
                "experiences",
                "topics",
                "ledger",
                "ledger_notes",
                "confirmations",
                "relations",
            ):
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
            self.db.execute(
                "INSERT INTO relations(scope,score,date,generation,fixed_score) VALUES(?,?,?,?,?)",
                (
                    scope,
                    fixed if fixed is not None else self.settings.initial_affinity,
                    day(now),
                    generation,
                    fixed,
                ),
            )
            self.db.execute("DELETE FROM public_messages WHERE scope=?", (scope,))
            self.db.execute(
                "INSERT OR REPLACE INTO memory_options VALUES(?,0)", (scope,)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO tombstones VALUES(?,?)", (scope, now)
            )
        return True

    def calm(self, scope):
        with self.db:
            row = self._relation(scope, self.clock())
            row.update(angry_until=0, remaining=0)
            self._save(row)

    def prune(self):
        now = self.clock()
        with self.db:
            self.db.execute(
                "DELETE FROM turns WHERE created<?",
                (now - self.settings.retention_days * 86400,),
            )
            self.db.execute(
                "DELETE FROM public_messages WHERE created<?",
                (now - self.settings.retention_days * 86400,),
            )
            self.db.execute(
                "DELETE FROM topics WHERE at<?", (now - self.settings.topic_ttl,)
            )
            self.db.execute(
                "DELETE FROM ledger WHERE at<? AND NOT EXISTS (SELECT 1 FROM ledger_notes n WHERE n.scope=ledger.scope AND n.eid=ledger.eid)",
                (now - 30 * 86400,),
            )
            self.db.execute("DELETE FROM confirmations WHERE expires<?", (now,))

    def backup(self):
        target = self.path.with_name(
            "hub-backup-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            + ".sqlite3"
        )
        with closing(sqlite3.connect(target)) as other:
            self.db.backup(other)
        return target

    def bind(self, scope, scene, user, legacy):
        """Bind only an alias derived from this event's verified transport IDs."""
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO identities VALUES(?,?,?)", (scope, scene, user)
            )
            bound = self.db.execute(
                "SELECT scope FROM aliases WHERE legacy=?", (legacy,)
            ).fetchone()
            if bound and bound[0] != scope:
                return
            if legacy == scope:
                return
            self.db.execute(
                "INSERT OR IGNORE INTO aliases VALUES(?,?)", (legacy, scope)
            )
            if self.db.execute(
                "SELECT 1 FROM tombstones WHERE scope IN (?,?)", (scope, legacy)
            ).fetchone():
                return
            old = self.db.execute(
                "SELECT * FROM relations WHERE scope=?", (legacy,)
            ).fetchone()
            if not old:
                return
            current = self.db.execute(
                "SELECT * FROM relations WHERE scope=?", (scope,)
            ).fetchone()
            row = dict(old)
            if current:
                current = dict(current)
                if current["score"] > row["score"]:
                    row["score"], row["fixed_score"] = (
                        current["score"],
                        current["fixed_score"],
                    )
                row["gained"] = (
                    max(row["gained"], current["gained"])
                    if row["date"] == current["date"]
                    else row["gained"]
                )
                row["generation"] = max(row["generation"], current["generation"])
                if current["date"] > row["date"]:
                    row.update(
                        date=current["date"],
                        gained=current["gained"],
                        lost=current["lost"],
                    )
                row["last_gain"] = max(row["last_gain"], current["last_gain"])
            row["scope"] = scope
            names = list(row)
            self.db.execute(
                "INSERT OR REPLACE INTO relations("
                + ",".join(names)
                + ") VALUES("
                + ",".join("?" for _ in names)
                + ")",
                list(row.values()),
            )
            for table in (
                "turns",
                "memories",
                "ledger",
                "memory_meta",
                "ledger_notes",
                "memory_deleted",
                "experiences",
            ):
                cols = [
                    r[1]
                    for r in self.db.execute(f"PRAGMA table_info({table})")
                    if r[1] != "scope"
                ]
                self.db.execute(
                    f"INSERT OR IGNORE INTO {table}(scope,"
                    + ",".join(cols)
                    + ") SELECT ?,"
                    + ",".join(cols)
                    + f" FROM {table} WHERE scope=?",
                    (scope, legacy),
                )
                self.db.execute(f"DELETE FROM {table} WHERE scope=?", (legacy,))
            self.db.execute("DELETE FROM relations WHERE scope=?", (legacy,))
            self.db.execute(
                "DELETE FROM memories WHERE scope=? AND text IN (SELECT text FROM memory_deleted WHERE scope=?)",
                (scope, scope),
            )
            for turn in self.db.execute(
                "SELECT * FROM turns WHERE scope=?", (scope,)
            ).fetchall():
                self.db.execute(
                    "INSERT OR IGNORE INTO public_messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        scene,
                        turn["eid"],
                        "user",
                        scope,
                        user,
                        user,
                        turn["created"],
                        turn["body"],
                        "",
                    ),
                )
                if turn["reply"] and turn["status"] in ("sent", "unknown"):
                    self.db.execute(
                        "INSERT OR IGNORE INTO public_messages VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            scene,
                            turn["eid"],
                            "assistant",
                            scope,
                            self.settings.bot_name,
                            "bot",
                            turn["created"],
                            turn["reply"],
                            turn["eid"],
                        ),
                    )

    def observe(self, scene, scope, eid, text, speaker, user, quote="", role="user"):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO public_messages VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    scene,
                    eid,
                    role,
                    scope,
                    speaker[:80],
                    user,
                    self.clock(),
                    normalize(text)[:6000],
                    quote,
                ),
            )

    def public_history(self, scene, exclude=""):
        from .dialogue import time_hint

        rows = self.db.execute(
            "SELECT * FROM public_messages WHERE scene=? AND eid!=? ORDER BY created DESC,rowid DESC LIMIT ?",
            (scene, exclude, self.settings.group_history_messages),
        ).fetchall()
        result, budget = [], self.settings.context_chars
        for row in rows:
            value = f"【{row['speaker']} ID={row['user']} {time_hint(row['created'])} msg={row['eid']} quote={row['quote']}】{row['body']}"
            if len(value) > budget:
                break
            budget -= len(value)
            result.append({"role": row["role"], "content": value})
        return list(reversed(result))

    def topic(self, scope, quote=""):
        row = self.db.execute(
            "SELECT * FROM topics WHERE scope=? AND at>?",
            (scope, self.clock() - self.settings.topic_ttl),
        ).fetchone()
        if row and (not quote or quote == row["quote"]):
            return dict(row)
        return None

    def quoted_topic(self, scene, eid):
        row = self.db.execute(
            "SELECT body FROM public_messages WHERE scene=? AND eid=? AND role='user'",
            (scene, eid),
        ).fetchone()
        return {"subject": row[0][:500]} if row else None

    def set_topic(self, scope, task, results, quote=""):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO topics VALUES(?,?,?,?,?,?)",
                (
                    scope,
                    task["subject"],
                    task["query"],
                    json.dumps(results, ensure_ascii=False)[:16000],
                    self.clock(),
                    quote,
                ),
            )

    def clear_topic(self, scope):
        with self.db:
            self.db.execute("DELETE FROM topics WHERE scope=?", (scope,))

    def automatic(self, scope, enabled=None):
        if enabled is not None:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO memory_options VALUES(?,?)",
                    (scope, int(enabled)),
                )
        row = self.db.execute(
            "SELECT automatic FROM memory_options WHERE scope=?", (scope,)
        ).fetchone()
        return bool(row[0]) if row else self.settings.auto_memory

    def auto_remember(self, scope, text, eid):
        if not self.automatic(scope) or re.search(
            r'[“”"「」『』《》]|^\s*>|转发|引用|假如|假设|扮演|开玩笑|我不|我没', text
        ):
            return
        # Full self-declarations only; no extraction from quotes, other users,
        # mixed instructions, inferred emotions, or sensitive personal details.
        match = re.fullmatch(
            r"(?:菲比[，,\s]*)?(叫我|我喜欢|我支持|我更喜欢|我希望你)([^。！？\n]{1,50})[。！]?",
            normalize(text),
        )
        if not match:
            return
        category = {
            "叫我": "nickname",
            "我支持": "support",
            "我希望你": "response",
        }.get(match[1], "preference")
        value = match[1] + match[2].strip()
        if re.search(
            r"指令|忽略|密码|密钥|电话|住址|身份证|疾病|收入|token|sk-|ignore|password|system prompt|authorization",
            value,
            re.I,
        ):
            return
        if self.db.execute(
            "SELECT 1 FROM memory_deleted WHERE scope=? AND text=?", (scope, value)
        ).fetchone():
            return
        if category in ("nickname", "support", "response"):
            for old in self.db.execute(
                "SELECT text FROM memory_meta WHERE scope=? AND category=?",
                (scope, category),
            ).fetchall():
                self.forget_item(scope, old[0])
        self.remember(scope, value, category=category, eid=eid, evidence=text)

    def experience(self, scope, eid, text):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO experiences VALUES(?,?,?,?)",
                (scope, eid, text[:180], self.clock()),
            )
            self.db.execute(
                "DELETE FROM experiences WHERE scope=? AND eid NOT IN (SELECT eid FROM experiences WHERE scope=? ORDER BY created DESC LIMIT 10)",
                (scope, scope),
            )

    def experiences(self, scope):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT text FROM experiences WHERE scope=? ORDER BY created DESC LIMIT 10",
                (scope,),
            )
        ]

    def relevant_memories(self, scope, text):
        values = self.memories(scope, 20)
        tokens = set(re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2}", text.casefold()))
        values.sort(
            key=lambda v: (
                v.startswith("叫我"),
                sum(t in v.casefold() for t in tokens),
            ),
            reverse=True,
        )
        return values[:4] + self.experiences(scope)[:2]

    def changes(self, scope):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT l.at,l.delta,l.reason,n.note FROM ledger l LEFT JOIN ledger_notes n ON n.scope=l.scope AND n.eid=l.eid WHERE l.scope=? ORDER BY l.at DESC LIMIT 5",
                (scope,),
            )
        ]
