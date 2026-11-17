import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT

TENANT = "00000000-0000-0000-0000-000000000001"
SUB = "00000000-0000-0000-0000-000000000002"
GROUP = f"/subscriptions/{SUB}/resourceGroups/synthetic-poc"
SEARCH = GROUP + "/providers/Microsoft.Search/searchServices/synthetic-search"
OPENAI = GROUP + "/providers/Microsoft.CognitiveServices/accounts/synthetic-openai"
CONTAINER = (
    GROUP + "/providers/Microsoft.Storage/storageAccounts/syntheticstate"
    "/blobServices/default/containers/ingestion-state"
)
SCRIPT = ROOT / "scripts" / "New-IngestionIdentity.ps1"


def scenario() -> dict[str, Any]:
    roles = [
        {
            "id": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/"
            f"00000000-0000-0000-0000-{number:012}",
            "roleName": name,
            "roleType": "BuiltInRole",
        }
        for number, name in enumerate(
            (
                "Search Index Data Contributor",
                "Cognitive Services OpenAI User",
                "Storage Blob Data Contributor",
            ),
            start=20,
        )
    ]
    return {
        "parameters": {
            "TenantId": TENANT,
            "SubscriptionId": SUB,
            "SearchServiceResourceId": SEARCH,
            "IndexName": "synthetic-index",
            "OpenAIResourceId": OPENAI,
            "StateContainerResourceId": CONTAINER,
        },
        "account": {
            "id": SUB,
            "tenantId": TENANT,
            "state": "Enabled",
            "environmentName": "AzureCloud",
        },
        "resources": {
            SEARCH: {"id": SEARCH, "type": "Microsoft.Search/searchServices"},
            OPENAI: {
                "id": OPENAI,
                "type": "Microsoft.CognitiveServices/accounts",
                "kind": "AIServices",
            },
            CONTAINER: {
                "id": CONTAINER,
                "type": "Microsoft.Storage/storageAccounts/blobServices/containers",
                "properties": {"publicAccess": "None"},
            },
        },
        "roles": roles,
        "apps": [],
        "principals": [],
        "assignments": [],
        "repeat": 1,
    }


def invoke(
    tmp_path: Path, data: dict[str, Any]
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    executable = shutil.which("pwsh") or shutil.which("pwsh.exe")
    assert executable is not None, "PowerShell 7 is required to validate the admin script"
    state = tmp_path / "scenario.json"
    result = tmp_path / "result.json"
    state.write_text(json.dumps(data), encoding="utf-8")
    process = subprocess.run(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "tests" / "fixtures" / "powershell" / "invoke-identity-script.ps1"),
            "-SetupScript",
            str(SCRIPT),
            "-ScenarioFile",
            str(state),
            "-ResultFile",
            str(result),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return process, json.loads(result.read_text(encoding="utf-8"))


def mutations(result: dict[str, Any]) -> list[list[str]]:
    return [
        call for call in result["calls"] if "create" in call or "update" in call or "delete" in call
    ]


def test_default_preview_and_whatif_do_not_change_any_identity_or_permissions(
    tmp_path: Path,
) -> None:
    for whatif in (False, True):
        data = scenario()
        if whatif:
            data["parameters"].update(Apply=True, WhatIf=True)
        process, result = invoke(tmp_path, data)
        assert process.returncode == 0, process.stderr
        assert result["results"][0]["mode"] == "preview"
        assert result["results"][0]["creates_secrets"] is False
        assert not mutations(result)
        assert not result["state"]["apps"] and not result["state"]["assignments"]


def test_apply_assigns_exact_narrow_scopes_and_idempotent_rerun(tmp_path: Path) -> None:
    data = scenario()
    data["parameters"]["Apply"] = True
    data["repeat"] = 2
    process, result = invoke(tmp_path, data)
    assert process.returncode == 0, process.stderr
    assert len(result["state"]["apps"]) == len(result["state"]["principals"]) == 1
    assert len(result["state"]["assignments"]) == 4
    assert {item["scope"] for item in result["state"]["assignments"]} == {
        SEARCH + "/indexes/synthetic-index",
        OPENAI,
        CONTAINER,
        SEARCH,
    }
    assert all(not item["created"] for item in result["results"][1]["assignments"])
    role = next(role for role in result["state"]["roles"] if role["roleType"] == "CustomRole")
    assert role["assignableScopes"] == [GROUP]
    assert role["permissions"][0]["actions"] == ["Microsoft.Search/searchServices/indexes/read"]
    assert role["permissions"][0]["dataActions"] == []
    assert all(item["runtime_access_verified"] is False for item in result["results"])
    calls = " ".join(" ".join(call) for call in result["calls"])
    for forbidden in (
        "create-for-rbac",
        "credential reset",
        "permission add",
        "account set",
        "login",
    ):
        assert forbidden not in calls
    assert "Search Service Contributor" not in calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenantId", "00000000-0000-0000-0000-000000000009"),
        ("id", "00000000-0000-0000-0000-000000000009"),
        ("state", "Disabled"),
        ("environmentName", "AzureUSGovernment"),
    ],
)
def test_wrong_or_disabled_context_fails_before_changes(
    tmp_path: Path, field: str, value: str
) -> None:
    data = scenario()
    data["parameters"]["Apply"] = True
    data["account"][field] = value
    process, result = invoke(tmp_path, data)
    assert process.returncode != 0
    assert not mutations(result)


