import re
import pypdf

def extract_timetable_from_pdf(pdf_path: str):
    reader = pypdf.PdfReader(pdf_path)
    page = reader.pages[0]

    # Collect all text snippets with coordinates: (x, y, text)
    snippets = []
    def visitor(text, cm, tm, fontDict, fontSize):
        t = text.strip()
        if t:
            snippets.append((tm[4], tm[5], t))

    page.extract_text(visitor_text=visitor)

    # 1. Detect Day column headers: e.g. Lundi, Mardi, Mercredi, Jeudi, Vendredi, Samedi, or Monday, Tuesday...
    day_patterns = {
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

    # Find day headers and their dates if present
    # Look for snippets matching day names
    day_columns = []
    for x, y, text in snippets:
        lower = text.lower()
        for pat, eng_name in day_patterns.items():
            if pat == lower:
                # Check if there is a date right below or next to it (e.g. 12/10)
                day_columns.append({"day": eng_name, "raw_day": text, "x": x, "y": y, "date": ""})
                break

    # Sort day columns left to right (by x)
    day_columns.sort(key=lambda d: d["x"])

    # Match dates (like 12/10 or 2026-10-12) to closest day column
    date_regex = re.compile(r'^\d{1,2}/\d{1,2}(?:/\d{2,4})?$')
    for x, y, text in snippets:
        if date_regex.match(text):
            # Find closest day column by X
            if day_columns:
                closest_col = min(day_columns, key=lambda c: abs(c["x"] - x))
                if abs(closest_col["x"] - x) < 60:
                    closest_col["date"] = text

    print("Detected Day Columns:")
    for col in day_columns:
        print(f"  {col['day']} ({col['date']}) at X={col['x']:.1f}")

    # Now find time range blocks: e.g. "09:00 - 12:15" or "13:30 - 16:45"
    # In this timetable, each class card has a time like "09:00 - 12:15", a room like "H311", and a title.
    # Let's inspect all snippets and assign each snippet to its closest day column (within column width tolerance)
    col_width = (day_columns[-1]["x"] - day_columns[0]["x"]) / (len(day_columns) - 1) if len(day_columns) > 1 else 150
    print(f"Estimated column width: {col_width:.1f}")

    # Group snippets by column
    column_buckets = {col["day"]: [] for col in day_columns}
    non_column_snippets = []

    for x, y, text in snippets:
        # Ignore day names, dates in headers, page numbers, title header
        if any(text == c["raw_day"] or text == c["date"] for c in day_columns):
            continue
        if "emploi du temps" in text.lower() or "page 1" in text.lower() or "légende" in text.lower() or "année" in text.lower():
            continue
        if re.match(r'^\d{1,2}h$', text):  # 09h, 10h...
            continue

        # Find closest day column
        closest_col = min(day_columns, key=lambda c: abs(c["x"] - x))
        dist = abs(closest_col["x"] - x)
        if dist < col_width * 0.55:
            column_buckets[closest_col["day"]].append((x, y, text))
        else:
            non_column_snippets.append((x, y, text))

    print("\n--- Grouped By Day Column ---")
    for day, items in column_buckets.items():
        col_info = next(c for c in day_columns if c["day"] == day)
        date_str = col_info.get("date", "")
        # Sort items vertically from top to bottom (y descending in PDF coordinates)
        items.sort(key=lambda item: -item[1])
        print(f"\n[{day} {date_str}]:")
        if not items:
            print("  (No classes / Free)")
        else:
            for it in items:
                print(f"  Y={it[1]:.1f}: {it[2]}")

extract_timetable_from_pdf('data/intake/emploi_du_temps_12-10-2026_14-17-18.pdf')
