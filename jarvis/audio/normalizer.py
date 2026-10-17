"""Speech Text Normalizer (STN) for Jarvis 2.0.

Converts structured text, timestamps, time ranges, dates, numbers, currency,
units, technical symbols, and room codes into natural, conversational British English
optimized for the Kokoro-82M neural TTS engine and Windows SAPI5.
"""
import re
from typing import Dict

MONTH_NAMES: Dict[int, str] = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December"
}


def ordinal_suffix(n: int) -> str:
    """Returns English ordinal representation (e.g. 1st, 2nd, 3rd, 12th)."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class SpeechNormalizer:
    """Comprehensive Text Normalization pipeline for natural voice synthesis."""

    @classmethod
    def clean_markdown_and_formatting(cls, text: str) -> str:
        """Strips markdown fences, inline code, bold, links, and horizontal rules."""
        if not text:
            return ""
        # Remove markdown code blocks and inline code
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(r"`[^`]*`", "", text)
        # Remove tool calls and inline XML tags
        text = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        # Remove URLs
        text = re.sub(r"https?://\S+", "", text)
        # Remove horizontal rules
        text = re.sub(r"[-*_]{2,}", "", text)
        # Remove emojis and non-standard unicode symbols that disrupt TTS phoneme conversion
        text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
        text = re.sub(r"[\u2600-\u26ff\u2700-\u27bf]", "", text)
        # Remove markdown markers (bold, italic, strikethrough, blockquotes)
        text = re.sub(r"[*_~`]", "", text)
        text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*[-*•]+\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.MULTILINE)
        return text

    @classmethod
    def normalize_times_and_ranges(cls, text: str) -> str:
        """Converts digital times (09:00 a.m., 13:30, 09:00 - 12:15) to conversational speech."""
        if not text:
            return ""

        # 1. Normalize time ranges: '09:00 - 12:15' -> '09:00 to 12:15'
        text = re.sub(
            r"(\b\d{1,2}:\d{2})\s*(?:-|–|—)\s*(\d{1,2}:\d{2}\b)",
            r"\1 to \2",
            text
        )

        # 2. Convert timestamps (with optional AM/PM or 24-hour military format)
        def _conv_time(match: re.Match) -> str:
            h = int(match.group(1))
            mn = match.group(2)
            ampm_raw = match.group(3)

            if ampm_raw:
                ampm = ampm_raw.replace(".", "").strip().upper()
                if h == 0:
                    h = 12
                elif h > 12:
                    h = h - 12
                if mn == "00":
                    return f"{h} {ampm}"
                return f"{h}:{mn} {ampm}"
            else:
                # 24-hour timestamp without AM/PM
                if h == 0 and mn == "00":
                    return "midnight"
                elif h == 12 and mn == "00":
                    return "noon"
                elif h >= 12:
                    h_12 = 12 if h == 12 else (h - 12)
                    if mn == "00":
                        return f"{h_12} PM"
                    return f"{h_12}:{mn} PM"
                else:
                    h_12 = 12 if h == 0 else h
                    if mn == "00":
                        return f"{h_12} AM"
                    return f"{h_12}:{mn} AM"

        # Regex strictly captures AM/PM inside optional group so space is not eaten when AM/PM is omitted
        text = re.sub(
            r"\b0?([0-9]|1[0-9]|2[0-3]):([0-5][0-9])(?:\s*([ap]\.?m\.?))?\b",
            _conv_time,
            text,
            flags=re.IGNORECASE
        )

        # 3. Clean any remaining ':00' before AM/PM (e.g. '9:00 AM' -> '9 AM')
        text = re.sub(r"\b([1-9]|1[0-2]):00\s*(AM|PM)\b", r"\1 \2", text)

        # 4. Standalone ':00' without AM/PM (e.g. 'at 9:00') -> 'at 9 o'clock'
        text = re.sub(r"\b([1-9]|1[0-2]):00\b", r"\1 o'clock", text)

        # 5. Clean mid-sentence periods left over from a.m. / p.m. abbreviations
        text = re.sub(r"\b(AM|PM)\.\s+([a-zA-Z])", r"\1 \2", text)

        return text

    @classmethod
    def normalize_dates(cls, text: str) -> str:
        """Converts YYYY-MM-DD or DD/MM/YYYY dates into spoken English."""
        if not text:
            return ""

        # YYYY-MM-DD
        def _sub_iso_date(m: re.Match) -> str:
            y = int(m.group(1))
            m_num = int(m.group(2))
            d = int(m.group(3))
            if 1 <= m_num <= 12 and 1 <= d <= 31:
                return f"{MONTH_NAMES[m_num]} {ordinal_suffix(d)}, {y}"
            return m.group(0)

        text = re.sub(r"\b(20\d\d)-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b", _sub_iso_date, text)

        # DD/MM/YYYY or DD-MM-YYYY
        def _sub_slash_date(m: re.Match) -> str:
            d = int(m.group(1))
            m_num = int(m.group(2))
            y = int(m.group(3))
            if 1 <= m_num <= 12 and 1 <= d <= 31:
                return f"{MONTH_NAMES[m_num]} {ordinal_suffix(d)}, {y}"
            return m.group(0)

        text = re.sub(r"\b(0?[1-9]|[12]\d|3[01])[/-](0?[1-9]|1[0-2])[/-](20\d\d)\b", _sub_slash_date, text)

        return text

    @classmethod
    def normalize_currencies_and_quantities(cls, text: str) -> str:
        """Converts currency symbols, percentages, and numeric quantities."""
        if not text:
            return ""

        # Currencies: Dollars, Euros, Pounds
        text = re.sub(r"\$(\d+)\.(\d{2})\b", r"\1 dollars and \2 cents", text)
        text = re.sub(r"\$(\d+)\b", r"\1 dollars", text)
        text = re.sub(r"€(\d+)\.(\d{2})\b", r"\1 euros and \2 cents", text)
        text = re.sub(r"€(\d+)\b", r"\1 euros", text)
        text = re.sub(r"£(\d+)\.(\d{2})\b", r"\1 pounds and \2 pence", text)
        text = re.sub(r"£(\d+)\b", r"\1 pounds", text)

        # Percentages
        text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"\1 percent", text)

        # Item numbers (e.g. #14 -> number 14)
        text = re.sub(r"#(\d+)\b", r"number \1", text)

        return text

    @classmethod
    def normalize_units_and_measurements(cls, text: str) -> str:
        """Spells out temperatures, compute units, latencies, and speed."""
        if not text:
            return ""

        text = re.sub(r"(\d+(?:\.\d+)?)\s*°[Cc]\b", r"\1 degrees Celsius", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*°[Ff]\b", r"\1 degrees Fahrenheit", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*°\b", r"\1 degrees", text)

        text = re.sub(r"(\d+(?:\.\d+)?)\s*TB\b", r"\1 terabytes", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*GB\b", r"\1 gigabytes", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*MB\b", r"\1 megabytes", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*KB\b", r"\1 kilobytes", text)

        text = re.sub(r"(\d+(?:\.\d+)?)\s*GHz\b", r"\1 gigahertz", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*MHz\b", r"\1 megahertz", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*ms\b", r"\1 milliseconds", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*km/h\b", r"\1 kilometers per hour", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*mph\b", r"\1 miles per hour", text)

        return text

    @classmethod
    def normalize_room_and_course_codes(cls, text: str) -> str:
        """Converts alphanumeric codes (H311, G011, C++) into clear spoken syllables."""
        if not text:
            return ""

        # Programming language names
        text = re.sub(r"\bC\+\+(?=[^\w+]|$)", "C plus plus", text)

        # Room codes with 1 capital letter followed by 3-4 digits (e.g. H311 -> H 3 1 1)
        # Prevents Kokoro from pronouncing H311 as "H three hundred eleven"
        def _sub_room_code(m: re.Match) -> str:
            prefix = m.group(1)
            digits = " ".join(m.group(2))
            return f"{prefix} {digits}"

        text = re.sub(r"\b([A-Z])(\d{3,4})\b", _sub_room_code, text)

        return text

    @classmethod
    def normalize_abbreviations(cls, text: str) -> str:
        """Expands common abbreviations for natural dialogue."""
        if not text:
            return ""

        abbrevs = [
            (r"\bapprox\.?\b", "approximately"),
            (r"\be\.g\.,?\b", "for example"),
            (r"\bi\.e\.,?\b", "that is"),
            (r"\betc\.?\b", "etcetera"),
            (r"\bvs\.?\b", "versus"),
            (r"\bw/\b", "with"),
            (r"\bw/o\b", "without"),
            (r"\bdept\.?\b", "department"),
            (r"(\d+)\s*mins?\b", r"\1 minutes"),
            (r"(\d+)\s*secs?\b", r"\1 seconds"),
            (r"(\d+)\s*hrs?\b", r"\1 hours"),
            (r"&", "and"),
        ]
        for pattern, repl in abbrevs:
            text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

        return text

    @classmethod
    def normalize(cls, text: str) -> str:
        """Main entry point: sequentially executes all speech text normalization stages."""
        if not text:
            return ""

        text = cls.clean_markdown_and_formatting(text)
        text = cls.normalize_room_and_course_codes(text)
        text = cls.normalize_currencies_and_quantities(text)
        text = cls.normalize_units_and_measurements(text)
        text = cls.normalize_dates(text)
        text = cls.normalize_times_and_ranges(text)
        text = cls.normalize_abbreviations(text)

        # Normalize excess whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text
