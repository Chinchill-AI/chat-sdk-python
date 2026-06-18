"""Example: expose chat-sdk actions to an LLM agent via ``create_chat_tools``.

Runs entirely against the in-memory ``MockAdapter`` — no platform
credentials required:

    uv run python examples/ai_tools_example.py

``create_chat_tools(chat, ...)`` returns the 17 upstream tool factories
(vercel/chat#492) as ``ChatTool`` dataclasses keyed by upstream's camelCase
tool ids, each carrying:

- ``description``     — model-facing description
- ``input_schema``    — JSON-Schema dict for the tool arguments
- ``execute``         — async callable taking the validated argument dict
- ``needs_approval``  — human-in-the-loop flag (write tools default True)

The dataclasses are SDK-agnostic on purpose: bind them into whatever agent
runtime you use by translating ``input_schema`` into that runtime's schema
layer and calling ``execute`` from its tool dispatcher. The sketch at the
bottom shows the Anthropic tool-use shape; chinchill-api does the same via
its own runner.
"""

from __future__ import annotations

import asyncio

from chat_sdk import Chat, ChatConfig
from chat_sdk.ai import create_chat_tools
from chat_sdk.testing import MockAdapter, MockStateAdapter, create_test_message


async def main() -> None:
    adapter = MockAdapter(name="slack")
    chat = Chat(
        ChatConfig(
            user_name="examplebot",
            adapters={"slack": adapter},
            state=MockStateAdapter(),
        )
    )
    await chat.webhooks["slack"]("request")  # triggers adapter/state init

    # ------------------------------------------------------------------
    # 1. Build the toolset. Presets scope what the model may do:
    #    "reader" (fetch/list only), "messenger" (reader + posting),
    #    "moderator" (everything, including delete/reactions).
    # ------------------------------------------------------------------
    tools = create_chat_tools(
        chat,
        preset="messenger",
        require_approval={"postMessage": False},  # auto-approve plain posts
        overrides={"postMessage": {"description": "Post a markdown reply into the current thread."}},
    )

    print(f"toolset ({len(tools)} tools):")
    for name, tool in sorted(tools.items()):
        gate = "needs approval" if tool.needs_approval else "auto"
        print(f"  {name:24s} [{gate}] {tool.description[:60]}")

    # ------------------------------------------------------------------
    # 2. Execute a tool the way an agent runtime would: validated args in,
    #    JSON-safe result out.
    # ------------------------------------------------------------------
    thread_id = "slack:C123:1234.5678"
    await chat.handle_incoming_message(adapter, thread_id, create_test_message("msg-1", "Hey @examplebot hello"))

    result = await tools["postMessage"].execute(
        {
            "threadId": thread_id,
            "message": {"markdown": "Hello from the example agent!"},
        }
    )
    print(f"\npostMessage -> {result}")

    fetched = await tools["fetchMessages"].execute({"threadId": thread_id})
    print(f"fetchMessages -> {len(fetched['messages'])} message(s)")

    # ------------------------------------------------------------------
    # 3. Binding sketch (Anthropic tool-use shape; any runtime works):
    #
    #    anthropic_tools = [
    #        {
    #            "name": name,
    #            "description": tool.description,
    #            "input_schema": tool.input_schema,
    #        }
    #        for name, tool in tools.items()
    #    ]
    #    ... when the model emits a tool_use block:
    #    if tools[block.name].needs_approval:
    #        ...ask a human first...
    #    result = await tools[block.name].execute(block.input)
    # ------------------------------------------------------------------


if __name__ == "__main__":
    asyncio.run(main())
