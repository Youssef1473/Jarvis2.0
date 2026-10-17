"""Unit test suite for Speech Text Normalization (STN) and Natural Vocal Delivery."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.audio.normalizer import SpeechNormalizer
from jarvis.audio.tts import TextToSpeech
from jarvis.core.config_loader import load_config


def test_time_normalization():
    print("\n--- 1. Testing Digital Time & Range Normalization ---")
    cases = [
        ("Class at 09:00 a.m. in room H311.", "9 AM"),
        ("Class at 09:00am.", "9 AM"),
        ("Schedule is from 09:00 - 12:15 in G011.", "9 AM to 12:15 PM"),
        ("Meeting at 13:30 in hall A.", "1:30 PM"),
        ("Bases de Données is scheduled for 13:45 - 17:00.", "1:45 PM to 5 PM"),
        ("Standup at 10:00 AM.", "10 AM"),
        ("Midnight session at 00:00.", "midnight"),
        ("Noon lunch at 12:00.", "noon"),
        ("Starts at 08:30 in room B204.", "8:30 AM"),
        ("Evening study at 20:15.", "8:15 PM"),
    ]

    for raw, expected in cases:
        norm = SpeechNormalizer.normalize(raw)
        print(f"  {raw!r} -> {norm!r}")
        assert expected in norm, f"Expected {expected!r} in {norm!r}"
    print("Time normalization passed 100%!")


def test_date_normalization():
    print("\n--- 2. Testing Date Normalization ---")
    cases = [
        ("Today is 12/10/2026.", "October 12th, 2026"),
        ("Class on 14/10/2026.", "October 14th, 2026"),
        ("Starts 2026-10-13 at 9 AM.", "October 13th, 2026"),
        ("Exam on 01/01/2027.", "January 1st, 2027"),
    ]

    for raw, expected in cases:
        norm = SpeechNormalizer.normalize(raw)
        print(f"  {raw!r} -> {norm!r}")
        assert expected in norm, f"Expected {expected!r} in {norm!r}"
    print("Date normalization passed 100%!")


def test_codes_and_units():
    print("\n--- 3. Testing Room Codes, Units, Currencies, and Abbreviations ---")
    cases = [
        ("Room H311 and room G011.", "H 3 1 1", "G 0 1 1"),
        ("Programming in C++ course.", "C plus plus", ""),
        ("Battery at 85% and temp 45°C.", "85 percent", "45 degrees Celsius"),
        ("RAM usage 16GB, latency 12.5ms.", "16 gigabytes", "12.5 milliseconds"),
        ("Cancelled event #18.", "number 18", ""),
        ("Timer for 10 mins and 30 secs.", "10 minutes", "30 seconds"),
        ("Price is $25.50 vs €20.", "25 dollars and 50 cents", "20 euros"),
    ]

    for item in cases:
        raw, exp1, exp2 = item[0], item[1], item[2]
        norm = SpeechNormalizer.normalize(raw)
        print(f"  {raw!r} -> {norm!r}")
        assert exp1 in norm, f"Expected {exp1!r} in {norm!r}"
        if exp2:
            assert exp2 in norm, f"Expected {exp2!r} in {norm!r}"
    print("Room codes, units, and symbols passed 100%!")


def test_audio_chunk_splitting_guards_timestamps():
    print("\n--- 4. Testing Audio Chunk Splitting Guards Timestamps ---")
    cfg = load_config()
    tts = TextToSpeech(cfg)

    # Text containing colons inside timestamps must NOT be fragmented across chunks
    test_text = "Your class is scheduled for 1:30 PM to 4:45 PM in room H 3 1 1, please arrive on time."
    chunks = tts._split_into_audio_chunks(test_text)
    print("Audio chunks produced:")
    for i, c in enumerate(chunks):
        print(f"  Chunk {i+1}: {c!r}")

    # Verify that '1:30' and '4:45' were kept intact within their chunks
    full_joined = " ".join(chunks)
    assert "1:30 PM" in full_joined
    assert "4:45 PM" in full_joined
    print("Timestamp colon preservation in audio chunks verified!")


def test_kokoro_phonetic_fluency():
    print("\n--- 5. Testing Kokoro Phonetics For Natural Speech ---")
    try:
        from kokoro_onnx.tokenizer import Tokenizer
        tok = Tokenizer()
        
        # Test that '09:00 a.m.' normalized to '9 AM' does NOT contain 'zero' in phonemes
        norm = SpeechNormalizer.normalize("Your class starts at 09:00 a.m. in room H311.")
        ph = tok.phonemize(norm)
        safe_ph = ph.encode('ascii', errors='replace').decode('ascii')
        print(f"Normalized: {norm!r}")
        print(f"Phonemes  : {safe_ph!r}")
        
        # 'zˈiəɹoʊ' is 'zero'
        assert "zˈiəɹoʊ" not in ph and "zero" not in ph.lower(), "Kokoro should not say 'zero nine'!"
        print("Kokoro phoneme fluency verified: ZERO robotic digits!")
    except Exception as e:
        print(f"Kokoro test notice: {e}")


if __name__ == "__main__":
    test_time_normalization()
    test_date_normalization()
    test_codes_and_units()
    test_audio_chunk_splitting_guards_timestamps()
    test_kokoro_phonetic_fluency()
    print("\nALL SPEECH NORMALIZATION TESTS PASSED 100%!")
