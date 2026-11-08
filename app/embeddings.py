import logging
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

logger = logging.getLogger(__name__)

_ef = None


def _get_ef():
    global _ef
    if _ef is None:
        logger.info("Initializing ONNX embedding model: all-MiniLM-L6-v2")
        _ef = ONNXMiniLM_L6_V2()
    return _ef


def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    ef = _get_ef()
    embeddings = ef(texts)
    return [e.tolist() if hasattr(e, 'tolist') else e for e in embeddings]