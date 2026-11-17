"""Pure consumer/document/schema validation; never calls an Azure service."""

import math
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from ado_markdown_ingestion.config import COMMIT
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.source import citation_url
from ado_markdown_ingestion.util import digest, object_dict, parse_json

PUBLICATION_FIELDS = frozenset({"contentVector", "ingested_at", "ingest_run_id"})
REQUIRED_NAMES = frozenset(
    {
        "id",
        "parent_id",
        "title",
        "content",
        "contentVector",
        "url",
        "filepath",
        "source",
        "repo_id",
        "source_ref",
        "source_path",
        "heading_path",
        "section_id",
        "chunk_id",
        "part",
        "part_count",
        "start_line",
        "end_line",
        "source_version_id",
        "content_checksum_sha256",
        "ingested_at",
        "ingest_run_id",
        "pipeline_version",
    }
)


def validate_vector(value: object) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 3072
        or any(
            type(number) not in (int, float)
            or abs(number) > 3.4028234663852886e38
            or not math.isfinite(number)
            for number in value
        )
        or not any(value)
    ):
        raise IngestionError("contentVector must contain 3072 finite, nonzero Single values")
    converted = [float(number) for number in value]
    if not any(struct.unpack("<3072f", struct.pack("<3072f", *converted))):
        raise IngestionError("Embedding vector becomes zero after Single conversion")
    return converted


def vector_checksum(value: object) -> str:
    vector = validate_vector(value)
    packed = struct.pack("<3072f", *vector)
    if not any(struct.unpack("<3072f", packed)):
        raise IngestionError("Embedding vector becomes zero after Single conversion")
    return digest(packed)


