"""Streaming Sentence & Clause Chunker for Jarvis 2.0.

Provides speculative chunking for real-time LLM token streams to achieve
sub-100ms Time-To-First-Token-Audio (TTFT) into the Kokoro-82M neural TTS pipeline.
Emits synthesis-ready text clauses at the first natural punctuation boundary
(,, ., !, ?, ;, :, \n) while guarding timestamps, decimals, and honorific abbreviations.
"""
import re
from typing import List, Optional

_MD_STRIP_RE = re.compile(r"[*_`#~>|]+")
_SNAKE_CASE_RE = re.compile(r"\b([a-z0-9]+)_([a-z0-9_]+)\b")
_FILE_EXT_RE = re.compile(r"\b([a-zA-Z0-9_-]+)\.(py|cpp|h|hpp|ts|tsx|js|json|sql|md)\b")


def sanitize_clause_for_ear(clause: str) -> str:
    """Strips residual visual artifacts and converts identifiers into natural spoken English."""
    if not clause:
        return ""
    # 1. Strip residual markdown symbols
    text = _MD_STRIP_RE.sub("", clause)
    # 2. Convert file extensions ('brain.py' -> 'brain dot py') so TTS doesn't slur the period
    text = _FILE_EXT_RE.sub(lambda m: f"{m.group(1)} dot {m.group(2)}", text)
    # 3. Convert snake_case ('habit_graph' -> 'habit graph')
    text = _SNAKE_CASE_RE.sub(lambda m: m.group(0).replace("_", " "), text)
    # 4. Remove leading bullets
    text = re.sub(r'^\s*[-*•]\s*', '', text)
    # 5. Collapse multiple whitespace while preserving punctuation
    return re.sub(r'[ \t]+', ' ', text).strip()


