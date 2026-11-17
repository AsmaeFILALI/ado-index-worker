import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.errors import IngestionError

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "index-contract.json"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@dataclass
class GitRepo:
    path: Path

    def git(
        self, *args: str, check: bool = True, input_data: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=", "-c", "core.autocrlf=false", *args],
            cwd=self.path,
            capture_output=True,
            check=check,
            input=input_data,
        )

    def write(self, path: str, content: str) -> None:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))

    def commit(self, message: str = "synthetic fixture") -> str:
        self.git("add", "-A")
        self.git("commit", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def settings(self, commit: str | None = None, **overrides: object) -> Settings:
        data: dict[str, object] = {
            "source_root": str(self.path),
            "repo_id": "synthetic-repository",
            "source_ref": "refs/heads/approved",
            "commit": commit or self.git("rev-parse", "HEAD").stdout.decode().strip(),
            "repo_url": "https://dev.azure.com/example-org/example-project/_git/example-repo",
            "include": ["**/*.md"],
            "contract": str(CONTRACT),
            "tokenizer_cache": str(ROOT / ".cache" / "tiktoken"),
        }
        data.update(overrides)
        return Settings.from_mapping(data, self.path)


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    result = GitRepo(tmp_path / "source checkout")
    result.path.mkdir()
    try:
        result.git("init")
        result.git("symbolic-ref", "HEAD", "refs/heads/approved")
    except FileNotFoundError as exc:
        raise IngestionError("Git is required for synthetic source tests") from exc
    result.git("config", "user.name", "Synthetic Test")
    result.git("config", "user.email", "synthetic@example.invalid")
    result.commit()
    return result
