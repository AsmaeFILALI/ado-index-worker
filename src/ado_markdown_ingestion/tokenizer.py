"""Explicit preparation and network-free loading of the pinned cl100k tokenizer."""

import base64
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.util import digest

ASSET_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
ASSET_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
ASSET_NAME = "cl100k_base.tiktoken"
# cl100k_base definition, verified against the resolved tiktoken distribution.
PATTERN = (
    r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+"
    r"| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
)


@dataclass(frozen=True)
class Encoder:
    encoding: tiktoken.Encoding

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))


def load_encoder(cache: Path) -> Encoder:
    try:
        data = (cache / ASSET_NAME).read_bytes()
    except OSError as exc:
        raise IngestionError("Tokenizer unavailable; run explicit prepare-tokenizer first") from exc
    if digest(data) != ASSET_SHA256:
        raise IngestionError(
            "Tokenizer integrity mismatch; prepare-tokenizer from an approved source"
        )
    ranks: dict[bytes, int] = {}
    try:
        for line in data.splitlines():
            token, rank = line.split()
            ranks[base64.b64decode(token, validate=True)] = int(rank)
    except (ValueError, TypeError) as exc:
        raise IngestionError("Invalid tokenizer artifact") from exc
    return Encoder(
        tiktoken.Encoding(
            name="cl100k_base",
            pat_str=PATTERN,
            mergeable_ranks=ranks,
            special_tokens={
                "<|endoftext|>": 100257,
                "<|fim_prefix|>": 100258,
                "<|fim_middle|>": 100259,
                "<|fim_suffix|>": 100260,
                "<|endofprompt|>": 100276,
            },
        )
    )


def prepare_tokenizer(cache: Path) -> str:
    target = cache / ASSET_NAME
    if target.exists():
        load_encoder(cache)
        return ASSET_SHA256
    temporary: str | None = None
    try:
        with urllib.request.urlopen(ASSET_URL, timeout=60) as response:
            data = response.read(10_000_001)
        if len(data) > 10_000_000 or digest(data) != ASSET_SHA256:
            raise IngestionError("Downloaded tokenizer failed integrity validation")
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
        os.replace(temporary, target)
    except OSError as exc:
        raise IngestionError("Cannot prepare tokenizer over verified HTTPS or write cache") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return ASSET_SHA256