def test_cross_subscription_resource_id_is_rejected_without_cli_calls(tmp_path: Path) -> None:
    data = scenario()
    data["parameters"]["Apply"] = True
    data["parameters"]["OpenAIResourceId"] = OPENAI.replace(SUB, TENANT)
    process, result = invoke(tmp_path, data)
    assert process.returncode != 0
    assert not result["calls"]


def test_public_container_and_missing_builtin_role_block_changes(tmp_path: Path) -> None:
    for public in (True, False):
        data = scenario()
        data["parameters"]["Apply"] = True
        if public:
            data["resources"][CONTAINER]["properties"]["publicAccess"] = "Blob"
        else:
            data["roles"].pop()
        process, result = invoke(tmp_path, data)
        assert process.returncode != 0
        assert not mutations(result)


def test_ambiguous_names_and_multitenant_apps_are_never_reused_silently(tmp_path: Path) -> None:
    app = {
        "displayName": "ado-markdown-ingestion",
        "appId": TENANT,
        "id": SUB,
        "signInAudience": "AzureADMyOrg",
    }
    for ambiguous in (True, False):
        data = scenario()
        data["parameters"]["Apply"] = True
        data["apps"] = (
            [app, deepcopy(app)]
            if ambiguous
            else [{**app, "signInAudience": "AzureADMultipleOrgs"}]
        )
        process, result = invoke(tmp_path, data)
        assert process.returncode != 0
        assert not mutations(result)


def test_cli_failure_is_nonzero_and_preserves_partial_setup_for_rerun(tmp_path: Path) -> None:
    data = scenario()
    data["parameters"]["Apply"] = True
    data["fail_operation"] = "role assignment create"
    process, result = invoke(tmp_path, data)
    assert process.returncode != 0
    assert len(result["state"]["apps"]) == len(result["state"]["principals"]) == 1
    assert not result["state"]["assignments"]
    assert not result["results"]
    assert not any("delete" in call for call in result["calls"])


def test_invalid_cli_json_is_not_a_successful_empty_lookup(tmp_path: Path) -> None:
    data = scenario()
    data["invalid_json"] = True
    process, result = invoke(tmp_path, data)
    assert process.returncode != 0
    assert "invalid JSON" in process.stderr
    assert not mutations(result)


def schema_role() -> dict[str, Any]:
    return {
        "id": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/"
        "00000000-0000-0000-0000-000000000099",
        "roleName": "ADO Markdown Index Schema Reader",
        "roleType": "CustomRole",
        "assignableScopes": [GROUP],
        "permissions": [
            {
                "actions": ["Microsoft.Search/searchServices/indexes/read"],
                "notActions": [],
                "dataActions": [],
                "notDataActions": [],
            }
        ],
    }


@pytest.mark.parametrize("difference", ["permissions", "assignable-scope"])
def test_existing_custom_role_is_not_broadened_or_replaced(tmp_path: Path, difference: str) -> None:
    data = scenario()
    data["parameters"]["Apply"] = True
    role = schema_role()
    if difference == "permissions":
        role["permissions"][0]["actions"] = ["*"]
    else:
        role["assignableScopes"] = [f"/subscriptions/{SUB}"]
    data["roles"].append(role)
    process, result = invoke(tmp_path, data)
    assert process.returncode != 0
    assert not mutations(result)


def test_explicit_client_id_selects_existing_app_without_creating_a_duplicate(
    tmp_path: Path,
) -> None:
    data = scenario()
    app = {
        "displayName": "ado-markdown-ingestion",
        "appId": "00000000-0000-0000-0000-000000000010",
        "id": "00000000-0000-0000-0000-000000000011",
        "signInAudience": "AzureADMyOrg",
    }
    data["apps"] = [app, {**app, "appId": "00000000-0000-0000-0000-000000000019"}]
    data["parameters"].update(Apply=True, ExistingAppId=app["appId"])
    process, result = invoke(tmp_path, data)
    assert process.returncode == 0, process.stderr
    assert not any(call[:3] == ["ad", "app", "create"] for call in result["calls"])
    assert result["results"][0]["client_id"] == app["appId"]


def test_existing_conditional_assignment_is_not_silently_bypassed(tmp_path: Path) -> None:
    data = scenario()
    data["parameters"]["Apply"] = True
    app = {
        "displayName": "ado-markdown-ingestion",
        "appId": "00000000-0000-0000-0000-000000000010",
        "id": "00000000-0000-0000-0000-000000000011",
        "signInAudience": "AzureADMyOrg",
    }
    principal = {
        "appId": app["appId"],
        "id": "00000000-0000-0000-0000-000000000012",
        "appOwnerOrganizationId": TENANT,
        "servicePrincipalType": "Application",
    }
    data["apps"], data["principals"] = [app], [principal]
    data["roles"].append(schema_role())
    data["assignments"] = [
        {
            "principalId": principal["id"],
            "roleDefinitionId": data["roles"][0]["id"],
            "scope": SEARCH + "/indexes/synthetic-index",
            "condition": "synthetic restricted assignment",
        }
    ]
    process, result = invoke(tmp_path, data)
    assert process.returncode != 0
    assert not mutations(result)
