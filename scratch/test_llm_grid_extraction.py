import json
import os
import re
from dotenv import load_dotenv
load_dotenv()
from openai import OpenAI
import pypdf
import sys
sys.path.insert(0, '.')
from scratch.test_spatial_function import extract_spatial_grid_or_text

reader = pypdf.PdfReader('data/intake/emploi_du_temps_12-10-2026_14-17-18.pdf')
grid_text = extract_spatial_grid_or_text(reader.pages[0])

system_prompt = """You are the document intelligence extraction engine for JARVIS.
Today's date is: 2026-10-12.
Sir has provided a college timetable document.
Extract all calendar events across ALL days of the week (Monday through Sunday).
If a day explicitly has no classes scheduled, do NOT generate events for that day.
For every class found on any day (including Friday):
- title: Subject name
- start_time: YYYY-MM-DD HH:MM (using the exact date indicated for that day, e.g. 2026-10-12, 2026-10-13, 2026-10-14, 2026-10-16)
- end_time: YYYY-MM-DD HH:MM
- location: Room code (e.g. H311, G011)

OUTPUT FORMAT:
Output ONLY valid JSON with this exact schema:
{
  "calendar_events": [
    {"title": "...", "start_time": "YYYY-MM-DD HH:MM", "end_time": "YYYY-MM-DD HH:MM", "location": "..."}
  ],
  "tasks": [],
  "knowledge_doc": {
    "title": "College Timetable",
    "category": "schedule",
    "summary": "..."
  }
}"""

client = OpenAI(api_key=os.environ['GROQ_API_KEY'], base_url='https://api.groq.com/openai/v1')
res = client.chat.completions.create(
    model='qwen/qwen3.8-27b',
    messages=[
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': f"Sir's instructions: 'This is my college timetable. Extract all classes and register them on my calendar.'\n\n{grid_text}"}
    ],
    temperature=0.1,
    max_tokens=2500
)
content = res.choices[0].message.content.strip()
content = re.sub(r'^```(?:json)?\s*', '', content)
content = re.sub(r'\s*```$', '', content)
data = json.loads(content)
print("\nExtracted Events:")
for ev in data.get('calendar_events', []):
    print(f"  • {ev['start_time']} -> {ev.get('end_time')}: {ev['title']} ({ev.get('location')})")
