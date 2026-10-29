import os
import math
import threading
from typing import List, Optional
from loguru import logger
from app.core.config import get_settings

_model_cache = {}
_model_init_lock = threading.Lock()

def get_embedding_model():
    """
    Loads and caches the open-source SentenceTransformer multilingual embedding model.
    Thread-safe singleton initialization guarantees the model is loaded exactly once
    per process, preventing concurrent load race conditions and Windows memory access violations.
    """
    settings = get_settings()
    model_name = getattr(settings, "embedding_model", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    
    # Fast path: already initialized and cached
    if model_name in _model_cache:
        return _model_cache[model_name]

    with _model_init_lock:
        # Double check inside the lock
        if model_name in _model_cache:
            return _model_cache[model_name]

        # Prevent Transformers from using multi-threaded tensor slicing during weight materialization,
        # which causes Windows fatal exception access violations in PyTorch C++ storage allocation.
        try:
            import transformers.core_model_loading as cml
            cml.GLOBAL_WORKERS = 1
        except Exception:
            pass

        # Patch safetensors.safe_open to use 'pread' instead of Windows 'mmap',
        # preventing Windows STATUS_ACCESS_VIOLATION and os error 1455 when system commit charge is constrained.
        try:
            import safetensors
            _orig_safe_open = safetensors.safe_open
            def _safe_open_pread(filename, framework="pt", device="cpu", backend="pread"):
                if backend == "mmap":
                    backend = "pread"
                return _orig_safe_open(filename, framework=framework, device=device, backend=backend)
            safetensors.safe_open = _safe_open_pread
            import transformers.modeling_utils
            transformers.modeling_utils.safe_open = _safe_open_pread
        except Exception:
            pass

        try:
            from sentence_transformers import SentenceTransformer
        except Exception as _st_import_err:
            SentenceTransformer = None

        if SentenceTransformer is None:
            logger.warning(f"SentenceTransformer not available. Fallback vector generator will be used.")
            _model_cache[model_name] = None
            return None


        try:
            logger.info(f"Loading open-source multilingual embedding model: {model_name}")
            model = SentenceTransformer(model_name)
            _model_cache[model_name] = model
        except (Exception, MemoryError) as e:
            logger.warning(f"Could not load SentenceTransformer model ({model_name}): {e}. Fallback vector generator will be used.")
            _model_cache[model_name] = None

    return _model_cache[model_name]


def generate_fallback_embedding(text: str, dim: int = 384) -> List[float]:
    """Generates a deterministic normalized dense embedding vector for fallback."""
    if not text:
        return [0.0] * dim
    vals = [0.01] * dim
    clean_words = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower()).split()
    for idx, word in enumerate(clean_words):
        for c_idx, char in enumerate(word):
            pos = (idx * 7 + c_idx * 13 + ord(char)) % dim
            vals[pos] += (ord(char) * 0.005) + ((c_idx + 1) * 0.002)
    
    magnitude = math.sqrt(sum(v * v for v in vals))
    if magnitude > 0:
        return [round(v / magnitude, 6) for v in vals]
    return [0.0] * dim


def generate_text_embedding(text: str) -> List[float]:
    """
    Generates embedding vector for a given text string using the configured
    open-source multilingual Sentence Transformers model.
    """
    if not text or not text.strip():
        return generate_fallback_embedding("", 384)
    
    cleaned = text.replace("\n", " ").strip()[:8000]
    model = get_embedding_model()
    if model is not None:
        try:
            vec = model.encode(cleaned, convert_to_numpy=True)
            return [float(x) for x in vec]
        except Exception as e:
            logger.error(f"Error producing SentenceTransformer embedding: {e}")
    
    return generate_fallback_embedding(cleaned, 384)
