import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import re
from openai import OpenAI
from jarvis.skills.base import SkillRegistry

client = OpenAI(
    api_key="sk-omniroute-jarvis-core-2026",
    base_url="http://127.0.0.1:20128/v1"
)

def extract_next_speech_chunk(buffer: str):
    m = re.search(r'(?<!\d)(?<!\b[A-Za-z])[.!?\n]+(?!\d)\s+', buffer)
    if m:
        idx = m.end()
        return buffer[:idx].strip(), buffer[idx:]
    if len(buffer) > 80:
        m_clause = re.search(r'[,;:]\s+', buffer[30:])
        if m_clause:
            cut_idx = 30 + m_clause.end()
            return buffer[:cut_idx].strip(), buffer[cut_idx:]
    return None, buffer

print("--- Testing Streaming Sentence Dispatch ---")
messages = [{"role": "user", "content": "Explain what a star is in 2 sentences."}]
stream = client.chat.completions.create(
    model="antigravity/claude-sonnet-4-6",
    messages=messages,
    stream=True,
)

buf = ""
sentence_count = 0
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        buf += delta.content
        s, buf = extract_next_speech_chunk(buf)
        while s:
            sentence_count += 1
            print(f"[Sentence {sentence_count} dispatched]: {s}")
            s, buf = extract_next_speech_chunk(buf)

if buf.strip():
    sentence_count += 1
    print(f"[Final Sentence {sentence_count} dispatched]: {buf.strip()}")

print("--- Test Completed Successfully ---")
