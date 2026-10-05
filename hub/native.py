"""Run AstrBot's native runner with the complete original ProviderRequest."""

import copy

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

from .tool_event import ReadOnlyToolEvent


async def run(context, event, request, provider_id, system, history, tools, prompt):
    provider = await context.provider_manager.get_provider_by_id(provider_id)
    if provider is None:
        raise RuntimeError("Provider unavailable")
    # A shallow copy preserves tools and request-specific multimodal fields;
    # assign fresh collections for fields we change, never mutate the caller.
    req = copy.copy(request)
    req.prompt, req.system_prompt, req.contexts = prompt, system, history
    req.func_tool = tools if tools.tools else None
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=req,
        run_context=AgentContextWrapper(
            context=AstrAgentContext(context=context, event=ReadOnlyToolEvent(event)),
            tool_call_timeout=15,
        ),
        tool_executor=FunctionToolExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=False,
    )
    async for _ in runner.step_until_done(4):
        pass
    response = runner.get_final_llm_resp()
    if response is None:
        raise RuntimeError("No final assistant reply")
    return response