class StreamingSentenceChunker:
    """Incremental, zero-redundant-allocation token chunker with speculative clause emission.
    
    Invariants:
    1. Speculative Emission: Emits synthesis chunks at the first punctuation boundary
       (',', '.', '!', '?', ';', ':', '\n', '—') instead of waiting for full sentence completion.
    2. Numerical & Formatting Guards: Prevents fragmentation of timestamps (e.g. 12:30),
       decimals (e.g. 3.14), prices ($4.50), and thousand-delimiters (10,000).
    3. Abbreviation & Initial Guards: Guards titles (Mr., Dr., Mrs., Prof., Sr., Jr.),
       common abbreviations (vs., etc., e.g., i.e.), and acronym initials (U.S., A.I.).
    4. Minimal Allocations: Accumulates tokens with slice-based inspection.
    5. Fallback Clause Splitting: Speculatively emits at word boundaries if token buffer
       exceeds max_chars without punctuation to eliminate speech starvation.
    """

    # Common abbreviations that should not trigger sentence terminal splitting
    _ABBREVIATIONS = {
        "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.",
        "vs.", "etc.", "e.g.", "i.e.", "st.", "approx.", "u.s.", "a.i."
    }

    # Delimiters that trigger speculative clause emission
    _CLAUSE_DELIMITERS = {',', ';', ':', '—', '–'}
    _TERMINAL_DELIMITERS = {'.', '!', '?', '\n'}

    def __init__(
        self,
        min_clause_words: int = 1,
        min_clause_chars: int = 4,
        max_buffer_chars: int = 80,
    ):
        self.min_clause_words = min_clause_words
        self.min_clause_chars = min_clause_chars
        self.max_buffer_chars = max_buffer_chars
        self._buffer: str = ""

    def reset(self) -> None:
        """Instantly resets the chunker buffer (used on user barge-in / interrupt)."""
        self._buffer = ""

    @property
    def pending_text(self) -> str:
        """Returns the current un-emitted text remaining in the buffer."""
        return self._buffer

    def is_empty(self) -> bool:
        """Returns True if the internal buffer contains no text."""
        return len(self._buffer.strip()) == 0

    def feed(self, token: str) -> List[str]:
        """Ingests a token from the LLM stream and yields completed synthesis chunks."""
        if not token:
            return []

        self._buffer += token
        emitted: List[str] = []

        while True:
            chunk = self._try_extract_chunk()
            if chunk:
                clean_chunk = self._clean_phoneme_segment(chunk)
                if clean_chunk:
                    emitted.append(clean_chunk)
            else:
                break

        return emitted

    def feed_tokens(self, tokens: List[str]) -> List[str]:
        """Helper to feed an iterable of tokens sequentially."""
        chunks: List[str] = []
        for token in tokens:
            chunks.extend(self.feed(token))
        return chunks

    def flush(self) -> List[str]:
        """Flushes and yields any remaining text in the buffer when the LLM stream ends."""
        remaining = self._buffer.strip()
        self._buffer = ""
        if not remaining:
            return []

        clean_chunk = self._clean_phoneme_segment(remaining)
        return [clean_chunk] if clean_chunk else []

    def _try_extract_chunk(self) -> Optional[str]:
        """Scans the internal buffer for the earliest valid clause or sentence boundary."""
        buf = self._buffer
        n = len(buf)
        if n < self.min_clause_chars:
            return None

        # 1. Check for ellipsis '...' special case
        ellipsis_pos = buf.find("...")
        if ellipsis_pos != -1:
            end_pos = ellipsis_pos + 3
            if end_pos < n:
                if buf[end_pos].isspace() or buf[end_pos] in self._TERMINAL_DELIMITERS:
                    chunk = buf[:end_pos]
                    self._buffer = buf[end_pos:].lstrip()
                    return chunk
            else:
                # Wait for next token
                return None

        # 2. Scan character by character for punctuation boundaries
        for i, ch in enumerate(buf):
            if ch in self._TERMINAL_DELIMITERS:
                # Terminal boundary: ., !, ?, \n
                if ch == '.':
                    # Guard decimals: e.g. 3.14
                    if i > 0 and buf[i - 1].isdigit():
                        if i + 1 < n:
                            if buf[i + 1].isdigit():
                                continue
                        else:
                            # End of buffer after digit+dot: wait for next token
                            return None

                    # Guard abbreviations
                    preceding_word = self._get_preceding_word(buf, i)
                    if (preceding_word + ".").lower() in self._ABBREVIATIONS:
                        if i + 1 >= n:
                            return None
                        continue

                    # Guard single-letter initials like U.S. or J.
                    if len(preceding_word) == 1 and preceding_word.isupper():
                        if i + 1 >= n:
                            return None
                        continue

                # Require that there's at least one non-whitespace character before punctuation
                prefix = buf[: i + 1]
                words = prefix.strip().split()
                if not words:
                    continue

                # Ensure next char is whitespace or end of buffer or next punctuation
                if i + 1 < n and not buf[i + 1].isspace() and buf[i + 1] not in self._TERMINAL_DELIMITERS:
                    # mid-word dot or symbol, continue
                    continue

                # Valid terminal boundary found
                chunk = buf[: i + 1]
                self._buffer = buf[i + 1 :].lstrip()
                return chunk

            elif ch in self._CLAUSE_DELIMITERS:
                # Clause boundary: ,, ;, :, —, –
                # Guard colons between digits (timestamps e.g. 12:30)
                if ch == ':':
                    if i > 0 and buf[i - 1].isdigit():
                        if i + 1 < n:
                            if buf[i + 1].isdigit():
                                continue
                        else:
                            return None

                # Guard commas between digits (e.g. 10,000)
                if ch == ',':
                    if i > 0 and buf[i - 1].isdigit():
                        if i + 1 < n:
                            if buf[i + 1].isdigit():
                                continue
                        else:
                            return None

                prefix = buf[: i + 1]
                words = prefix.strip().split()
                if len(words) >= self.min_clause_words and len(prefix.strip()) >= self.min_clause_chars:
                    # Clause boundary satisfied
                    chunk = buf[: i + 1]
                    self._buffer = buf[i + 1 :].lstrip()
                    return chunk

        # 3. Fallback: Buffer length limit reached without punctuation (run-on clause)
        words = buf.strip().split()
        if len(buf) >= self.max_buffer_chars and len(words) >= 6:
            # Find last whitespace in buffer
            last_space = buf.rfind(' ')
            if last_space > self.min_clause_chars:
                chunk = buf[:last_space]
                self._buffer = buf[last_space:].lstrip()
                return chunk

        return None

    @staticmethod
    def _get_preceding_word(text: str, punct_index: int) -> str:
        """Finds the continuous alphabetical word immediately preceding punct_index."""
        start = punct_index - 1
        while start >= 0 and (text[start].isalnum() or text[start] == '.'):
            start -= 1
        return text[start + 1 : punct_index].strip()

    @staticmethod
    def _clean_phoneme_segment(text: str) -> str:
        """Normalizes Markdown formatting, symbols, and whitespace for Kokoro phonemizer."""
        if not text:
            return ""

        t = sanitize_clause_for_ear(text)

        # Ensure text contains at least one alphanumeric character
        if not re.search(r'[a-zA-Z0-9]', t):
            return ""

        return t
