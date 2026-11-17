import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from ado_markdown_ingestion.errors import IngestionError


def digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value: object) -> str:
    return digest(canonical_json(value))


def object_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise IngestionError(f"{label} must be an object with string keys")
    return dict(value)


def parse_json(raw: bytes) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise IngestionError("JSON artifact contains a non-finite numeric constant")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise IngestionError("JSON artifact contains duplicate keys")
            result[key] = value
        return result

    try:
        value: object = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=reject_constant
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IngestionError("Cannot read a valid JSON artifact") from exc
    return object_dict(value, "JSON artifact")


def read_json(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IngestionError("Cannot read the JSON artifact") from exc
    return parse_json(raw)


def pinned_json(path: Path, expected_sha256: str | None) -> dict[str, object]:
    if expected_sha256 is None or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise IngestionError("A trusted external SHA-256 reference is required for this artifact")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IngestionError("Cannot read the pinned local artifact") from exc
    if digest(raw) != expected_sha256:
        raise IngestionError("Pinned artifact integrity mismatch")
    return parse_json(raw)


def string_value(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IngestionError(f"Setting {key} must be a nonempty string")
    return value


def integer_value(data: Mapping[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    if type(value) is not int:
        raise IngestionError(f"Setting {key} must be an integer")
    return value
