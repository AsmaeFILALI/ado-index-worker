from pathlib import Path

import pytest

from ado_markdown_ingestion.config import Settings, load_settings
from ado_markdown_ingestion.errors import IngestionError


def values(tmp_path: Path) -> dict[str, object]:
    return {
        "source_root": str(tmp_path),
        "repo_id": "synthetic-repository",
        "source_ref": "refs/heads/approved",
        "commit": "a" * 40,
        "repo_url": "https://dev.azure.com/example-org/example-project/_git/example-repo",
        "include": ["*.md", "**/*.md"],
        "contract": str(tmp_path / "contract.json"),
    }


def test_config_precedence_and_relative_paths(tmp_path: Path) -> None:
    config = tmp_path / "worker.toml"
    config.write_text(
        'source_root = "."\nrepo_id = "synthetic-repository"\n'
        'source_ref = "refs/heads/approved"\ncommit = "' + "a" * 40 + '"\n'
        'repo_url = "https://dev.azure.com/example-org/example-project/_git/example-repo"\n'
        'include = ["*.md"]\ncontract = "contract.json"\ntarget_tokens = 400\n',
        encoding="utf-8",
    )
    settings = load_settings(config, {"target_tokens": 200}, {"ADO_INGEST_TARGET_TOKENS": "300"})
    assert settings.source_root == tmp_path
    assert settings.contract == tmp_path / "contract.json"
    assert settings.target_tokens == 200
    assert settings.dimensions == 3072
    assert settings.deployment != settings.model


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("commit", "HEAD"),
        ("source_ref", "main"),
        ("source_ref", "refs/heads/a..b"),
        ("repo_url", "https://example.com/foreign/_git/repo"),
        ("repo_url", "https://user:secret@dev.azure.com/a/b/_git/c"),
        ("repo_url", "http://dev.azure.com/a/b/_git/c"),
        ("repo_url", "https://dev.azure.com/a/b/_git/c?token=secret"),
        ("dimensions", 1536),
        ("model", "text-embedding-3-small"),
        ("target_tokens", True),
        ("max_content_chars", 0),
        ("overlap_tokens", 900),
        ("max_content_chars", 3801),
        ("target_tokens", 901),
        ("include", []),
        ("include", ["../*.md"]),
        ("unknown_setting", "ignored"),
    ],
)
def test_invalid_settings_fail(tmp_path: Path, key: str, value: object) -> None:
    data = values(tmp_path)
    data[key] = value
    with pytest.raises(IngestionError):
        Settings.from_mapping(data, tmp_path)


def test_fingerprint_is_stable_and_processing_sensitive(tmp_path: Path) -> None:
    original = values(tmp_path)
    a = Settings.from_mapping(original, tmp_path)
    b = Settings.from_mapping({**original, "source_root": str(tmp_path / "elsewhere")}, tmp_path)
    c = Settings.from_mapping({**original, "target_tokens": 200}, tmp_path)
    assert a.processing_fingerprint() == b.processing_fingerprint()
    assert a.processing_fingerprint() != c.processing_fingerprint()


def test_missing_config_and_unknown_env_fail(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="configuration"):
        load_settings(tmp_path / "missing.toml", {}, {})
    with pytest.raises(IngestionError, match="environment"):
        load_settings(None, values(tmp_path), {"ADO_INGEST_IGNORED": "bad"})