def _objects(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise IngestionError(f"{label} must be a list")
    return [object_dict(item, label) for item in value]


def _named(items: list[dict[str, object]], label: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or not name or name in result:
            raise IngestionError(f"Missing or duplicate {label} name")
        result[name] = item
    return result


@dataclass(frozen=True)
class Contract:
    data: dict[str, object]
    fields: dict[str, dict[str, object]]
    sha256: str

    @classmethod
    def load(cls, path: Path) -> "Contract":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise IngestionError("Cannot read consumer contract") from exc
        data = parse_json(raw)
        if (
            data.get("contractId") != "ado-markdown-ingestion"
            or data.get("contractVersion") != "1.0.0-draft"
            or data.get("artifactKind") != "consumer-contract-not-deployable-index"
            or data.get("platformOwnsSchema") is not True
        ):
            raise IngestionError("Expected the versioned ingestion consumer contract, not an index")
        fields = _named(_objects(data.get("requiredFields"), "requiredFields"), "consumer field")
        if set(fields) != REQUIRED_NAMES:
            raise IngestionError("Consumer contract must declare the exact 23 required field names")
        embeddings = object_dict(data.get("embeddingRequirements"), "embeddingRequirements")
        if (
            embeddings.get("model") != "text-embedding-3-large"
            or embeddings.get("dimensions") != 3072
        ):
            raise IngestionError("Unsupported consumer embedding model/dimension configuration")
        return cls(data, fields, digest(raw))


def validate_document(
    document: Mapping[str, object],
    contract: Contract,
    *,
    draft: bool = False,
    max_content_chars: int = 3800,
) -> None:
    required = REQUIRED_NAMES - PUBLICATION_FIELDS if draft else REQUIRED_NAMES
    if required - document.keys():
        raise IngestionError("Document is missing required consumer fields")
    if draft and document.keys() & PUBLICATION_FIELDS:
        raise IngestionError("Offline draft must not contain vectors or publication provenance")
    for name in required:
        value = document[name]
        kind = contract.fields[name].get("type")
        if kind == "Edm.String":
            if not isinstance(value, str) or (not value and name != "heading_path"):
                raise IngestionError(f"Document field {name} must be a valid string")
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise IngestionError(f"Document field {name} must be valid UTF-8") from exc
        elif kind == "Edm.Int32":
            if type(value) is not int or not -(2**31) <= value < 2**31:
                raise IngestionError(f"Document field {name} must be an Int32")
        elif kind == "Edm.DateTimeOffset":
            try:
                if not isinstance(value, str):
                    raise ValueError
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if timestamp.utcoffset() != timedelta(0):
                    raise ValueError
            except ValueError as exc:
                raise IngestionError("ingested_at must be a UTC DateTimeOffset") from exc
        elif kind == "Collection(Edm.Single)":
            vector_checksum(value)
        else:
            raise IngestionError("Unsupported consumer field type")
    key = document["id"]
    content = document["content"]
    revision = document["source_version_id"]
    url = document["url"]
    path = document["source_path"]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_=-]{1,1024}", key):
        raise IngestionError("Invalid Search document key")
    if not isinstance(content, str) or len(content) > max_content_chars or not content.strip():
        raise IngestionError("Canonical content violates the application character budget")
    if digest(content) != document["content_checksum_sha256"]:
        raise IngestionError("Canonical content checksum mismatch")
    if not isinstance(revision, str) or not COMMIT.fullmatch(revision):
        raise IngestionError("Document source revision must be an immutable commit")
    if document["source"] != "azure_devops" or document["filepath"] != url:
        raise IngestionError("Invalid source/citation alias")
    if not isinstance(url, str) or not isinstance(path, str):
        raise IngestionError("Invalid citation field types")
    parsed = urlsplit(url)
    base = parsed._replace(query="", fragment="").geturl()
    if parsed.fragment or citation_url(base, path, revision) != url:
        raise IngestionError("Citation must target the exact source path and commit")
    numbers: dict[str, int] = {}
    for name in ("part", "part_count", "start_line", "end_line", "chunk_id"):
        value = document[name]
        if not isinstance(value, int) or value < (0 if name == "chunk_id" else 1):
            raise IngestionError("Invalid source line/part/chunk numbering")
        numbers[name] = value
    if numbers["part"] > numbers["part_count"]:
        raise IngestionError("part exceeds part_count")
    if numbers["start_line"] > numbers["end_line"]:
        raise IngestionError("Invalid source line range")


def validate_schema(
    schema: Mapping[str, object],
    contract: Contract,
    *,
    deployment: str = "text-embedding",
    model: str = "text-embedding-3-large",
    embedding_resource: str | None = None,
) -> None:
    """Compare local REST-shaped schema JSON. This is not live-index evidence."""
    fields = _named(_objects(schema.get("fields"), "schema fields"), "schema field")
    if any(field.get("key") is True and name != "id" for name, field in fields.items()):
        raise IngestionError("Only id may be the index key")
    for name, constraints in contract.fields.items():
        if name not in fields:
            raise IngestionError(f"Required schema field missing: {name}")
        actual = fields[name]
        if "hidden" in actual:
            raise IngestionError("Supply REST field retrievability, not SDK hidden defaults")
        for prop, expected in constraints.items():
            # Only these unambiguous REST defaults are accepted without declaration.
            defaults: dict[str, object] = {"key": False, "retrievable": True}
            found = actual.get(prop, defaults.get(prop))
            if type(found) is not type(expected) or found != expected:
                raise IngestionError(f"Incompatible schema property: {name}.{prop}")
    vector = object_dict(schema.get("vectorSearch"), "vectorSearch")
    profiles = _named(_objects(vector.get("profiles"), "vector profiles"), "vector profile")
    algorithms = _named(_objects(vector.get("algorithms"), "vector algorithms"), "algorithm")
    vectorizers = _named(_objects(vector.get("vectorizers"), "vectorizers"), "vectorizer")
    profile = profiles.get("default", {})
    algorithm = algorithms.get(str(profile.get("algorithm")), {})
    parameters = object_dict(algorithm.get("hnswParameters"), "HNSW parameters")
    if algorithm.get("kind") != "hnsw" or parameters.get("metric") != "cosine":
        raise IngestionError("Vector profile requires cosine HNSW")
    vectorizer = vectorizers.get(str(profile.get("vectorizer")), {})
    params = object_dict(vectorizer.get("azureOpenAIParameters"), "query vectorizer parameters")
    resource = params.get("resourceUri")
    try:
        uri = urlsplit(resource) if isinstance(resource, str) else None
        if (
            uri is None
            or uri.scheme != "https"
            or not uri.hostname
            or uri.username is not None
            or uri.password is not None
            or uri.query
            or uri.fragment
            or uri.port not in (None, 443)
        ):
            raise ValueError
    except ValueError as exc:
        raise IngestionError("Query vectorizer requires an explicit HTTPS resource URI") from exc
    if (
        vectorizer.get("kind") != "azureOpenAI"
        or params.get("modelName") != model
        or params.get("deploymentId") != deployment
    ):
        raise IngestionError("Query/ingestion embedding model or deployment mismatch")
    if embedding_resource is not None and (
        str(params.get("resourceUri", "")).rstrip("/") != embedding_resource.rstrip("/")
    ):
        raise IngestionError("Query/ingestion embedding resource mismatch")
    semantic = object_dict(schema.get("semantic"), "semantic")
    configurations = _named(
        _objects(semantic.get("configurations"), "semantic configurations"),
        "semantic configuration",
    )
    config = configurations.get("semantic-config", {})
    priorities = object_dict(config.get("prioritizedFields"), "semantic priorities")
    if (
        semantic.get("defaultConfiguration") != "semantic-config"
        or object_dict(priorities.get("titleField"), "semantic title").get("fieldName") != "title"
        or [
            f.get("fieldName")
            for f in _objects(priorities.get("prioritizedContentFields"), "semantic content")
        ]
        != ["content"]
        or [
            f.get("fieldName")
            for f in _objects(priorities.get("prioritizedKeywordsFields"), "semantic keywords")
        ]
        != ["heading_path", "category"]
    ):
        raise IngestionError("Semantic priorities do not meet the consumer contract")
    for name in ("title", "content", "heading_path", "category"):
        if fields.get(name, {}).get("searchable") is not True or (
            fields.get(name, {}).get("retrievable", True) is not True
        ):
            raise IngestionError("Semantic fields must be searchable and retrievable")


def compare_declared(expected: object, actual: object) -> None:
    """Preserve all pinned platform declarations, not just the consumer subset."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise IngestionError("Deployed schema differs from the pinned platform definition")
        for key, value in expected.items():
            if key == "@odata.etag":
                continue
            found = actual.get(key, {"key": False, "retrievable": True}.get(key))
            compare_declared(value, found)
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            raise IngestionError("Deployed schema list differs from the pinned definition")
        if expected and all(isinstance(item, dict) and "name" in item for item in expected):
            named = _named(_objects(actual, "schema declarations"), "schema declaration")
            for item in expected:
                declaration = object_dict(item, "pinned declaration")
                compare_declared(declaration, named.get(str(declaration["name"])))
        else:
            if len(expected) != len(actual):
                raise IngestionError("Deployed schema priorities differ from the pinned definition")
            for left, right in zip(expected, actual, strict=True):
                compare_declared(left, right)
    elif type(expected) is not type(actual) or expected != actual:
        raise IngestionError("Deployed schema differs from a pinned platform property")
