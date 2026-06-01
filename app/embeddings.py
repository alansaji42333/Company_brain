import logging
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

_ef = None


def _get_ef():
    global _ef
    if _ef is None:
        logger.info("Initializing ONNX embedding model: all-MiniLM-L6-v2")
        _ef = embedding_functions.DefaultEmbeddingFunction()
    return _ef


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    ef = _get_ef()
    embeddings = ef(texts)
    return [e.tolist() if hasattr(e, 'tolist') else e for e in embeddings]