import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from conftest import ROOT

from ado_markdown_ingestion.config import load_settings
from ado_markdown_ingestion.contract import Contract, validate_schema
from ado_markdown_ingestion.publishing import PublishSettings


def load_yaml(relative: str) -> dict[str, object]:
    path = ROOT / relative
    with path.open(encoding="utf-8") as stream:
        value: object = yaml.safe_load(stream)
    assert isinstance(value, dict)
    return dict(value)


def test_worker_ci_is_offline_and_covers_both_hosts() -> None:
    ci = load_yaml("pipelines/ci.yml")
    text = (ROOT / "pipelines" / "ci.yml").read_text(encoding="utf-8")
    assert "windows-latest" in text and "ubuntu-latest" in text
    for command in (
        "ruff check .",
        "ruff format --check .",
        "mypy src tests",
        "pytest",
        "uv build",
    ):
        assert command in text
    assert ci["jobs"]
    assert all(
        value not in text for value in ("AzureCLI@", "System.AccessToken", "DownloadSecureFile@")
    )


@pytest.mark.parametrize(
    "relative",
    [
        "pipelines/ci.yml",
        "pipelines/templates/setup-worker.yml",
        "pipelines/templates/ingest-job.yml",
        "examples/content-repo/azure-pipelines.yml",
    ],
)
def test_pipeline_documents_are_valid_yaml(relative: str) -> None:
    assert load_yaml(relative)


def test_template_uses_explicit_checkouts_and_protected_identity() -> None:
    text = (ROOT / "pipelines" / "templates" / "ingest-job.yml").read_text(encoding="utf-8")
    for required in (
        "path: s/content",
        "path: s/worker",
        "fetchDepth: 0",
        "persistCredentials: false",
        "deployment:",
        "environment:",
        "download: none",
        "AzureCLI@2",
        "azureSubscription:",
        "addSpnToEnvironment: true",
        "visibleAzLogin: false",
        "failOnStandardError: false",
        "$(System.AccessToken)",
        "INGEST_WORKER_COMMIT:",
        "INGEST_RESOLVED_WORKER_COMMIT:",
        "INGEST_ADMISSION_SHA256:",
    ):
        assert required in text
    assert "PullRequest" in text and "Build.SourceBranch" in text
    assert "--documents" not in text
    assert "allow_history_reset" not in text and "allow_bootstrap_existing" not in text


def test_consuming_example_pins_worker_and_owns_schedule() -> None:
    example = load_yaml("examples/content-repo/azure-pipelines.yml")
    text = (ROOT / "examples" / "content-repo" / "azure-pipelines.yml").read_text(encoding="utf-8")
    schedule = example["schedules"]
    assert isinstance(schedule, list) and schedule[0]["always"] is True
    assert "reconcile" in text and "Schedule" in text
    assert "PullRequest" in text and "enablePublishing" in text
    assert "<REVIEWED_40_CHARACTER_WORKER_COMMIT>" in text
    assert "refs/heads/main" not in text.split("resources:", 1)[1].split("stages:", 1)[0]
    assert "ingest-job.yml@ingestionWorker" in text
    assert text.count("sourceConfig: ${{ parameters.sourceConfig }}") == 2
    assert "default: config/<NAME>/pipeline-source.toml" in text
    template = (ROOT / "pipelines" / "templates" / "ingest-job.yml").read_text(encoding="utf-8")
    assert "schedules:" not in template


def test_setup_pins_toolchain_and_prepares_tokenizer_explicitly() -> None:
    text = (ROOT / "pipelines" / "templates" / "setup-worker.yml").read_text(encoding="utf-8")
    assert "versionSpec: '3.12'" in text
    assert "uv==0.7.19" in text
    assert "uv sync --locked" in text and "prepare-tokenizer" in text
    assert "System.AccessToken" not in text


def test_offline_template_branch_has_no_protected_resources_or_credentials() -> None:
    template = load_yaml("pipelines/templates/ingest-job.yml")
    jobs = template["jobs"]
    assert isinstance(jobs, list)
    offline_branch = jobs[0]
    assert isinstance(offline_branch, dict)
    assert "eq(parameters.operation, 'plan')" in next(iter(offline_branch))
    serialized = yaml.safe_dump(offline_branch)
    for protected in (
        "AzureCLI@",
        "System.AccessToken",
        "DownloadSecureFile@",
        "group:",
        "environment:",
    ):
        assert protected not in serialized
    content = load_yaml("examples/content-repo/azure-pipelines.yml")
    parameters = content["parameters"]
    assert isinstance(parameters, list)
    enabled = next(item for item in parameters if item["name"] == "enablePublishing")
    assert enabled["default"] is False
    stages = content["stages"]
    assert isinstance(stages, list) and len(stages) == 2
    assert "PullRequest" in next(iter(stages[1]))


