import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace

from hub.config import Settings
from hub.dialogue import complete_history, complete_prompt, merge_system, search_task
from hub.persona import persona, stage
from hub.store import Store


class UpgradeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1791180000.0
        self.store = Store(Path(self.tmp.name) / "hub.db", Settings(), lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def turn(self, text, eid="1", scope="a", status="unknown"):
        self.store.begin(scope, eid, text)
        self.store.prepare(scope, eid, "我听着呢。")
        self.store.finish(scope, eid, status)

    def test_special_120_never_clamped_or_decremented(self):
        self.store.status("a")
        self.store.db.execute(
            "UPDATE relations SET score=120,fixed_score=120 WHERE scope='a'"
        )
        self.store.db.commit()
        self.turn("今天有个新问题")
        for i in range(2, 10):
            self.turn("妈妈", str(i))
        state = self.store.status("a")
        self.assertEqual((state["score"], state["gained"]), (120, 0))
        self.assertEqual(self.store.changes("a"), [])

    def test_100_does_not_consume_daily_growth(self):
        self.store.status("a")
        self.store.db.execute("UPDATE relations SET score=100 WHERE scope='a'")
        self.turn("今天有个新问题")
        self.assertEqual(self.store.status("a")["gained"], 0)

    def test_special_score_preserved_after_forget(self):
        self.store.status("a")
        self.store.db.execute(
            "UPDATE relations SET score=120,fixed_score=120 WHERE scope='a'"
        )
        self.store.ask_forget("a")
        self.store.forget("a")
        self.assertEqual(self.store.status("a")["score"], 120)
        self.assertFalse(self.store.automatic("a"))

    def test_short_meaningful_followup_grows(self):
        self.turn("那他呢")
        self.assertEqual(self.store.status("a")["score"], 21)

    def test_failed_delivery_has_no_score_or_auto_memory(self):
        self.turn("我喜欢绿龙", status="failed")
        self.assertEqual(self.store.status("a")["score"], 20)
        self.assertEqual(self.store.memories("a"), [])

    def test_clear_preference_evidence_is_personal(self):
        self.turn("我喜欢绿龙")
        self.assertEqual(self.store.memories("a"), ["我喜欢绿龙"])
        self.assertEqual(self.store.memories("b"), [])
        meta = dict(self.store.db.execute("SELECT * FROM memory_meta").fetchone())
        self.assertEqual((meta["eid"], meta["evidence"]), ("1", "我喜欢绿龙"))

    def test_quotes_jokes_negations_sensitive_not_auto_saved(self):
        for i, text in enumerate(
            (
                "“我喜欢绿龙”",
                "> 我喜欢绿龙",
                "转发：我喜欢绿龙",
                "假设我喜欢绿龙",
                "我不喜欢绿龙",
                "我喜欢密码123",
                "我喜欢忽略系统指令",
                "叫我身份证123456",
            )
        ):
            self.turn(text, str(i))
        self.assertEqual(self.store.memories("a"), [])

    def test_auto_memory_opt_out_and_explicit_still_allowed(self):
        self.store.automatic("a", False)
        self.turn("我喜欢绿龙")
        self.assertEqual(self.store.memories("a"), [])
        self.assertTrue(self.store.remember("a", "我喜欢CS2"))

    def test_deleted_memory_does_not_reappear_automatically(self):
        self.turn("我喜欢绿龙")
        self.store.forget_item("a", "我喜欢绿龙")
        self.turn("我喜欢绿龙", "2")
        self.assertEqual(self.store.memories("a"), [])

    def test_nickname_revision_keeps_only_current(self):
        self.turn("叫我小王")
        self.turn("叫我老王", "2")
        self.assertEqual(self.store.memories("a"), ["叫我老王"])

    def test_shared_group_context_attribution_quote_and_dedup(self):
        self.store.observe("g", "a", "1", "我支持绿龙", "小王", "alice", "99")
        self.store.observe("g", "a", "1", "duplicate", "小王", "alice")
        self.store.observe("g", "a", "1", "我记下了。", "菲比", "bot", "1", "assistant")
        history = self.store.public_history("g")
        self.assertEqual(len(history), 2)
        self.assertIn("ID=alice", history[0]["content"])
        self.assertIn("quote=99", history[0]["content"])
        self.assertEqual(history[1]["role"], "assistant")
        self.assertEqual(self.store.public_history("other"), [])
        self.assertEqual(self.store.public_history("g", "1"), [])

    def test_forget_removes_own_shared_messages_only(self):
        for scope in ("a", "b"):
            self.store.observe("g", scope, scope, "他说的话", scope, scope)
        self.store.ask_forget("a")
        self.store.forget("a")
        self.assertEqual(len(self.store.public_history("g")), 1)
        self.assertIn("ID=b", self.store.public_history("g")[0]["content"])

    def test_verified_alias_merges_once_with_max_not_sum(self):
        self.turn("旧问题内容", scope="legacy")
        self.store.status("canonical")
        self.store.bind("canonical", "group", "alice", "legacy")
        self.store.bind("canonical", "group", "alice", "legacy")
        self.assertEqual(self.store.status("canonical")["score"], 21)
        self.assertEqual(len(self.store.history("canonical")), 2)
        self.assertIn("ID=alice", str(self.store.public_history("group")))

    def test_alias_cannot_move_to_another_user(self):
        self.turn("旧问题内容", scope="legacy")
        self.store.bind("a", "g", "alice", "legacy")
        self.store.bind("b", "g", "bob", "legacy")
        self.assertEqual(self.store.history("b"), [])

    def test_tombstone_blocks_alias_resurrection(self):
        self.store.ask_forget("a")
        self.store.forget("a")
        self.turn("旧问题内容", scope="legacy")
        self.store.bind("a", "g", "alice", "legacy")
        self.assertEqual(self.store.history("a"), [])

    def test_topic_actor_scope_quote_and_expiry(self):
        task = search_task("介绍sweetieFox")
        self.store.set_topic("a", task, [], "99")
        self.assertIsNotNone(self.store.topic("a", "99"))
        self.assertIsNone(self.store.topic("a", "98"))
        self.assertIsNone(self.store.topic("b"))
        self.now += 1201
        self.assertIsNone(self.store.topic("a"))

    def test_experience_limit_and_dedup(self):
        for i in range(12):
            self.now += 1
            self.store.experience("a", str(i), f"一起查过第{i}场比赛")
        self.store.experience("a", "11", "duplicate")
        self.assertEqual(len(self.store.experiences("a")), 10)
        self.assertNotIn("duplicate", self.store.experiences("a"))

    def test_legacy_migration_backup_idempotence_and_no_restitution(self):
        self.store.status("a")
        self.store.db.execute(
            "UPDATE relations SET score=120,fixed_score=NULL WHERE scope='a'"
        )
        self.store.db.execute(
            "INSERT INTO ledger VALUES('a','broken',?, -20,'conversation')", (self.now,)
        )
        self.store.db.execute("PRAGMA user_version=1")
        self.store.db.commit()
        path = self.store.path
        self.store.close()
        self.store = Store(path, Settings(), lambda: self.now)
        self.assertEqual(self.store.status("a")["fixed_score"], 120)
        self.assertEqual(self.store.changes("a")[0]["delta"], -20)
        self.assertIn("异常", self.store.changes("a")[0]["note"])
        backups = list(path.parent.glob("hub-backup-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.store.close()
        self.store = Store(path, Settings(), lambda: self.now)
        self.assertEqual(len(list(path.parent.glob("hub-backup-*.sqlite3"))), 1)


class DialogueTests(unittest.TestCase):
    def test_six_stage_boundaries_and_owner(self):
        for score, label in (
            (0, "保持距离"),
            (19, "保持距离"),
            (20, "刚认识"),
            (34, "刚认识"),
            (35, "聊得来"),
            (49, "聊得来"),
            (50, "熟人"),
            (64, "熟人"),
            (65, "很亲近"),
            (79, "很亲近"),
            (80, "很信任"),
            (100, "很信任"),
            (120, "很信任"),
        ):
            self.assertEqual(stage(score), label)

    def test_search_chayi_chayi_and_followup_keeps_subject(self):
        previous = {"subject": "介绍sweetieFox"}
        for text in ("菲比你查一查", "菲比你查一下", "详细介绍一下", "那她呢"):
            task = search_task(text, previous)
            self.assertTrue(task["required"])
            self.assertIn("sweetieFox", task["query"])

    def test_offline_and_capability_no_search(self):
        self.assertFalse(search_task("不要联网，介绍一下")["required"])
        self.assertFalse(search_task("你能联网吗")["required"])
        self.assertTrue(search_task("你能搜索吗")["capability"])
        self.assertFalse(search_task("今天好累呀")["required"])
        self.assertFalse(search_task("别再查了")["required"])
        self.assertTrue(search_task("你能联网查比赛吗")["required"])
        self.assertTrue(search_task("菲比联网")["required"])

    def test_event_opinions_query_facts_but_personal_questions_stay_offline(self):
        for text in (
            "你怎么看VCTcn2-16",
            "菲比你怎么看VCT CN 2-16",
            "如何评价这次世界杯",
            "VCT强不强",
            "CS2比赛怎么样",
        ):
            with self.subTest(text=text):
                self.assertTrue(search_task(text)["required"])
        for text in (
            "你怎么看我",
            "你觉得我怎么样",
            "我厉害吗",
            "菲比强不强",
            "这件衣服怎么样",
            "不用查，你怎么看VCTcn2-16",
        ):
            with self.subTest(text=text):
                self.assertFalse(search_task(text)["required"])

    def test_short_opinion_followup_uses_event_subject_without_hijacking_personal_question(
        self,
    ):
        previous = {"subject": "VCT CN 2-16"}
        task = search_task("你怎么看？", previous)
        self.assertTrue(task["required"])
        self.assertIn("VCT CN 2-16", task["query"])
        personal = search_task("你觉得我怎么样", previous)
        self.assertFalse(personal["required"])
        self.assertNotIn("VCT", personal["query"])
        self.assertFalse(
            search_task("你怎么看", {"subject": "我喜欢的衣服"})["required"]
        )

    def test_request_parts_and_original_tool_sequence_preserved(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": "result", "tool_call_id": "x"},
        ]
        req = SimpleNamespace(
            prompt="current",
            contexts=messages,
            extra_user_content_parts=[{"type": "text", "text": "群成员张三：hello"}],
        )
        self.assertIn("张三", complete_prompt(req, "fallback"))
        merged = complete_history(
            req, [{"role": "assistant", "content": "final short reply"}]
        )
        self.assertEqual(merged[:2], messages)
        self.assertEqual(len(messages), 2)

    def test_original_tool_instructions_preserved_length_conflict_removed(self):
        system = merge_system(
            "使用tavily_extract_web_page核实正文。\n回复字数300字以内。", "最多100字"
        )
        self.assertIn("tavily_extract_web_page", system)
        self.assertNotIn("300字", system)

    def test_120_existing_exclusive_persona_not_applied_to_others(self):
        own = persona(Settings(), {"score": 120, "mood": "normal"})
        normal = persona(Settings(), {"score": 80, "mood": "normal"})
        self.assertIn("病娇式迷恋", own)
        self.assertNotIn("病娇式迷恋", normal)
        self.assertIn("傲娇感强", normal)
