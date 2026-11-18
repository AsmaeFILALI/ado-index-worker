# Ingestion operations and pipeline onboarding

This repository produces Search documents; it does not provision infrastructure,
change the authoritative schema, register knowledge sources or promote an index.
The [index consumer contract](index-contract.json) is the requirements baseline for the
target index. For first-time setup read the [deployment guide](deployment.md).

## 1. Local and agent setup

Use Python 3.12 and the reviewed uv version, currently 0.7.19. Prepare dependencies
from approved HTTPS sources:

```text
uv sync --locked --python 3.12 --group dev
uv run --locked ado-ingest prepare-tokenizer --cache-dir .cache/tiktoken
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src tests
uv run --locked pytest -q
uv build
```

Dependency/tokenizer preparation can use network access. The worker's `plan`
must not. uv's `--offline` controls package downloads, not Azure SDK calls:
`sync` launched through `uv run --offline` still publishes to Azure.
The tokenizer is integrity checked; an unavailable/corrupt cache fails explicitly.
Do not silently substitute tokenizers or disable TLS verification.

Use the worker CI definition at [pipelines/ci.yml](../pipelines/ci.yml). It runs
Windows/Linux checks with no production identity. Configure an Azure Repos
build-validation branch policy for PRs; YAML `pr` triggers are not that integration.
The content pipeline PR job runs only the pinned worker's original synthetic
tests. It deliberately does not pretend an unmerged PR snapshot is an approved
publishing revision; testing its real corpus requires a separately reviewed
offline source setup.

## 2. External prerequisites

