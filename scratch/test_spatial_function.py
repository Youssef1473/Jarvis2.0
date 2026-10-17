import datetime
import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
import pypdf
from openai import OpenAI

def extract_spatial_grid_or_text(page) -> str:
    snippets = []
    def visitor(text, cm, tm, fontDict, fontSize):
        t = text.strip()
        if t:
            snippets.append((tm[4], tm[5], t))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        snippets = []

    day_map = {
        "lundi": "Monday",
        "mardi": "Tuesday",
        "mercredi": "Wednesday",
        "jeudi": "Thursday",
        "vendredi": "Friday",
        "samedi": "Saturday",
        "dimanche": "Sunday",
        "monday": "Monday",
        "tuesday": "Tuesday",
        "wednesday": "Wednesday",
        "thursday": "Thursday",
        "friday": "Friday",
        "saturday": "Saturday",
        "sunday": "Sunday"
    }

    # Detect day headers
    day_columns = []
    for x, y, text in snippets:
        lower = text.lower()
        if lower in day_map:
            day_columns.append({"day": day_map[lower], "raw_day": text, "x": x, "y": y, "date": ""})

    if len(day_columns) >= 2:
        # Sort columns left to right by x coordinate
        day_columns.sort(key=lambda d: d["x"])

        # Match dates near headers (e.g. 12/10, 12/10/2026)
        date_pattern = re.compile(r'^\d{1,2}/\d{1,2}(?:/\d{2,4})?$')
        for x, y, text in snippets:
            if date_pattern.match(text):
                closest = min(day_columns, key=lambda c: abs(c["x"] - x))
                if abs(closest["x"] - x) < 70 and abs(closest["y"] - y) < 80:
                    closest["date"] = text

        col_width = (day_columns[-1]["x"] - day_columns[0]["x"]) / (len(day_columns) - 1)

        # Y boundaries for timetable grid: between header row (Y <= 650) and footer legend (Y >= 85)
        grid_items = [it for it in snippets if 85 <= it[1] <= 650]

        # Bucket items by column
        col_buckets = {c["day"]: [] for c in day_columns}
        for x, y, text in grid_items:
            # Ignore headers, page markers, legend labels, isolated hour labels (09h, 10h...)
            if any(text == c["raw_day"] or text == c["date"] for c in day_columns):
                continue
            lower_text = text.lower()
            if any(k in lower_text for k in ["emploi du temps", "page 1", "légende", "legende", "année universitaire", "annee universitaire"]):
                continue
            if re.match(r'^\d{1,2}h$', text):
                continue

            closest = min(day_columns, key=lambda c: abs(c["x"] - x))
            dist = abs(closest["x"] - x)
            if dist < col_width * 0.55:
                col_buckets[closest["day"]].append((x, y, text))

        lines = ["--- Timetable Grid Extracted By Day Column ---"]
        for col in day_columns:
            day_name = col["day"]
            date_str = f" ({col['date']})" if col["date"] else ""
            items = col_buckets[day_name]
            # Sort top-to-bottom
            items.sort(key=lambda it: -it[1])
            lines.append(f"\n### Day: {day_name}{date_str}")
            if not items:
                lines.append("(No classes or events scheduled)")
            else:
                current_block = []
                for it in items:
                    current_block.append(it[2])
                lines.append(" | ".join(current_block))

        return "\n".join(lines)

    # Fallback to layout mode
    try:
        layout_txt = page.extract_text(extraction_mode="layout")
        if layout_txt and len(layout_txt.strip()) > 30:
            return layout_txt
    except Exception:
        pass

    return page.extract_text() or ""

test_pdf = 'data/intake/emploi_du_temps_12-10-2026_14-17-18.pdf'
reader = pypdf.PdfReader(test_pdf)
res_text = extract_spatial_grid_or_text(reader.pages[0])
print(res_text)
