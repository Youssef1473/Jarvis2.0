import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import re
from openai import OpenAI
from jarvis.skills.base import SkillRegistry

client = OpenAI(
    api_key="sk-omniroute-jarvis-core-2026",
    base_url="http://127.0.0.1:20128/v1"
)

tools = SkillRegistry.get_openai_tools()
print(f"Loaded {len(tools)} tools.")

messages = [
    {"role": "system", "content": "You are Jarvis. You control the user's PC using tools."},
    {"role": "user", "content": "Set the system volume to 65%"}
]

print("Calling with stream=True and tools...")
stream = client.chat.completions.create(
    model="antigravity/claude-sonnet-4-6",
    messages=messages,
    tools=tools,
    stream=True,
)

tool_calls_map = {}
content_chunks = []
for chunk in stream:
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta
    if delta.tool_calls:
        for tc in delta.tool_calls:
            idx = tc.index
            if idx not in tool_calls_map:
                tool_calls_map[idx] = {
                    "id": tc.id or "",
                    "name": tc.function.name or "" if tc.function else "",
                    "arguments": tc.function.arguments or "" if tc.function else "",
                }
            else:
                if tc.id:
                    tool_calls_map[idx]["id"] += tc.id
                if tc.function and tc.function.name:
                    tool_calls_map[idx]["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    tool_calls_map[idx]["arguments"] += tc.function.arguments
    if delta.content:
        content_chunks.append(delta.content)

print(f"Tool calls received: {tool_calls_map}")
print(f"Direct content received: {''.join(content_chunks)}")