For an Agent Landing Zone customer handoff, follow the delivered platform
revision's customer runbook and exporter, and map its values with
[deployment Appendix A](deployment.md#appendix-a-agent-landing-zone-handoff-optional). The nonsecret
export is a binding consistency check only: recheck private connectivity, actual roles, current
serving bindings and source admission before publication. Keep the full schema
and its independently approved hash together; do not regenerate an approval or
silently replace the worker's pinned target to accommodate a failed check.

Owners must arrange these outside the worker:

| Owner | Required setup |
| --- | --- |
| Platform | Existing full index definition with immutable revision/hash, compatible document/query embeddings and semantic settings; existing private state container; explicit serving/non-serving target designation. |
| Content | Approved immutable repository ID/ref/include/exclude rules, uniform-audience admission or an implemented permission contract, and reviewed source commits. |
| Azure DevOps | Worker repository hosting/branch policy, source/worker checkout access, protected environment, an authorized agent pool, and (unless the in-repo options in section 4 are chosen) a reviewed worker commit pin, secure files and approval variable group. |
| Identity | Federated service connection authorized only to intended publishing jobs; exact tenant/client/connection IDs and scoped resource permissions. |
| Network/data | Approved agent geography, model processing location, dependency egress and endpoint/DNS reachability. |

The worker identity needs Search document read/write, embedding inference and
private Blob state/lease access. Index-definition read permission is separately
required; do not assume document access grants it. Platform owners provide
least-privilege read-only schema access rather than schema administration or
subscription-wide Contributor. Repository Build Service permissions are separate
from Azure role assignments. Preserve project-scoped authorization and explicitly
grant the build identity access to each intended repository.

Search and Azure OpenAI can be deployed with authenticated public endpoints or as
private-only accounts. Private-only state storage or OpenAI accounts return 403
`AuthorizationFailure` to Microsoft-hosted agents (section 7). None of this implies production
approval or any data-residency guarantee. Microsoft-hosted agents have neither guaranteed
private-endpoint reachability nor a geography guarantee based on Search's region. For private
endpoints, select an approved VNet-connected agent pool with private DNS and
permitted outbound access to Azure DevOps, Entra and approved dependency sources.
Owners must verify current subscription readiness before the first run.

### Admin-owned identity setup script

[scripts/New-IngestionIdentity.ps1](../scripts/New-IngestionIdentity.ps1) is a
separate **administrator tool**, not part of worker ingestion or pipeline
execution. Generating this script does not authorize running it against Azure.
It requires PowerShell 7, Azure CLI and an existing logged-in session selecting
the explicit tenant and subscription. It never logs in or changes your context.

An Entra administrator role alone does not grant Azure resource IAM authority.
The caller also needs permission to read the selected resources/role definitions,
create the custom role definition in the Search resource group, and assign roles
at the listed scopes. App/service-principal creation requires the appropriate
directory permissions. A denied operation fails; the script never grants itself
permissions, uses a secret fallback or automatically elevates the caller.

Run it **after** the platform resources are provisioned: it reads the Search
service, embedding account and state container and fails if one is missing. When
a platform runbook already created the federated service connection with the
manual (existing app registration) option, pass that application's client ID as
`-ExistingAppId` instead of creating another app. If the platform's own
post-provision step already granted Storage Blob Data Contributor on the state
container to that identity, the script finds the exact assignment and reports it
with `"created": false`; the duplicate is harmless. For Agent Landing Zone
handoff values, see the parameter table in
[deployment Step 3.2](deployment.md#step-3-create-the-service-connection-and-grant-roles).

Prepare exact existing ARM resource IDs from each resource's Properties/JSON
view. Do not supply only display names or infer a production subscription:

```powershell
$setup = @{
    TenantId = '<APPROVED_TENANT_UUID>'
    SubscriptionId = '<APPROVED_SUBSCRIPTION_UUID>'
    SearchServiceResourceId = '/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.Search/searchServices/<SERVICE>'
    IndexName = '<EXISTING_APPROVED_TEST_INDEX>'
    OpenAIResourceId = '/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<ACCOUNT>'
    StateContainerResourceId = '/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.Storage/storageAccounts/<ACCOUNT>/blobServices/default/containers/<CONTAINER>'
    AppDisplayName = 'ado-markdown-ingestion'
}

# Read-only preview: checks context/resources and prints proposed changes.
.\scripts\New-IngestionIdentity.ps1 @setup

# After administrator review, explicitly apply the listed changes.
.\scripts\New-IngestionIdentity.ps1 @setup -Apply
```

`-Apply -WhatIf` also previews without changes. Preview still makes read-only
Azure/Entra requests; it is not an offline mock. An existing registration can be
selected explicitly with `-ExistingAppId '<APPLICATION_CLIENT_UUID>'`.
Otherwise an exact unique display-name match is reused; ambiguous or multitenant
matches are rejected. The script creates no client secret, certificate, Graph API
permission, Azure resource, index, service connection or federation credential.

The script assigns these roles to the application's **service principal**:

| Permission | Exact assignment scope |
| --- | --- |
| Search Index Data Contributor | Selected index only: `<SEARCH_RESOURCE_ID>/indexes/<INDEX>` |
| Cognitive Services OpenAI User | Selected embedding account only |
| Storage Blob Data Contributor | Selected existing private state container only |
| Custom index-definition reader | Selected Search service only; its sole action is `Microsoft.Search/searchServices/indexes/read` |

The custom role's assignable scope is the Search resource group; that is role
definition availability, not a resource-group-wide assignment. Existing custom
roles with different permissions/assignable scopes and conditional/duplicate
assignments require owner review; the script does not replace or broaden them.
No subscription-wide Contributor or Search Service Contributor is assigned.

Normal reruns reuse the app, service principal, exact schema-reader role and
existing exact role assignments. Pre-existing unrelated privileges on a reused
identity are not removed or audited by this tool. Avoid concurrent runs; app
display names are not globally unique.
On partial failure, preserve the displayed nonsecret application/principal IDs,
resolve the actual permission/propagation problem, and rerun with the intended
`ExistingAppId`. No destructive rollback or blind retry is performed.

The final JSON contains tenant/subscription/client/application-object/principal
IDs and scoped assignment results only. It explicitly states that runtime
access is **not verified**. IAM propagation can take time. Configure the Azure
DevOps WIF service connection draft next, then use its exact issuer/subject/
audience for federation; do not invent those values or create a secret instead.
This script does not grant repository checkout access or modify the Search
service's separate query-vectorizer identity.

The automated tests use a test-only PowerShell `az` emulator with no fallback to
the real executable. They exercise preview, apply, rerun and failure behavior
without identity/role changes in Azure. The same PowerShell 7 prerequisite applies
to agents running this test suite. Actual app creation, RBAC propagation and
federated runtime access are not exercised by the tests; verify them in your tenant.

## 3. Configure the pinned worker and content pipeline

1. With separate source-control approval, host this worker in the intended
   repository. No Git initialization/commit/push is performed by these examples.
2. Create reviewed source rules using
   [examples/pipeline-source.toml](../examples/pipeline-source.toml). Enter the real
   repository GUID, full approved branch, clean Azure DevOps URL, deployment and
   include/exclude rules. `source_root` and `commit` are agent-bound at runtime;
   they are not inferred from arbitrary developer login state.
3. Create a reviewed pipeline publishing configuration from
   [examples/publishing-config.toml](../examples/publishing-config.toml), with
   `auth_mode = "pipeline"`, explicit tenant/client/service connection UUIDs,
   existing canonical endpoints/index/container and the owner-supplied schema
   hash. Set `search_schema_api_version` to the version the schema was exported
   with: a schema with preview permission properties needs that preview version
   (`2025-05-01-preview` for Agent Landing Zone schemas), because a stable read
   omits them and the comparison fails. This affects schema reads only. Copy
   `embedding_endpoint` so that it equals the schema vectorizer's `resourceUri`.
4. Provide the approved full schema either as an Azure DevOps Secure File (set the
   template's `schemaSecureFile`) or as a reviewed copy in the worker repository
   (see "Platform schema as a pinned in-repo copy" below). Either is artifact
   delivery, not a way to make the consumer JSON deployable; the copy remains pinned by
   `schema_sha256`.
5. Provide the approval model (section 4): per-commit admission as another Secure File
   with its independently approved SHA-256 in `Ingestion.AdmissionSha256` inside an
   owner-controlled variable group, or a scope admission/POC waiver in the reviewed
   publishing config. Any variable group and secure files must have explicit pipeline
   permissions and branch/approval checks; do not grant access to all pipelines.
6. Precreate the protected publishing environment and configure its approvals,
   branch control, exclusive execution policy if needed and pipeline permissions.
   Prevent unauthorized environment auto-creation. A YAML environment name or
   branch condition is not itself an authorization boundary.
7. Configure the workload-identity-federated Azure Resource Manager service
   connection, or reuse the one a platform runbook already created with the
   manual option (do not create a second one). Do not select a secret-based connection.
   Do not grant resource administration solely to make service-connection verification
   succeed. Creating the connection with the automatic option also creates its own app
   registration and federated credential; grant that identity (or the
   `New-IngestionIdentity.ps1` app, if you use the manual option) only the four scoped
   roles above, and remove any unrelated role the wizard or an earlier step assigned
   (for example a stray Contributor on a resource group).
8. Production: pin a reviewed **full 40-character worker commit** in both the repository
   resource `ref` and template `workerCommit` in the
   [content example](../examples/content-repo/azure-pipelines.yml). The commit is one of the
   worker repository hosted in your Azure DevOps project, which contains the reviewed
   `config/<name>/`; the source revision you imported (for example a GitHub commit) is
   provenance only and is not a valid pin. Azure Pipelines does not
   officially accept a bare SHA as a repository resource `ref`, so use an immutable tag
   for `ref` and keep the full SHA in `workerCommit`; the runtime check requires the
   exact SHA. The POC may instead leave `workerCommit` empty on a protected `main`
   (see "Unpinned worker"); pin before real content.
   Replace all placeholders, including repository, connection, environment and secure-file
   names.
9. Copy the thin root example to the content repository only with owner approval.
   It uses explicit distinct `s/content` and `s/worker` checkouts and full history.
   Template expansion alone does not check out worker code.

The protected deployment job uses `AzureCLI@2` as the service-connection resource
reference. It exports only in-memory `servicePrincipalId`, `tenantId` and federated
`idToken` values; the adapter rejects secret-based credentials. It verifies those
identity fields against the reviewed configuration and uses
`AzurePipelinesCredential` with job-scoped `SYSTEM_ACCESSTOKEN`. The worker does
not use the Azure CLI session as its pipeline credential. No job/OIDC token is
written to the temporary TOML settings, artifacts, checkpoints or logs.

The small [pipeline adapter](../src/ado_markdown_ingestion/pipeline.py) checks the
worker checkout, resource-resolved version and explicit immutable worker pin (when a
pin is set); source HEAD, build commit, repository ID and approved branch must also agree.
After those checks it points the **local** approved branch ref at the run's verified
commit: detached hosted checkouts have no such ref, and persistent self-hosted
workspaces keep a stale one from an earlier run. It never fetches history, commits,
pushes, runs content scripts or follows document links.
Agent-relative input paths replace developer-local paths. Temporary configurations
contain only nonsecret settings and are removed after the worker exits. The
adapter neither creates an approval record nor derives a trusted checksum from
an unreviewed admission.

## 4. Approval models: per-commit admission (default), scope admission, POC waiver

The default **per-commit admission** approves one exact source commit, selection
fingerprint and target. The two lighter models below are deliberate, documented
relaxations; choose one explicitly.
Schema and admission files must come from the approved artifact workflow, not
mutable developer files or an untrusted PR. In particular, do not place a
commit-pinned admission inside the same content commit that it identifies:
that creates a self-referential commit-hash problem.

For each new approved corpus snapshot:

1. Resolve the real source commit and run offline `plan` with the reviewed
   source rules. Review files, counts, selection/config fingerprints and audience.
2. The authorized owner approves the exact commit, audience, physical target and
   any explicit bootstrap/history/selection recovery flags.
3. Deliver that immutable admission record and its independent checksum through
   the protected Secure File/variable-group workflow, then authorize the run.
4. If the run's source commit and admission do not match, publishing fails closed
   before Azure worker clients are constructed. It does not update the admission
   to whatever HEAD happens to be.

The example's `enablePublishing` defaults to **false**. First run the offline
stage, then separately authorize a manual publishing run against the waived or
admitted target. Only after service checks/pipeline evidence pass may owners change
the default to enable approved-branch CI and scheduled publication (the POC profile
in section 7 does this for a waived POC index: non-serving by default, or a serving
POC index under the conditions below).

The UTC `always: true` schedule is in the consuming root pipeline; when publishing
is enabled, it selects `reconcile` even without new Git commits. It still needs
an admission for the checked-out snapshot. A new unreviewed branch commit does
not become admissible because the trigger is scheduled. UI-defined schedules
override YAML schedules; validate the real schedule configuration. `always: true`
can overlap scheduled runs despite `batch`; Blob index-wide leasing remains
necessary. The reusable template does not invent repository-resource `batch`.

Manual rebuild must select a reviewed publishing configuration/admission/schema
for an explicitly non-serving generation. The adapter rejects nonmanual rebuilds;
the core also checks the admission's target designation. It never promotes it.

### Optional scope admission (`policy_version` 2) for hands-off ingestion

Per-commit admission is the default and stays supported. When the owners decide that
review happens in the content repository instead, a version 2 admission approves the
**scope** once: repository ID, full ref, selection fingerprint, physical target, audience
and access mode. It must not contain `approved_commit`. Then every commit merged to the
approved ref can run `sync` or `reconcile` without a new admission, Secure File or
variable-group SHA. Version 2 never admits `rebuild`, which still needs a per-commit
(version 1) admission for a non-serving target. Any change to repo, ref, include/exclude
rules, target or audience changes the scope and needs a new admission.

Starting from [admission.scope.template.json](../examples/admission.scope.template.json),
adopt this only after the owners confirm:

- the approved ref has branch policies (required PR review) in the content repository,
  because merged content is published to the approved audience without a second review;
- the protected environment has no blocking approval check for `sync`/`reconcile` (the
  check is portal-side and is not removed by this repository);
- the audience reference covers all content the approved paths can ever match.

### POC admission waiver

For a POC, the platform owner may decide that a reviewed merge to the approved content ref is
sufficient approval. Setting `admission_waiver = "<recorded decision>"` in the reviewed publishing
config (and omitting `admission`/`admission_sha256`) replaces the admission file. The pinned worker
config still binds the exact target (index, endpoints, container), and the source config still binds
repo, ref and paths. The waiver implies: `allow_bootstrap`, `allow_selection_reset` (reconcile only),
no bootstrap over existing documents and no history reset. It is rejected if combined with an
admission or an external admission pin, or if it contains `<` or `>` or is under 20 characters.

**Target.** The recommended target is a **non-serving test index** with synthetic content. A POC owner
may also use the waiver on a **serving POC index** (one a POC chat or application queries) as a
recorded decision, only when **all** of these apply:

- the content is synthetic, or common-audience content approved for every user who can reach the
  application (in an anonymous access mode, everyone who can reach it, for example everyone on the VPN);
- there are no production users;
- no document permissions are claimed;
- the index holds none of that source's documents yet: the waiver refuses bootstrap over existing
  documents for the same repository and branch.

Record the decision and its date in the waiver text, for example
`"POC owner decision 2026-01-15: serving POC index, common-audience content only, no permissions claimed"`.
The worker cannot check whether an index serves an application; the waiver text is the record.

The worker treats every waived target as `non-serving` (the waiver carries that designation
internally), so the core does not reject a `rebuild` against a waived serving POC index. Never select
`rebuild` for it; use `sync` and `reconcile` only.

This is **not** an audience or authorization claim. The external gates for document
authorization and the audience mapping remain unmet. Remove the waiver and use a version 1 or 2
admission **before any real or restricted content or any production users**, on any index, and
before any serving index that does not meet every condition above.

Source changes to the include/exclude rules are still blocked by the selection fingerprint.

### Platform schema as a pinned in-repo copy

The platform schema may also be a reviewed copy in the pinned worker repository (for example
`config/<name>/<index>.schema.json`, referenced by a relative `platform_schema` in the publishing
config; see [examples/index-schema.example.json](../examples/index-schema.example.json) for the
expected shape). It is still verified byte-for-byte against `schema_sha256` and compared with the live
index before any write, so a stale or edited copy fails closed. It contains no secrets (no keys,
only the embedding endpoint). The platform owner remains the source of truth: update the copy and
its hash whenever the index schema changes. Leave the template's `schemaSecureFile` empty to use
it; supplying a Secure File instead overrides the in-repo path. Keep it LF (`.gitattributes`).

**Reviewed in-repo admission (no Secure File, no variable group).** Because a version 2
admission names no commit, it can be committed in the pinned worker repository, for
example `config/<name>/admission.json`. Set `admission = "config/<name>/admission.json"` and
its `admission_sha256` in the reviewed publishing config and leave the template's
`admissionSecureFile` and `approvalVariableGroup` empty (the default). The pinned worker
commit, reviewed by PR, is then the approval record. When both external values are
supplied (`INGEST_ADMISSION_PATH` and `INGEST_ADMISSION_SHA256`), they still replace the
in-repo values; supplying only one fails closed. Prefer the external Secure
File/variable-group path when approval must be access-controlled separately from code.

## 5. Failure and recovery runbook

| Failure | Safe response |
| --- | --- |
| Missing/corrupt source, wrong ref or shallow history | Stop. Correct the approved checkout/history through normal repository/pipeline permissions. Never interpret enumeration failure as empty content. |
| Schema or query-vectorizer mismatch | Stop. Ask the platform owner to supply/implement a compatible pinned definition; do not modify the index from the worker or drop constraints. |
| Missing checkpoint | Treat as explicit bootstrap. Review existing scoped documents/configuration; a missing pointer does not prove an empty index. |
| Partial upload, deletion or visibility failure | Keep the prior checkpoint and pending journal. Replay the admitted compatible snapshot or an explicitly admitted reconciliation. |
| State write failure after Search writes | Do not report success. Retry/reconcile from durable successful and pending pointers; Search may already contain part or all of the new documents. |
| Lease contention/loss | Let the active publisher finish; abort further writes on ownership loss. Never force-break a lease without proving the publisher is stopped and obtaining operator approval. |
| Older queued build | Reject it. Do not bypass ancestry checks or compare against `HEAD~1`. |
| Divergent history or source-selection change | Obtain explicit owner-reviewed reconciliation admission/reset flags. Do not manufacture an empty inventory to force progress. |
| Model/dimension/processing incompatibility | Select an explicit platform-provisioned compatible generation and rebuild; do not mix configurations in the serving index. A parser, chunking or embedding-text change (for example changing the heading depth, breadcrumb, front-matter handling or the token/character budgets) alters the processing fingerprint and is this case: plan a full rebuild into a new, platform-provisioned index, then have the platform repoint the knowledge source. |
| Quarantined file (`file_quarantined` log, `quarantined` list in the result) | Fix the file in the content repository (encoding, path, heading or table size). Its already-indexed documents stay until the fixed file is processed; the next run reprocesses it because its checksum changes. Use `--fail-on-quarantine` / `INGEST_FAIL_ON_QUARANTINE=true` if the owners want the run to fail instead. Never delete documents by hand. |
| Pull-request `check` failure | Read the listed path and problem (`invalid-utf8`, `nul-character`, `unsafe-or-nonportable-path`, `chunking-failed: ...`, `source lines not covered ...`) and fix the Markdown; the job uses no secrets or Azure access. |
| Credential, network or capacity error | Report it, preserve recoverable state and involve the appropriate owner. No API key/PAT, local state or broad role-grant fallback. |

Control state is private metadata/hashes, not source Markdown or vector storage.
All publishers for a canonical physical index must share the same state container/
namespace; choosing another container does not coordinate with an existing lease.
Changing that location requires a coordinated owner-approved state migration.
Scopes isolate repo/ref inventories, but the lease is physical-index-wide.

Full replacement uploads prevent stale optional metadata. Unchanged files retain
their original document ingestion/source provenance; run checkpoints record the
current reconciled snapshot. Reconciliation checks complete scoped inventory and
vectors; the supported inventory bound is 10000 records, and exceeding it fails.
Tune bounds/retry/visibility limits only from approved evidence, not by suppressing
failure or claiming a truncated inventory is authoritative.

In-place Search updates are not transactional. A Blob lease does not fence an
already in-flight Search request, and state and Search have no cross-service
transaction. Readers can observe partial snapshots. For strict snapshots use
separately validated platform-owned blue/green promotion/rollback.
Worker rollback must use a pinned version compatible with schema, processing and
state formats. Rolling back worker code does not undo visible Search writes.
Do not delete control blobs/journals, broadly delete documents or switch serving
bindings as a worker recovery shortcut.

## 6. Evidence and release gates

Offline checks establish YAML syntax/static rules, actual adapter behavior with
temporary Git repos, and fake SDK flows. They do **not** prove Azure DevOps
template expansion, service-connection authorization, protected-resource checks,
schedule/branch-policy behavior or hosted agent connectivity.

Before calling a pipeline operationally ready, owners separately authorize an
Azure DevOps preview/test run. Record source/worker resolved commits, task/SDK
versions, safe target identity, per-stage status, job-token isolation, exact
document/checkpoint outcomes and no-change embedding/write counts. Keep source
text, vectors, credentials and process environments out of logs/artifacts.
Then run an independent verification of the deployed pipeline (checkpoint contents,
no-change counts, access tests) before treating it as production.

Quality and user-access tests follow [evaluation.md](evaluation.md), separately
from ingestion correctness and schema compatibility.

Official references consulted for these templates:

- [Multi-repository checkout](https://learn.microsoft.com/en-us/azure/devops/pipelines/repos/multi-repo-checkout?view=azure-devops)
- [Repository template refs and immutable pins](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/templates?view=azure-devops)
- [Scheduled triggers](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/scheduled-triggers?view=azure-devops)
- [AzureCLI@2 inputs](https://learn.microsoft.com/en-us/azure/devops/pipelines/tasks/reference/azure-cli-v2?view=azure-pipelines)
- [Secure File download timing and cleanup](https://learn.microsoft.com/en-us/azure/devops/pipelines/tasks/reference/download-secure-file-v1?view=azure-pipelines)
- [Deployment job checkout and artifact behavior](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/deployment-jobs?view=azure-devops)

## 7. Proof-of-concept deployment profile

A POC can use a deliberately relaxed profile for synthetic or common-audience content: against a
non-serving test index (recommended) or, as a recorded owner decision, a serving POC index under
the conditions in section 4. It is **not** production approval. Everything below is non-secret;
no key, token or secret belongs in the repository.

| Item | Typical POC setting |
| --- | --- |
| Content | A test content repository, `refs/heads/main`, a dedicated folder such as `docs/**/*.md` (copy [examples/pipeline-source.toml](../examples/pipeline-source.toml)) |
| Target | A non-serving index, or a serving POC index that meets every waiver condition in section 4, with Search/OpenAI/Storage in the platform resource groups (copy [examples/publishing-config.toml](../examples/publishing-config.toml)) |
| Identity | A federated service connection, either created by the platform runbook (manual option) or by the wizard; its service principal holds Search Index Data Contributor on the index, OpenAI User on the account, Blob Data Contributor on the state container and a schema-reader role. Remove any unrelated Contributor role the wizard assigns |
| Trigger | Pushes to `main` changing the test folder run `sync` automatically; manual runs may choose `sync` or `reconcile` (never `rebuild` for a serving target, which the waiver does not block) |
| Approval | Recorded `admission_waiver` (no admission file, no variable group); audience/ACL are **not** claimed |
| Schema | Byte-pinned in-repo copy, verified by `schema_sha256` and compared with the live index before writes, read with the `search_schema_api_version` it was exported with |
| Worker | Optionally unpinned: the hosted worker repository's protected `main` branch (`workerCommit` empty); pin before real content |
| Agent | Publishing on a self-hosted agent that can reach the private endpoints: a VPN-connected machine for short runs, or a VM in the platform's agent subnet for sustained use (see below), with `toolchain: local` when it has no package-index access; the offline Validate stage may use a hosted image |

Known constraints:

- Private-only state storage and OpenAI accounts return 403 `AuthorizationFailure` to
  Microsoft-hosted agents, which never reach private endpoints. Use an agent that can.
- A split-tunnel VPN leaves package-index traffic on the normal network; that is why the
  offline `toolchain: local` wheel-directory build exists.
- To leave POC mode, restore a commit-pinned worker, an admission (scope or per-commit) with a
  reviewed audience, the platform-owned schema delivery, protected-environment approvals and
  an agent policy owned by the platform team, then independently verify the pipeline.

### Unpinned worker (POC)

The template's `workerCommit` is optional. When empty, the pipeline uses whatever the worker
repository resource's `ref` checks out (for example `refs/heads/main`) and still verifies it is a
valid Git checkout. This removes the commit/tag pin, so anyone able to push to the worker branch
changes the code and the in-repo configuration that write to the index: protect that branch with
required PR review. Set `workerCommit` to a full 40-character commit (and a tag `ref`) again before
real content and for production; [the deployment guide](deployment.md#step-7-tag-and-pin-the-worker-release)
shows how to tag a release and get that value. The pin always names a commit of the worker repository
hosted in your Azure DevOps project, after your `config/<name>/` is merged there, never the imported
source commit.

### Self-hosted agent for private endpoints

When the Search, OpenAI or state Blob endpoints are private-only, run the publishing job on a
self-hosted agent that can reach them. Microsoft-hosted agents never reach private endpoints.
Two hosting options:

- **A machine connected to the point-to-site VPN with private DNS.** Suitable for short POC runs,
  and only while the machine stays connected: a dropped or sleeping connection fails the run. The
  agent must run as a user with the VPN connected.
- **A VM in the platform's agent subnet** (for the Agent Landing Zone, `devops-build-agents-subnet`
  in the workload VNet). Recommended for sustained use: it resolves private endpoints through the
  VNet's linked private DNS zones. The platform reserves the subnet but does not deploy an agent, and
  the subnet has **no NAT gateway by default**. The network owner must provide approved outbound HTTPS
  for it (Azure DevOps, Microsoft Entra ID and the package/tokenizer sources), for example by
  associating an existing NAT gateway, or the agent must use `toolchain: local` for packages.

`toolchain: local` covers Python packages only. The tokenizer asset is still downloaded from
`openaipublic.blob.core.windows.net` on the first run unless the verified `cl100k_base.tiktoken` is
already in the worker checkout's `.cache/tiktoken`, and Azure DevOps and Entra ID egress is always
required. Verify that the endpoints resolve to the private addresses and that port 443 connects
before the first publication.

If the agent cannot reach `files.pythonhosted.org`, set the template's `toolchain: local`. It builds
the locked environment from the wheel directory named by the agent's `INGEST_WHEEL_DIR`
environment variable using `uv export`, `uv pip sync --no-index --require-hashes`, the pinned
`hatchling` and a non-editable install, and runs every later `uv run` with `UV_NO_SYNC=1`. The agent
needs Python 3.12 and `uv`. The hash-verified wheels must match `uv.lock`; refresh the directory
when dependencies change. The agent needs the `az` CLI installed (a VPN-hosted agent must also run as
a user with the VPN connected), and the agent pool must be authorized for the pipeline. The default `toolchain:
download` is unchanged for Microsoft-hosted agents.

#### Build the wheel directory

Build it on a machine that can reach the package index, with the **same operating system,
architecture and Python 3.12** as the agent (the wheels are platform-specific, for example
`tiktoken`), from a checkout of the exact worker commit the pipeline will use:

```powershell
$wheels = 'D:\agent-wheels'            # the directory the agent will use as INGEST_WHEEL_DIR
New-Item -ItemType Directory -Force $wheels | Out-Null
uv export --locked --no-emit-project --group dev --format requirements-txt --no-header -o requirements.txt
python -m pip download --only-binary=:all: --no-deps --require-hashes -r requirements.txt -d $wheels
python -m pip download --only-binary=:all: hatchling==1.32.4 -d $wheels
```

The first `pip download` fetches every locked package and verifies its hash against the lock; the
second adds the pinned build backend and its dependencies. Copy the directory to the agent, set
`INGEST_WHEEL_DIR` for the user the agent runs as, and restart the agent so it sees the variable.
Rebuild the directory whenever `uv.lock` changes, and keep it outside any repository checkout.
The template then installs from it with `--no-index`, so a missing or mismatched wheel fails the run
instead of reaching the network.
