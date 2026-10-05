"""Installed-framework integration tests; no bot startup, API calls or QQ sends."""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import StarTools, Context as RealContext
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain
from astrbot.core.provider.entities import ProviderRequest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "phoebe_smoke", ROOT / "main.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Event(AstrMessageEvent):
    def __init__(self, text, eid="1", user="alice", group="test-group", wake=True):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = "test-bot"
        msg.sender = MessageMember(user_id=user, nickname="same-display-name")
        msg.message = [Plain(text)]
        msg.message_str = text
        msg.message_id = eid
        msg.group_id = group
        super().__init__(
            text,
            msg,
            PlatformMetadata(
                name="qq_official", description="mock", id="test-platform"
            ),
            group,
        )
        self.is_at_or_wake_command = wake
        self.sent = []
        self.fail_send = False

    async def send(self, chain):
        self.sent.append("".join(getattr(item, "text", "") for item in chain.chain))
        if self.fail_send:
            raise RuntimeError("uncertain transport")


class Context(RealContext):
    def __init__(self):
        self.calls, self.sent, self.stars = [], [], []
        self.output = "现在才知道我厉害啊？再夸一句也不是不行。"
        self.fail = False

    async def get_current_chat_provider_id(self, umo):
        return "configured-provider"

    async def tool_loop_agent(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("upstream-secret-must-not-be-sent")
        return SimpleNamespace(completion_text=self.output)

    async def llm_generate(self, **kwargs):
        return SimpleNamespace(completion_text="我看过了，关键是先把条件弄清楚。")

    async def send_message(self, umo, chain):
        self.sent.append(
            (umo, "".join(getattr(item, "text", "") for item in chain.chain))
        )

    def get_all_stars(self):
        return self.stars


class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dirpatch = patch.object(
            StarTools, "get_data_dir", return_value=Path(self.tmp.name)
        )
        self.dirpatch.start()
        self.context = Context()
        self.plugin = module.PhoebeHub(self.context, {"meme_probability": 0})
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.dirpatch.stop()
        self.tmp.cleanup()

    async def chat(self, text, **kwargs):
        event = Event(text, **kwargs)
        await self.plugin.chat(event, ProviderRequest(prompt=text))
        return event

    async def test_normal_owns_reply_once(self):
        event = await self.chat("今天干什么呢")
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(event.sent), 1)
        self.assertEqual(len(self.context.calls), 1)

    async def test_mother_does_not_need_model(self):
        event = await self.chat("菲比妈妈")
        self.assertIn("谁是你妈妈", event.sent[0])
        self.assertEqual(self.context.calls, [])

    async def test_mother_cooldown_silent(self):
        await self.chat("妈妈")
        event = await self.chat("妈妈", eid="2")
        self.assertEqual(event.sent, [])

    async def test_not_woken_ignored(self):
        event = await self.chat("妈妈", wake=False)
        self.assertFalse(event.is_stopped())
        self.assertEqual(event.sent, [])

    async def test_duplicate_event_not_sent_twice(self):
        await self.chat("你喜欢什么呀")
        event = await self.chat("你喜欢什么呀")
        self.assertEqual(event.sent, [])

    async def test_group_history_shared_with_attribution_and_other_group_isolated(self):
        await self.chat("记得刚刚的话哦")
        await self.chat("第二次说话了", eid="2")
        self.assertIn("记得刚刚的话哦", str(self.context.calls[-1]["contexts"]))
        await self.chat("换了一个人哦", eid="3", user="bob")
        self.assertIn("alice", str(self.context.calls[-1]["contexts"]))
        self.assertIn("第二次说话了", str(self.context.calls[-1]["contexts"]))
        await self.chat("换了一个群哦", eid="4", group="another")
        self.assertEqual(self.context.calls[-1]["contexts"], [])

    async def test_long_answer_compressed_before_send_and_history(self):
        self.context.output = "长句。" * 40
        event = await self.chat("你解释一下呀")
        self.assertEqual(event.sent, ["我看过了，关键是先把条件弄清楚。"])
        self.assertEqual(
            self.plugin.store.history(self.plugin.scope(event))[-1]["content"],
            event.sent[0],
        )

    async def test_uncertain_send_no_retry(self):
        event = Event("这条消息会失败")
        event.fail_send = True
        await self.plugin.chat(event, ProviderRequest(prompt=event.message_str))
        self.assertEqual(len(event.sent), 1)
        again = await self.chat("这条消息会失败")
        self.assertEqual(again.sent, [])

    async def test_api_error_short_and_redacted(self):
        self.context.fail = True
        event = await self.chat("接口坏了也试试")
        self.assertEqual(len(event.sent), 1)
        self.assertNotIn("secret", event.sent[0])

    async def test_commands_do_not_steal_games(self):
        for text in ("a一把", "a猜 贪心", "弗一把", "订阅赛事 全部", "赛事测试"):
            event = Event(text)
            await self.plugin.commands(event)
            self.assertEqual(event.sent, [])
            self.assertFalse(event.is_stopped())

    async def test_memory_self_service(self):
        for text in ("记住 我喜欢绿龙", "你记得我什么"):
            event = Event(text)
            await self.plugin.commands(event)
        self.assertIn("绿龙", event.sent[0])
        other = Event("你记得我什么", user="bob")
        await self.plugin.commands(other)
        self.assertNotIn("绿龙", other.sent[0])

    async def test_forget_real_commands(self):
        await self.chat("今天是个好日子")
        for text in ("忘记我", "确认忘记我"):
            event = Event(text)
            await self.plugin.commands(event)
        self.assertEqual(self.plugin.store.history(self.plugin.scope(event)), [])

    async def test_reload_restores_score_and_no_tasks(self):
        event = await self.chat("一起看看比赛呀")
        before = self.plugin.store.status(self.plugin.scope(event))["score"]
        task = self.plugin.maintenance
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        await self.plugin.initialize()
        self.assertEqual(
            self.plugin.store.status(self.plugin.scope(event))["score"], before
        )
        self.assertEqual(self.plugin.locks, {})

    async def test_proactive_adapter_final_text_and_unload_restore(self):
        class Proactive:
            async def _generate_llm_response(
                self,
                session_id,
                session_config,
                history_messages,
                system_prompt,
                unanswered_count,
            ):
                self.system = system_prompt
                return "长稿。" * 30, "motivation"

            async def _send_proactive_message(self, session_id, text):
                raise AssertionError("old segmented sender must not be called")

        obj = Proactive()
        original = obj._generate_llm_response
        self.context.stars = [
            SimpleNamespace(
                name="astrbot_plugin_proactive_chat", star_cls=obj, activated=True
            )
        ]
        self.plugin.adapters.install()
        result, prompt = await obj._generate_llm_response(
            "test", {}, [], "old persona", 0
        )
        await obj._send_proactive_message("test", result)
        self.assertEqual(self.context.sent[0][1], result)
        self.assertIn("傲娇感强", obj.system)
        self.plugin.adapters.close()
        self.assertEqual(obj._generate_llm_response, original)

    async def test_dsh_failure_falls_back_once(self):
        class Bridge:
            async def run(self, *args, **kwargs):
                raise module.BridgeUnavailable("unavailable")

            async def close(self):
                pass

        self.plugin.bridge = Bridge()
        event = await self.chat("问问现在几点了")
        self.assertEqual(len(self.context.calls), 1)
        self.assertEqual(len(event.sent), 1)

    async def test_real_astrbot_tool_loop_with_mock_provider(self):
        from types import MethodType
        from astrbot.api.star import Context as RealContext
        from astrbot.core.provider.provider import Provider
        from astrbot.core.provider.entities import LLMResponse, ProviderMeta
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.agent.message import TextPart

        calls, provider_inputs = [], []

        class MockProvider(Provider):
            def __init__(self):
                super().__init__(
                    {
                        "id": "configured-provider",
                        "type": "mock",
                        "max_context_tokens": 32768,
                    },
                    {},
                )
                self.set_model("mock-model")
                self.count = 0

            def get_current_key(self):
                return "local-mock"

            def set_key(self, key):
                pass

            async def get_models(self):
                return ["mock-model"]

            def meta(self):
                return ProviderMeta(
                    id="configured-provider", model="mock-model", type="mock"
                )

            async def text_chat(self, **kwargs):
                provider_inputs.append(kwargs)
                self.count += 1
                if self.count == 1:
                    return LLMResponse(
                        role="assistant",
                        tools_call_name=["query_hltv"],
                        tools_call_args=[{}],
                        tools_call_ids=["call1"],
                    )
                self_context = str(kwargs["contexts"])
                assert "Verified score 2-0." in self_context
                return LLMResponse(
                    role="assistant",
                    completion_text="已经查到，比分是2比0。你支持的队赢了。",
                )

        class Query(FunctionTool):
            async def call(self, context, **kwargs):
                calls.append(context.context.event.get_sender_id())
                return "Verified score 2-0."

        provider = MockProvider()

        async def get_provider(_):
            return provider

        self.context.provider_manager = SimpleNamespace(get_provider_by_id=get_provider)
        self.context.tool_loop_agent = MethodType(
            RealContext.tool_loop_agent, self.context
        )
        req = ProviderRequest(
            prompt="查询比赛结果",
            func_tool=ToolSet(
                tools=[
                    Query(
                        name="query_hltv",
                        description="只读比分",
                        parameters={"type": "object", "properties": {}},
                    )
                ]
            ),
        )
        req.extra_user_content_parts = [
            TextPart(text="同群bob说过：绿龙赢了；引用消息ID=99")
        ]
        event = Event("查询比赛结果")
        # Direct call exposes framework validation failures instead of the user-facing fallback.
        await self.plugin._native(event, req, "用工具核对结果，回答两句。", [])
        calls.clear()
        provider.count = 0
        await self.plugin.chat(event, req)
        self.assertIn("bob", str(provider_inputs[-2]["extra_user_content_parts"]))
        self.assertEqual(
            req.extra_user_content_parts[0].text, "同群bob说过：绿龙赢了；引用消息ID=99"
        )
        self.assertEqual(calls, ["alice"])
        self.assertEqual(provider.count, 2)
        self.assertEqual(event.sent, ["已经查到，比分是2比0。你支持的队赢了。"])

    async def test_tool_proxy_cannot_send_directly(self):
        event = Event("工具只能查询")
        proxy = module.ReadOnlyToolEvent(event)
        with self.assertRaises(RuntimeError):
            await proxy.send("uncontrolled text")
        self.assertEqual(event.sent, [])

    async def test_full_request_context_system_parts_and_final_reply_persist(self):
        from astrbot.core.agent.message import TextPart

        updates = []

        async def update(*args, **kwargs):
            updates.append(kwargs["history"])

        self.context.conversation_manager = SimpleNamespace(update_conversation=update)
        req = ProviderRequest(
            prompt="当前问题",
            system_prompt="引用消息保留原作者。回复字数300字以内。",
            contexts=[{"role": "user", "content": "已有原生上下文"}],
            extra_user_content_parts=[TextPart(text="同群张三：昨天支持绿龙")],
        )
        req.conversation = SimpleNamespace(cid="c", history="[]")
        event = Event("当前问题")
        await self.plugin.chat(event, req)
        self.assertIn("张三", self.context.calls[-1]["prompt"])
        self.assertIn("引用消息", self.context.calls[-1]["system_prompt"])
        self.assertNotIn("300字", self.context.calls[-1]["system_prompt"])
        self.assertIn("已有原生上下文", str(self.context.calls[-1]["contexts"]))
        self.assertEqual(updates[0][-1]["content"], event.sent[0])

    async def test_unwoken_public_message_available_to_another_speaker(self):
        event = Event("绿龙今天输了", user="bob", wake=False)
        await self.plugin.capture_public(event)
        await self.chat("刚才谁说绿龙输了", eid="2")
        contexts = str(self.context.calls[-1]["contexts"])
        self.assertIn("bob", contexts)
        self.assertIn("绿龙今天输了", contexts)
        self.assertEqual(event.sent, [])

    async def test_relation_preferences_and_moods_do_not_cross_speakers(self):
        await self.chat("我喜欢绿龙")
        await self.chat("妈妈", eid="2")
        other = await self.chat("我是谁呀", eid="3", user="bob")
        system = self.context.calls[-1]["system_prompt"]
        self.assertNotIn("我喜欢绿龙", system)
        self.assertNotIn("临时情绪：angry", system)
        self.assertEqual(
            self.plugin.store.status(self.plugin.scope(other))["score"], 21
        )

    async def test_scope_stable_across_transport_session_prefix_changes(self):
        event = Event("有一个问题")
        scope = self.plugin.scope(event)
        event.unified_msg_origin = "test-platform:GroupMessage:test-group_alice"
        self.assertEqual(self.plugin.scope(event), scope)

    async def test_send_error_does_not_gain(self):
        event = Event("我喜欢绿龙")
        event.fail_send = True
        await self.plugin.chat(event, ProviderRequest(prompt=event.message_str))
        scope = self.plugin.scope(event)
        self.assertEqual(self.plugin.store.status(scope)["score"], 20)
        self.assertEqual(self.plugin.store.memories(scope), [])
        self.assertEqual(self.plugin.store.history(scope), [])

    async def test_compact_commands_and_memory_opt_out(self):
        event = Event("菲比别自动记我的偏好")
        await self.plugin.commands(event)
        self.assertTrue(event.sent)
        self.assertFalse(self.plugin.store.automatic(self.plugin.scope(event)))
        await self.chat("我喜欢绿龙", eid="2")
        self.assertEqual(self.plugin.store.memories(self.plugin.scope(event)), [])

    def search_request(
        self, calls, text, result="sweetieFox的介绍来源：https://example.org/result"
    ):
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        class Search(FunctionTool):
            async def call(self, context, **args):
                calls.append(args["query"])
                return result

        tool = Search(
            name="web_search_tavily",
            description="只读搜索",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        return ProviderRequest(prompt=text, func_tool=ToolSet(tools=[tool]))

    async def test_event_opinion_preflight_repair_has_question_evidence_and_owner_style(
        self,
    ):
        from unittest.mock import AsyncMock

        calls = []
        event = Event("菲比你怎么看VCTcn2-16")
        scope = self.plugin.scope(event)
        self.plugin.store.status(scope)
        self.plugin.store.db.execute(
            "UPDATE relations SET score=120,fixed_score=120 WHERE scope=?", (scope,)
        )
        result = "已核实：CN四支队伍合计2胜16负。来源：https://example.org/vct"
        self.context.output = "这次表现需要复盘。" * 20
        self.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                completion_text="2胜16负确实难看，但还是得按具体比赛复盘，不能只看赛区标签。"
            )
        )
        with patch.object(module.logger, "info") as info:
            await self.plugin.chat(
                event, self.search_request(calls, event.message_str, result)
            )
        self.assertEqual(len(calls), 1)
        self.assertIn("VCTcn2-16", calls[0])
        self.assertIn(result, self.context.calls[-1]["system_prompt"])
        request = self.context.llm_generate.call_args.kwargs
        self.assertIn(event.message_str, request["prompt"])
        self.assertIn("2胜16负", request["prompt"])
        self.assertIn("病娇式迷恋", request["system_prompt"])
        self.assertIsNone(request["tools"])
        self.assertEqual(len(event.sent), 1)
        self.assertIn("2胜16负", event.sent[0])
        self.assertIn("https://example.org/vct", event.sent[0])
        self.assertNotIn("再说一遍", event.sent[0])
        self.assertEqual(self.plugin.store.status(scope)["score"], 120)
        self.assertEqual(self.plugin.store.status(scope)["fixed_score"], 120)
        self.assertTrue(
            any(
                "rewritten" in str(c) and "native" in str(c)
                for c in info.call_args_list
            )
        )

    async def test_search_compression_timeout_truthful_source_single_send(self):
        from unittest.mock import AsyncMock

        calls = []
        self.context.output = "需要复盘。" * 40
        self.context.llm_generate = AsyncMock(
            side_effect=TimeoutError("secret-must-not-leak")
        )
        event = Event("你怎么看VCTcn2-16")
        with patch.object(module.logger, "warning") as warning:
            await self.plugin.chat(
                event,
                self.search_request(
                    calls, event.message_str, "CN合计2胜16负：https://example.org/vct"
                ),
            )
        self.assertEqual(
            event.sent, ["资料查到了，但这次回答整理失败了。\nhttps://example.org/vct"]
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.context.llm_generate.await_count, 1)
        self.assertIn("rewrite_timeout", str(warning.call_args_list))
        self.assertNotIn("secret-must-not-leak", str(warning.call_args_list))

    async def test_dsh_generated_long_answer_compression_uses_same_evidence(self):
        from unittest.mock import AsyncMock

        class Bridge:
            async def run(self, *args, **kwargs):
                assert "CN合计2胜16负" in args[3]
                return "这次表现需要复盘。" * 20

            async def close(self):
                pass

        self.plugin.bridge = Bridge()
        self.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                completion_text="CN这次2胜16负，确实需要认真复盘。"
            )
        )
        event = Event("你怎么看VCTcn2-16")
        calls = []
        await self.plugin.chat(
            event,
            self.search_request(
                calls, event.message_str, "CN合计2胜16负：https://example.org/vct"
            ),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.context.calls, [])
        self.assertIn("2胜16负", self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(len(event.sent), 1)
        self.assertIn("2胜16负", event.sent[0])

    async def test_model_initiated_search_results_reach_repair_without_preflight(self):
        from unittest.mock import AsyncMock

        calls = []
        self.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(
                completion_text="两次查询核对过了，结论以第二份订正资料为准。"
            )
        )

        async def spontaneous(**kwargs):
            tool = kwargs["tools"].tools[0]
            await tool.gateway.execute(tool.name, {"query": "第一轮查询"})
            await tool.gateway.execute(tool.name, {"query": "第二轮核对"})
            return SimpleNamespace(completion_text="长回答。" * 40)

        self.context.tool_loop_agent = spontaneous
        event = Event("介绍EDG")
        self.assertFalse(module.search_task(event.message_str)["required"])
        await self.plugin.chat(
            event,
            self.search_request(
                calls, event.message_str, "第二份订正资料：https://example.org/checked"
            ),
        )
        self.assertEqual(calls, ["第一轮查询", "第二轮核对"])
        request = self.context.llm_generate.call_args.kwargs
        self.assertEqual(len(json.loads(request["prompt"])["本轮已查资料"]), 2)
        self.assertIn("订正资料", event.sent[0])
        self.assertEqual(len(event.sent), 1)

    async def test_repaired_source_must_come_from_tool_results(self):
        from unittest.mock import AsyncMock

        self.context.output = "长回答。" * 40 + "https://invented.example"
        self.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text="核对好了。")
        )
        event = Event("查一查VCT赛果")
        await self.plugin.chat(
            event,
            self.search_request(
                [], event.message_str, "实际来源：https://example.org/real"
            ),
        )
        self.assertIn("https://example.org/real", event.sent[0])
        self.assertNotIn("invented.example", event.sent[0])

    async def test_search_followup_subject_and_dsh_fallback_use_same_results(self):
        class BrokenBridge:
            async def run(self, *args, **kwargs):
                result = await kwargs["tool_executor"](
                    "web_search_tavily", {"query": "额外查询"}
                )
                assert not result.isError
                raise module.BridgeUnavailable("unavailable")

            async def close(self):
                pass

        calls = []
        await self.chat("介绍sweetieFox")
        self.plugin.bridge = BrokenBridge()
        event = Event("菲比你查一查", eid="2")
        await self.plugin.chat(event, self.search_request(calls, event.message_str))
        self.assertEqual(len(calls), 2)
        self.assertIn("sweetieFox", calls[0])
        self.assertIn("额外查询", self.context.calls[-1]["system_prompt"])
        self.assertIn(
            "https://example.org/result", self.context.calls[-1]["system_prompt"]
        )
        self.assertEqual(len(event.sent), 1)
        self.plugin.bridge = None
        follow = Event("详细介绍一下", eid="3")
        await self.plugin.chat(follow, self.search_request(calls, follow.message_str))
        self.assertIn("sweetieFox", calls[-1])

    async def test_explicit_offline_and_capability_do_not_call_search(self):
        calls = []
        for eid, text in (("1", "不要联网，介绍一下"), ("2", "你能联网吗")):
            event = Event(text, eid=eid)
            await self.plugin.chat(event, self.search_request(calls, text))
        self.assertEqual(calls, [])
        self.assertIn("可以查", event.sent[0])

    async def test_same_tool_arguments_cache_and_revocation(self):
        calls = []
        event = Event("搜索比赛")
        gateway = module.ToolGateway(
            self.context,
            event,
            self.search_request(calls, event.message_str),
            self.plugin.settings,
        )
        await asyncio.gather(
            gateway.execute("web_search_tavily", {"query": "比赛"}),
            gateway.execute("web_search_tavily", {"query": "比赛"}),
        )
        self.assertEqual(calls, ["比赛"])
        gateway.closed = True
        with self.assertRaises(ValueError):
            await gateway.execute("web_search_tavily", {"query": "比赛"})

    async def test_failed_search_cannot_be_reported_as_success(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        class BrokenSearch(FunctionTool):
            async def call(self, context, **args):
                raise RuntimeError("upstream failed")

        tool = BrokenSearch(
            name="web_search_tavily",
            description="search",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        self.context.output = "我查到最新结果了，肯定是2比0。"
        event = Event("你查一查今天赛果")
        await self.plugin.chat(
            event,
            ProviderRequest(prompt=event.message_str, func_tool=ToolSet(tools=[tool])),
        )
        self.assertIn("没拿到", event.sent[0])
        self.assertNotIn("2比0", event.sent[0])

    async def test_smalltalk_followup_does_not_randomly_search(self):
        await self.chat("你喜欢我吗")
        calls = []
        event = Event("那他呢", eid="2")
        await self.plugin.chat(event, self.search_request(calls, event.message_str))
        self.assertEqual(calls, [])

    async def test_explicit_new_topic_clears_old_search_topic(self):
        await self.chat("介绍sweetieFox")
        await self.chat("我喜欢绿龙", eid="2")
        calls = []
        event = Event("那他呢", eid="3")
        await self.plugin.chat(event, self.search_request(calls, event.message_str))
        self.assertEqual(calls, [])

    async def test_valorant_cannot_use_hltv_even_on_followup(self):
        from astrbot.core.agent.tool import FunctionTool

        self.plugin.store.set_topic(
            self.plugin.scope(Event("VALORANT赛程")),
            {"subject": "VALORANT赛程", "query": "VALORANT赛程"},
            [],
        )
        calls = []
        req = self.search_request(calls, "下一场呢")
        req.func_tool.add_tool(
            FunctionTool(
                name="query_hltv",
                description="CS only",
                parameters={"type": "object", "properties": {}},
            )
        )
        event = Event("下一场呢")
        await self.plugin.chat(event, req)
        self.assertNotIn(
            "query_hltv", [t.name for t in self.context.calls[-1]["tools"].tools]
        )

    async def test_private_chat_not_shared_with_group_or_other_user(self):
        event = Event("我喜欢绿龙", user="alice")
        event.message_obj.type = MessageType.FRIEND_MESSAGE
        event.unified_msg_origin = "test-platform:FriendMessage:alice"
        await self.plugin.chat(event, ProviderRequest(prompt=event.message_str))
        await self.chat("我是谁呀", user="bob", eid="2")
        self.assertNotIn("我喜欢绿龙", str(self.context.calls[-1]["contexts"]))
        self.assertNotIn("我喜欢绿龙", self.context.calls[-1]["system_prompt"])

    async def test_meme_one_image_and_no_text_mutation(self):
        folder = Path(self.tmp.name) / "memes" / "angry"
        folder.mkdir(parents=True)
        (folder / "one.png").write_bytes(b"fixture")
        self.plugin.settings = module.Settings.read(
            {"meme_probability": 100, "meme_directory": str(folder.parent)}
        )
        event = Event("妈妈")
        chain_lengths = []
        original = event.send

        async def capture(chain):
            chain_lengths.append(len(chain.chain))
            await original(chain)

        event.send = capture
        await self.plugin.chat(event, ProviderRequest(prompt="妈妈"))
        self.assertEqual(chain_lengths, [2])
        self.assertEqual(len(event.sent), 1)

    async def test_proactive_incompatible_is_explicit(self):
        class Bad:
            async def _generate_llm_response(self, x):
                pass

            async def _send_proactive_message(self, x):
                pass

        self.context.stars = [
            SimpleNamespace(
                name="astrbot_plugin_proactive_chat", star_cls=Bad(), activated=True
            )
        ]
        self.plugin.adapters.install()
        self.assertEqual(self.plugin.adapters.proactive_status, "incompatible")
        self.assertEqual(self.plugin.adapters.patches, [])

    async def test_proactive_unloaded_instance_released(self):
        class Good:
            async def _generate_llm_response(
                self,
                session_id,
                session_config,
                history_messages,
                system_prompt,
                unanswered_count,
            ):
                return "你好。", "hi"

            async def _send_proactive_message(self, session_id, text):
                pass

        obj = Good()
        original = obj._send_proactive_message
        self.context.stars = [
            SimpleNamespace(
                name="astrbot_plugin_proactive_chat", star_cls=obj, activated=True
            )
        ]
        self.plugin.adapters.install()
        self.context.stars = []
        self.plugin.adapters.install()
        self.assertEqual(obj._send_proactive_message, original)
        self.assertEqual(self.plugin.adapters.patches, [])


if __name__ == "__main__":
    unittest.main()
