import json
import os
import re
from dotenv import load_dotenv
load_dotenv()
from openai import OpenAI
import pypdf

reader = pypdf.PdfReader('data/intake/emploi_du_temps_12-10-2026_14-17-18.pdf')
layout_text = reader.pages[0].extract_text(extraction_mode='layout')

system_prompt = """You are the document intelligence extraction engine for JARVIS.
Today's date is: 2026-10-12.
Carefully analyze the timetable / document content.
Note that the document is laid out in 2D columns corresponding to days of the week (Lundi = Monday, Mardi = Tuesday, Mercredi = Wednesday, Jeudi = Thursday, Vendredi = Friday, Samedi = Saturday).
Match each class or event strictly under its respective day column based on horizontal alignment.
If a day has no classes aligned under its column (e.g. Jeudi, Samedi), do NOT hallucinate classes for it.
Extract all calendar events with:
- title: subject name
- start_time: YYYY-MM-DD HH:MM
- end_time: YYYY-MM-DD HH:MM
- location: room code (e.g. H311, G011)

OUTPUT FORMAT:
Output ONLY valid JSON with schema:
{
  "calendar_events": [
    {"title": "...", "start_time": "YYYY-MM-DD HH:MM", "end_time": "YYYY-MM-DD HH:MM", "location": "..."}
  ]
}"""

client = OpenAI(api_key=os.environ['GROQ_API_KEY'], base_url='https://api.groq.com/openai/v1')
res = client.chat.completions.create(
    model='qwen/qwen3.8-27b',
    messages=[
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': f'Timetable payload:\n\n{layout_text}'}
    ],
    temperature=0.1,
    max_tokens=1500
)
content = res.choices[0].message.content.strip()
content = re.sub(r'^```(?:json)?\s*', '', content)
content = re.sub(r'\s*```$', '', content)
data = json.loads(content)
for ev in data.get('calendar_events', []):
    print(f"{ev['start_time']} to {ev.get('end_time')} | {ev['title']} ({ev.get('location')})")
