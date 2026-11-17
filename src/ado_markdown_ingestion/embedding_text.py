"""Deterministic text sent to the embedding model; the indexed `content` stays unchanged."""

import re

# Bump when the derivation changes; it is part of the processing fingerprint.
EMBEDDING_TEXT_VERSION = 1
_DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{64,}")


def embedding_text(content: str) -> str:
    """Drop inline base64 payloads, which carry no meaning for retrieval and fill the budget."""
    return _DATA_URI.sub("data:[omitted]", content)
