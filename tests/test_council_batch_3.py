"""Verification test suite for Council Batch 3:
1. Dynamic Model Latency Telemetry (Brain & UI Server)
2. Semantic Vector Memory Engine & Context Biasing (MemoryManager & Productivity Skills)
3. Progressive Chunked TTS Streaming (TextToSpeech)
"""
import sys
from pathlib import Path
import time
import os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager, VectorMemoryEngine
from jarvis.skills.productivity import recall_fact, semantic_search_memory, set_memory_manager
from jarvis.audio.tts import TextToSpeech
from jarvis.core.brain import JarvisBrain
from jarvis.core.safety import SafetyGate
from jarvis.ui.server import JarvisUIService


def test_vector_memory_engine():
    print("\n--- Testing VectorMemoryEngine ---")
    engine = VectorMemoryEngine()
    
    # 1. Test tokenization & synonyms
    tokens = engine.tokenize("What is my favorite code editor?")
    assert "editor" in tokens
    assert "ide" in tokens, "Expected 'ide' synonym in tokens"
    assert "vscode" in tokens, "Expected 'vscode' synonym in tokens"
    print(f"Tokenization & Synonym Expansion: PASSED ({len(tokens)} tokens generated)")

    # 2. Test TF-IDF vectorization and cosine similarity
    corpus = [
        "favorite_editor VS Code IDE for Python",
        "car Tesla Model 3 electric vehicle",
        "favorite_drink black coffee without sugar"
    ]
    idf = engine.compute_idf(corpus)
    q_vec = engine.vectorize("What IDE or programming editor do I like?", idf)
    d1_vec = engine.vectorize(corpus[0], idf)
    d2_vec = engine.vectorize(corpus[1], idf)
    
    sim1 = engine.cosine_similarity(q_vec, d1_vec)
    sim2 = engine.cosine_similarity(q_vec, d2_vec)
    print(f"Cosine similarity - Target doc (editor): {sim1:.4f}, Unrelated doc (car): {sim2:.4f}")
    assert sim1 > 0.25, f"Expected high similarity for target doc, got {sim1}"
    assert sim1 > sim2, "Target document must score higher than unrelated document"
    print("VectorMemoryEngine Math & Similarity: PASSED")


def test_semantic_memory_integration():
    print("\n--- Testing Semantic Memory Integration in MemoryManager ---")
    cfg = load_config()
    test_db = Path("data/test_semantic_memory.db")
    if test_db.exists():
        try:
            os.remove(test_db)
        except Exception:
            pass

    cfg["memory"]["db_path"] = "data/test_semantic_memory.db"
    mem = MemoryManager(cfg)
    set_memory_manager(mem)

    # Populate facts
    mem.remember_fact("favorite_editor", "Visual Studio Code with Vim bindings", "development")
    mem.remember_fact("daily_vehicle", "Matte black Tesla Model 3", "personal")
    mem.remember_fact("primary_drink", "Single-origin pour-over Ethiopian coffee", "habits")

    # 1. Test semantic_search_facts
    results = mem.semantic_search_facts("Which IDE and coding software do I prefer?")
    print(f"Semantic search returned {len(results)} matches:")
    for r in results:
        print(f"  • {r['key']}: {r['value']} (Score: {r['relevance_score']:.1%})")
    assert len(results) > 0, "Expected at least one semantic match"
    assert results[0]["key"] == "favorite_editor"
    assert results[0]["relevance_score"] >= 0.20

    # 2. Test recall_fact skill with fuzzy semantic query
    recalled = recall_fact("what code editor do I use")
    print(f"Skill recall_fact output:\n{recalled}")
    assert "Visual Studio Code" in recalled
    assert "memory recall" in recalled.lower()

    # 3. Test semantic_search_memory skill
    skill_output = semantic_search_memory("tell me about my car or vehicle")
    print(f"Skill semantic_search_memory output:\n{skill_output}")
    assert "Tesla Model 3" in skill_output

    # 4. Test context summary semantic biasing
    biased_summary = mem.get_context_summary(query="programming IDE")
    print(f"Biased Context Summary:\n{biased_summary}")
    assert "[Directly Relevant Knowledge to Current Context]" in biased_summary
    assert "Visual Studio Code" in biased_summary

    # Cleanup
    set_memory_manager(None)
    if test_db.exists():
        try:
            os.remove(test_db)
        except Exception:
            pass
    print("Semantic Memory Integration: PASSED")


def test_chunked_tts_streaming():
    print("\n--- Testing Progressive Chunked TTS Streaming ---")
    cfg = load_config()
    tts = TextToSpeech(cfg)

    # 1. Test clause splitting helper
    long_sentence = (
        "All diagnostic systems are functioning nominally, Sir, and I have completed the security audit. "
        "Furthermore, our memory indices have been upgraded to semantic vector recall."
    )
    chunks = tts._split_into_audio_chunks(long_sentence)
    print(f"Split long sentence into {len(chunks)} chunks:")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}] {c}")
    assert len(chunks) >= 2, f"Expected sentence to split into multiple chunks, got {len(chunks)}"

    # Test short sentence (should not split)
    short_sentence = "Yes, Sir."
    short_chunks = tts._split_into_audio_chunks(short_sentence)
    assert len(short_chunks) == 1
    assert short_chunks[0] == "Yes, Sir."

    # 2. Test TTS double-buffered streaming session with chunked synthesis
    print("Testing streaming session start & finish...")
    tts.start_streaming_session()
    tts.queue_sentence("Diagnostics are fully verified, Sir.")
    finished = tts.finish_streaming_session()
    assert finished is True
    assert tts.is_speaking is False
    print("Chunked TTS Streaming: PASSED")


def test_dynamic_latency_telemetry():
    print("\n--- Testing Dynamic Model Latency Telemetry ---")
    cfg = load_config()
    brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))
    
    # 1. Verify latency getter initialized
    latencies = brain.get_latencies()
    print(f"Initial Brain Latencies: {latencies}")
    assert isinstance(latencies, dict)
    assert "ollama" in latencies
    assert "groq" in latencies
    assert "gemini" in latencies

    # 2. Update latencies and verify getter isolation
    brain.last_latencies["ollama"] = 42.5
    brain.last_latencies["groq"] = 188.2
    assert brain.get_latencies()["ollama"] == 42.5
    assert brain.get_latencies()["groq"] == 188.2

    # 3. Test UIService telemetry aggregation
    class MockOrchestrator:
        def __init__(self, b):
            self.brain = b
        def is_muted(self):
            return False

    mock_orch = MockOrchestrator(brain)
    ui_service = JarvisUIService(orchestrator=mock_orch)
    telemetry = ui_service._gather_telemetry()
    print(f"UIService Telemetry Latencies: {telemetry.get('latencies')}")
    assert "latencies" in telemetry
    assert telemetry["latencies"]["ollama"] == 42.5
    assert telemetry["latencies"]["groq"] == 188.2
    print("Dynamic Model Latency Telemetry: PASSED")


if __name__ == "__main__":
    test_vector_memory_engine()
    test_semantic_memory_integration()
    test_chunked_tts_streaming()
    test_dynamic_latency_telemetry()
    print("\n========================================================")
    print("ALL COUNCIL BATCH 3 VERIFICATION SUITE TESTS PASSED 100%")
    print("========================================================\n")