@pytest.mark.parametrize("name", ["index-schema.example.json", "index-schema.minimal.example.json"])
def test_example_schema_matches_the_consumer_contract_and_stays_generic(name: str) -> None:
    raw = (ROOT / "examples" / name).read_text(encoding="utf-8")
    contract = Contract.load(ROOT / "docs" / "index-contract.json")
    validate_schema(
        json.loads(raw),
        contract,
        embedding_resource="https://<OPENAI_RESOURCE>.openai.azure.com",
    )
    assert "<INDEX_NAME>" in raw and ".search.windows.net" not in raw


def _guide_examples() -> dict[str, str]:
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    pattern = r"```toml\n# Example \(fictional values\): (config/contoso/[\w.-]+)\n(.*?)```"
    return {name: body for name, body in re.findall(pattern, text, re.DOTALL)}


def _toml_keys(text: str) -> set[str]:
    return set(tomllib.loads(text))


def test_deployment_guide_example_configs_are_valid_and_match_the_templates(
    tmp_path: Path,
) -> None:
    examples = _guide_examples()
    assert set(examples) == {
        "config/contoso/pipeline-source.toml",
        "config/contoso/publishing-config.toml",
    }
    folder = tmp_path / "config" / "contoso"
    folder.mkdir(parents=True)

    source = folder / "pipeline-source.toml"
    source.write_text(examples["config/contoso/pipeline-source.toml"], encoding="utf-8")
    settings = load_settings(source, {"source_root": str(tmp_path), "commit": "a" * 40}, {})
    assert settings.repo_url.endswith("/_git/handbook") and settings.include == ("docs/**/*.md",)
    # The guide tells customers to add one `../` because the file sits in config/<name>/.
    relative = tomllib.loads(source.read_text(encoding="utf-8"))["contract"]
    assert (
        ROOT / "config" / "contoso" / relative
    ).resolve() == ROOT / "docs" / "index-contract.json"

    publishing = folder / "publishing-config.toml"
    publishing.write_text(examples["config/contoso/publishing-config.toml"], encoding="utf-8")
    target = PublishSettings.load(publishing)
    assert target.admission_waiver and target.auth_mode == "pipeline"

    for name, template in (
        ("config/contoso/pipeline-source.toml", "pipeline-source.toml"),
        ("config/contoso/publishing-config.toml", "publishing-config.toml"),
    ):
        shipped = (ROOT / "examples" / template).read_text(encoding="utf-8")
        assert _toml_keys(examples[name]) == _toml_keys(shipped), name


def test_guide_covers_only_the_poc_waiver_and_example_pipeline_needs_no_secure_files() -> None:
    guide = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    prose = re.sub(r"\]\([^)]*\)", "]", guide)  # ignore link targets such as operations anchors
    for removed in (
        "scope admission",
        "per-commit",
        "policy_version",
        "admission_sha256",
        "selection_fingerprint",
        "AdmissionSha256",
        "admission.json",
    ):
        assert removed not in prose, removed
    assert "admission_waiver" in prose

    example = load_yaml("examples/content-repo/azure-pipelines.yml")
    names = {item["name"] for item in example["parameters"]}  # type: ignore[attr-defined]
    assert not {"schemaSecureFile", "admissionSecureFile"} & names
    text = (ROOT / "examples" / "content-repo" / "azure-pipelines.yml").read_text(encoding="utf-8")
    active = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("SecureFile" in line for line in active)
    assert not any("approvalVariableGroup" in line for line in active)


def test_minimal_example_schema_has_only_contract_fields_and_no_preview_properties() -> None:
    raw = (ROOT / "examples" / "index-schema.minimal.example.json").read_text(encoding="utf-8")
    contract = Contract.load(ROOT / "docs" / "index-contract.json")
    names = {field["name"] for field in json.loads(raw)["fields"]}
    assert names == set(contract.fields) | {"category"}
    assert "permissionFilter" not in raw


def test_local_toolchain_never_touches_the_package_index_and_is_opt_in() -> None:
    setup = load_yaml("pipelines/templates/setup-worker.yml")
    parameters = {item["name"]: item for item in setup["parameters"]}  # type: ignore[attr-defined]
    assert parameters["toolchain"]["default"] == "download"
    assert parameters["toolchain"]["values"] == ["download", "local"]
    steps = setup["steps"]
    assert isinstance(steps, list)
    local = next(item for item in steps if "eq(parameters.toolchain, 'local')" in next(iter(item)))
    script = yaml.safe_dump(local)
    for required in (
        "INGEST_WHEEL_DIR",
        "--no-index",
        "--require-hashes",
        "--locked",
        "UV_NO_SYNC",
    ):
        assert required in script
    assert "pip install --disable-pip-version-check" not in script
    assert "uv sync" not in script and "UsePythonVersion" not in script
    template = (ROOT / "pipelines" / "templates" / "ingest-job.yml").read_text(encoding="utf-8")
    assert template.count("toolchain: ${{ parameters.toolchain }}") == 2
    assert template.count("UV_NO_SYNC") == 4
