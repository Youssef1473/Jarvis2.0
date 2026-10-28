"""Vector search, tokenization, TF-IDF, and RRF rank fusion algorithms for Jarvis 2.0."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_FTS_STOPWORDS = {
    'what', 'whats', 'who', 'when', 'where', 'why', 'how', 'tell', 'me', 'about',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'we', 'you', 'i', 'he', 'she', 'they', 'it', 'did', 'do', 'does', 'our',
    'my', 'your', 'their', 'his', 'her', 'its', 'that', 'this', 'these', 'those',
    'can', 'could', 'would', 'should', 'will', 'shall', 'may', 'might', 'must',
    'say', 'said', 'saying', 'talk', 'talked', 'talking', 'ask', 'asked', 'asking',
    'please', 'recall', 'remember', 'past', 'conversation', 'previous', 'discussed',
    'regarding', 'concerning', 'related',
    'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'as', 'into',
    'and', 'or', 'not', 'but', 'if', 'then', 'so', 'have', 'has', 'had'
}


def sanitize_fts5_query(query: str) -> str | None:
    """Sanitizes raw user query for safe SQLite FTS5 search.
    Removes punctuation/special FTS operators, filters conversational stopwords,
    and returns OR-delimited prefix wildcard terms, e.g. '"weather"* OR "tomorrow"*'.
    Returns None if no searchable tokens remain."""
    if not query or not isinstance(query, str):
        return None
    cleaned = re.sub(r"[^\w\s]", " ", query.lower())
    words = [w for w in cleaned.split() if len(w) > 1]
    filtered = [w for w in words if w not in _FTS_STOPWORDS]
    tokens = filtered if filtered else words
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens)


_MEMORY_TAINT_REGEX = re.compile(
    r"(?i)(ignore\s+previous|system\s*:|update_user_preference|always\s+run\s+|override\s+instructions)"
)


def sanitize_memory_taint(text: str) -> str:
    """Strips raw untrusted ARIA/DOM prompt-injection patterns before persisting to memory."""
    if not text:
        return ""
    cleaned = _MEMORY_TAINT_REGEX.sub("", str(text))
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def fuse_vec_and_fts_rrf(
    vec_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    k: int = 60,
    max_l2_distance: float = 0.90,
) -> list[dict[str, Any]]:
    """Fuses vector KNN and FTS5 BM25 search results via Reciprocal Rank Fusion (RRF).

    Applies calibrated L2 distance gating (max_l2_distance = 0.90, S_cos >= 0.595)
    to eliminate orthogonal noise while retaining semantic synonyms.
    """
    scores: dict[Any, float] = {}
    item_map: dict[Any, dict[str, Any]] = {}

    def _get_key(item: dict[str, Any], default_val: Any) -> Any:
        if "key" in item:
            return item["key"]
        if "id" in item:
            return item["id"]
        if "doc_id" in item:
            return item["doc_id"]
        return default_val

    # 1. RRF score from FTS5 ranking
    for rank, item in enumerate(fts_results, start=1):
        key = _get_key(item, rank)
        scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
        item_map[key] = item

    # 2. Filter vector results by L2 distance gate
    filtered_vec: list[dict[str, Any]] = []
    for item in vec_results:
        dist = item.get("distance")
        if dist is None:
            dist = item.get("l2_distance")
        if dist is not None and float(dist) > max_l2_distance:
            continue
        filtered_vec.append(item)

    # 3. RRF score from filtered Vector KNN ranking
    for rank, item in enumerate(filtered_vec, start=1):
        key = _get_key(item, rank)
        scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
        if key not in item_map:
            item_map[key] = item

    sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    results = []
    for key in sorted_keys:
        res = dict(item_map[key])
        res["rrf_score"] = round(scores[key], 4)
        results.append(res)
    return results


class VectorMemoryEngine:
    """Local vector recall engine using TF-IDF subword and token n-gram vectors with cosine similarity.
    Runs 100% locally with zero external network or model download dependencies in <1ms."""

    SEMANTIC_SYNONYMS = {
        "editor": ["ide", "vscode", "code", "programming", "sublime", "pycharm", "cursor", "neovim", "vim"],
        "ide": ["editor", "vscode", "code", "development", "programming"],
        "code": ["programming", "script", "developer", "ide", "editor", "software"],
        "favorite": ["preferred", "best", "liked", "fav", "top", "choice"],
        "preference": ["favorite", "preferred", "setting", "likes", "choice"],
        "music": ["song", "audio", "spotify", "playlist", "track", "tunes", "artist"],
        "work": ["job", "project", "career", "task", "company", "office", "business"],
        "home": ["house", "apartment", "residence", "address", "location"],
        "car": ["vehicle", "automobile", "drive", "transport"],
        "drink": ["coffee", "tea", "water", "beverage"],
        "food": ["meal", "lunch", "dinner", "breakfast", "snack"],
        "os": ["operating system", "windows", "linux", "mac", "wsl"],
        "city": ["town", "location", "country", "place", "residence"],
        "watchdog": ["sentinel", "monitor", "telemetry", "hardware", "vitals"],
        "note": ["notes", "memo", "reminder", "scratchpad", "jot"],
    }

    def tokenize(self, text: str) -> list[str]:
        """Extracts word tokens, semantic aliases, and subword n-grams."""
        if not text:
            return []
        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        words = [w for w in cleaned.split() if len(w) > 1]

        tokens = list(words)
        for w in words:
            syns = self.SEMANTIC_SYNONYMS.get(w, [])
            tokens.extend(syns)

        # Add character 3-grams and 4-grams for root matching and typo resilience
        for w in words:
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens.append(f"#{w[i:i+3]}")
            if len(w) >= 4:
                for i in range(len(w) - 3):
                    tokens.append(f"##{w[i:i+4]}")

        return tokens

    def vectorize(self, text: str, idf_weights: dict[str, float] | None = None) -> dict[str, float]:
        """Calculates L2-normalized TF-IDF vector for a given text."""
        tokens = self.tokenize(text)
        if not tokens:
            return {}
        counts = Counter(tokens)
        total = len(tokens)

        vec = {}
        for token, count in counts.items():
            tf = count / total
            idf = idf_weights.get(token, 1.0) if idf_weights else 1.0
            vec[token] = tf * idf

        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for k in vec:
                vec[k] /= norm
        return vec

    def compute_idf(self, corpus: list[str]) -> dict[str, float]:
        """Computes inverse document frequency across the corpus."""
        n_docs = len(corpus)
        if n_docs == 0:
            return {}
        doc_freq = Counter()
        for doc in corpus:
            unique_tokens = set(self.tokenize(doc))
            for tok in unique_tokens:
                doc_freq[tok] += 1

        idf = {}
        for tok, df in doc_freq.items():
            idf[tok] = math.log((1 + n_docs) / (1 + df)) + 1.0
        return idf

    @staticmethod
    def cosine_similarity(vec1: dict[str, float], vec2: dict[str, float]) -> float:
        """Computes cosine similarity between two unit-normalized sparse vectors."""
        if not vec1 or not vec2:
            return 0.0
        if len(vec1) > len(vec2):
            vec1, vec2 = vec2, vec1
        return sum(val * vec2.get(k, 0.0) for k, val in vec1.items())

    def dense_vectorize(self, text: str, dim: int | None = None, is_query: bool = False) -> list[float]:
        """Calculates deterministic L2-normalized float vector (default native 384-dim) via OnnxEmbeddingEngine."""
        from jarvis.core.memory.onnx_embeddings import (
            DEFAULT_EMBEDDING_DIM,
            OnnxEmbeddingEngine,
        )
        target_dim = dim if dim and dim > 0 else DEFAULT_EMBEDDING_DIM
        engine = OnnxEmbeddingEngine.get_instance(dim=target_dim)
        return engine.embed(text, is_query=is_query, dim=target_dim)

    def embed_query(self, query: str, dim: int | None = None) -> list[float]:
        """Calculates query vector with asymmetric search prefix."""
        return self.dense_vectorize(query, dim=dim, is_query=True)

    def embed_batch(self, texts: list[str], batch_size: int = 64, is_query: bool = False, dim: int | None = None) -> list[list[float]]:
        """Calculates batch embedding vectors using tokenizer.encode_batch()."""
        from jarvis.core.memory.onnx_embeddings import (
            DEFAULT_EMBEDDING_DIM,
            OnnxEmbeddingEngine,
        )
        target_dim = dim if dim and dim > 0 else DEFAULT_EMBEDDING_DIM
        engine = OnnxEmbeddingEngine.get_instance(dim=target_dim)
        return engine.embed_batch(texts, batch_size=batch_size, is_query=is_query, dim=target_dim)

    def get_backend_name(self) -> str:
        """Returns the active embedding backend identifier string."""
        from jarvis.core.memory.onnx_embeddings import OnnxEmbeddingEngine
        return OnnxEmbeddingEngine.get_instance().get_backend_name()
