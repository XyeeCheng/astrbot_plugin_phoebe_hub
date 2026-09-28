"""Installed-framework integration tests; no bot startup, API calls or QQ sends."""
import asyncio
import importlib.util
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
spec = importlib.util.spec_from_file_location("phoebe_smoke", ROOT / "main.py", submodule_search_locations=[str(ROOT)])
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
        super().__init__(text, msg, PlatformMetadata(name="qq_official", description="mock", id="test-platform"), group)
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
        self.sent.append((umo, "".join(getattr(item, "text", "") for item in chain.chain)))

    def get_all_stars(self):
        return self.stars


class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dirpatch = patch.object(StarTools, "get_data_dir", return_value=Path(self.tmp.name))
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

    async def test_group_and_user_history_isolated(self):
        await self.chat("记得刚刚的话哦")
        await self.chat("第二次说话了", eid="2")
        self.assertEqual(len(self.context.calls[-1]["contexts"]), 2)
        await self.chat("换了一个人哦", eid="3", user="bob")
        self.assertEqual(self.context.calls[-1]["contexts"], [])
        await self.chat("换了一个群哦", eid="4", group="another")
        self.assertEqual(self.context.calls[-1]["contexts"], [])

    async def test_long_answer_compressed_before_send_and_history(self):
        self.context.output = "长句。" * 40
        event = await self.chat("你解释一下呀")
        self.assertEqual(event.sent, ["我看过了，关键是先把条件弄清楚。"])
        self.assertEqual(self.plugin.store.history(self.plugin.scope(event))[-1]["content"], event.sent[0])

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
        self.assertEqual(self.plugin.store.status(self.plugin.scope(event))["score"], before)
        self.assertEqual(self.plugin.locks, {})

    async def test_proactive_adapter_final_text_and_unload_restore(self):
        class Proactive:
            async def _generate_llm_response(self, session_id, session_config, history_messages, system_prompt, unanswered_count):
                self.system = system_prompt
                return "长稿。" * 30, "motivation"
            async def _send_proactive_message(self, session_id, text):
                raise AssertionError("old segmented sender must not be called")
        obj = Proactive()
        original = obj._generate_llm_response
        self.context.stars = [SimpleNamespace(name="astrbot_plugin_proactive_chat", star_cls=obj, activated=True)]
        self.plugin.adapters.install()
        result, prompt = await obj._generate_llm_response("test", {}, [], "old persona", 0)
        await obj._send_proactive_message("test", result)
        self.assertEqual(self.context.sent[0][1], result)
        self.assertIn("傲娇感强", obj.system)
        self.plugin.adapters.close()
        self.assertEqual(obj._generate_llm_response, original)

    async def test_dsh_failure_falls_back_once(self):
        class Bridge:
            async def run(self, *args):
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

        calls = []
        class MockProvider(Provider):
            def __init__(self):
                super().__init__({"id": "configured-provider", "type": "mock", "max_context_tokens": 32768}, {})
                self.set_model("mock-model")
                self.count = 0
            def get_current_key(self):
                return "local-mock"
            def set_key(self, key):
                pass
            async def get_models(self):
                return ["mock-model"]
            def meta(self):
                return ProviderMeta(id="configured-provider", model="mock-model", type="mock")
            async def text_chat(self, **kwargs):
                self.count += 1
                if self.count == 1:
                    return LLMResponse(role="assistant", tools_call_name=["query_hltv"], tools_call_args=[{}], tools_call_ids=["call1"])
                return LLMResponse(role="assistant", completion_text="已经查到，比分是2比0。你支持的队赢了。")

        class Query(FunctionTool):
            async def call(self, context, **kwargs):
                calls.append(context.context.event.get_sender_id())
                return "Verified score 2-0."

        provider = MockProvider()
        async def get_provider(_):
            return provider
        self.context.provider_manager = SimpleNamespace(get_provider_by_id=get_provider)
        self.context.tool_loop_agent = MethodType(RealContext.tool_loop_agent, self.context)
        req = ProviderRequest(prompt="查询比赛结果", func_tool=ToolSet(tools=[Query(name="query_hltv", description="只读比分", parameters={"type":"object", "properties":{}})]))
        event = Event("查询比赛结果")
        # Direct call exposes framework validation failures instead of the user-facing fallback.
        await self.plugin._native(event, req, "用工具核对结果，回答两句。", [])
        calls.clear(); provider.count = 0
        await self.plugin.chat(event, req)
        self.assertEqual(calls, ["alice"])
        self.assertEqual(provider.count, 2)
        self.assertEqual(event.sent, ["已经查到，比分是2比0。你支持的队赢了。"])

    async def test_tool_proxy_cannot_send_directly(self):
        event = Event("工具只能查询")
        proxy = module.ReadOnlyToolEvent(event)
        with self.assertRaises(RuntimeError):
            await proxy.send("uncontrolled text")
        self.assertEqual(event.sent, [])

    async def test_meme_one_image_and_no_text_mutation(self):
        folder = Path(self.tmp.name) / "memes" / "angry"
        folder.mkdir(parents=True)
        (folder / "one.png").write_bytes(b"fixture")
        self.plugin.settings = module.Settings.read({"meme_probability": 100, "meme_directory": str(folder.parent)})
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
        self.context.stars = [SimpleNamespace(name="astrbot_plugin_proactive_chat", star_cls=Bad(), activated=True)]
        self.plugin.adapters.install()
        self.assertEqual(self.plugin.adapters.proactive_status, "incompatible")
        self.assertEqual(self.plugin.adapters.patches, [])

    async def test_proactive_unloaded_instance_released(self):
        class Good:
            async def _generate_llm_response(self, session_id, session_config, history_messages, system_prompt, unanswered_count):
                return "你好。", "hi"
            async def _send_proactive_message(self, session_id, text):
                pass
        obj = Good()
        original = obj._send_proactive_message
        self.context.stars = [SimpleNamespace(name="astrbot_plugin_proactive_chat", star_cls=obj, activated=True)]
        self.plugin.adapters.install()
        self.context.stars = []
        self.plugin.adapters.install()
        self.assertEqual(obj._send_proactive_message, original)
        self.assertEqual(self.plugin.adapters.patches, [])


if __name__ == "__main__":
    unittest.main()
