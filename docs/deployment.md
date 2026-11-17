# Deployment guide: step by step

Follow the steps in order. Each step names who does it, what to do, and a **Done when** check that
you must pass before moving on. Nothing here is run by the worker itself: the platform team
provisions infrastructure, and the worker only publishes documents into it.

## Words used in this guide

| Term | Meaning |
| --- | --- |
| **Worker repository** | This code: the worker, the pipeline templates, and the reviewed `config/<name>/` folders. It is where you write the configuration, but it is never indexed |
| **Content repository** | The Azure DevOps Git repository whose Markdown you want searchable. It holds the `.md` files and the pipeline YAML that runs ingestion |
| **`sync`** | The normal run: publishes new, changed and deleted documents |
| **`reconcile`** | A full repair: compares the whole index and its stored vectors with the repository. The nightly run |
| **`rebuild`** | Publishes into a separate non-serving index generation. Manual only, and not used in this guide |
| **Bootstrap** | The first publication of a repository and branch, when no checkpoint exists. The waiver allows it |
| **Waiver** | A recorded owner decision for a non-serving proof-of-concept index. It makes no audience or access claim |
| **Quarantine** | A file that is skipped with a reported reason (for example invalid UTF-8) while other files proceed |

## Before you start

**Tools** on the machine that runs the rehearsal, and on any self-hosted agent:

| Tool | Version | Used for |
| --- | --- | --- |
| Python | 3.12 | The worker |
| `uv` | 0.7.19 | Locked dependency install |
| `git` | any recent | Reading the exact commit |
| PowerShell | 7 (`pwsh`) | Pipeline steps and the identity script |
| Azure CLI (`az`) | current | Identity script, resource commands. Add the `azure-devops` extension to look up repository IDs |

**Permissions** (one person may hold all of them, or several teams may share them):

- **Azure DevOps:** create repositories and pipelines in the worker and content projects; create a
  service connection and an environment.
- **Microsoft Entra:** create an app registration, which the automatic service connection needs.
  If you cannot, an administrator uses the manual option described in
  [operations](operations.md#admin-owned-identity-setup-script).
- **Azure:** assign roles at the index, account and container scopes, and create a custom role
  definition on the Search resource group (Step 3). Creating the resources themselves is done in your
  provisioning repository, not here.
- **Quota:** enough embedding capacity (tokens per minute) on the `text-embedding-3-large`
  deployment. The first run embeds every section, so a large corpus needs headroom; `plan` in Step 6
  reports the document count so you can size it.

### What the provisioning repository must have delivered

The platform resources are provisioned in your separate provisioning repository, and the worker never
creates or changes them. Collect these values before you start; later steps use them:

| Value | Used in |
| --- | --- |
| Search endpoint, index name | The publishing config (Step 5.4) and the identity script (Step 3) |
| OpenAI endpoint and embedding deployment name | The source and publishing configs (Steps 5.3 and 5.4) |
| Storage account URL and state container name | The publishing config (Step 5.4) |
| Resource IDs of the Search service, the OpenAI account and the state container, plus the subscription ID and tenant ID | The identity script (Step 3) |
| The exact index schema JSON used to create the index, and the REST API version used | The pinned schema (Step 5.5) and `search_schema_api_version` (Step 5.4) |

Confirm these requirements, because the worker refuses to publish when they are not met:

- **Search** has role-based access control enabled for the data plane. The worker signs in with
  Microsoft Entra tokens only, never an admin key.
- **Azure OpenAI** has a `text-embedding-3-large` deployment on the account's own
  `https://<name>.openai.azure.com` endpoint (token sign-in needs a custom subdomain). The index's query
  vectorizer uses the same model and deployment name as ingestion, and its `resourceUri` equals the
  `embedding_endpoint` you will configure, or the worker stops with "Query/ingestion embedding resource
  mismatch".
- **Blob state** is a private container (public access off), shared by all publishers to the same index.
- **The index** satisfies the [index contract](index-contract.json): the fields the worker writes, a cosine
  HNSW vector profile named `default` with a 3072-dimension `contentVector`, a semantic configuration named
  `semantic-config` (title `title`, content `content`, keywords `heading_path` and `category`), and a
  searchable, retrievable `category` field that the worker does not populate. The
  [minimal example schema](../examples/index-schema.minimal.example.json) shows the least that is needed.
  The larger [full example](../examples/index-schema.example.json) also declares preview-only permission
  properties, so an index created from it needs a preview `search_schema_api_version` (the proof of concept
  used `2025-11-01-preview`).

## The steps

| Step | What | Who |
| --- | --- | --- |
| [1](#step-1-decide-the-deployment-profile) | Decide the deployment profile | All |
| [2](#step-2-host-the-worker) | Host the worker | Pipeline owners |
| [3](#step-3-create-the-service-connection-and-grant-roles) | Create the service connection and grant roles | Platform and pipeline owners |
| [4](#step-4-create-the-environment-and-agent-access) | Create the environment and agent access | Pipeline owners |
| [5](#step-5-write-the-reviewed-configuration) | Write the reviewed configuration | Pipeline and content owners |
| [6](#step-6-rehearse-locally-offline) | Rehearse locally, offline | Pipeline owners |
| [7](#step-7-tag-and-pin-the-worker-release) | Tag and pin the worker release | Pipeline owners |
| [8](#step-8-add-the-pipeline-to-the-content-repository) | Add the pipeline to each content repository | Pipeline owners |
| [9](#step-9-first-publication) | First publication | All |
| [10](#step-10-verify-and-automate) | Verify and automate | All |
| [11](#step-11-move-to-production) | Move to production | All |

## Step 1: Decide the deployment profile

This guide sets up the **proof-of-concept profile**: a non-serving test index with synthetic content, approved
by a recorded waiver. For a serving index or real content, see [Step 11](#step-11-move-to-production). Agree
these three decisions first. They change what the later steps need.

| Decision | Options | Effect |
| --- | --- | --- |
| Purpose | Confirm that the target is a **non-serving test index** and the content is synthetic or approved for the test | The waiver makes no audience or access claim, so use it only for that. A proof of concept may also leave the worker unpinned |
| Network | Public endpoints, or **private-only** Search, OpenAI and Storage | Private-only needs a self-hosted agent that can reach them (Step 4). Microsoft-hosted agents get 403 `AuthorizationFailure` |
| Package access | The agent can reach the package index, or cannot | If it cannot, use `toolchain: local` with an offline wheel directory (Step 4) |

**Done when:** the three decisions are written down and the owners (platform, content, pipeline) agree.
If you are on the Agent Landing Zone, also read [Appendix A](#appendix-a-agent-landing-zone-handoff-optional).

## Step 2: Host the worker

**Who:** pipeline owners.

1. **Create the repository and push the code.** Create an empty Git repository in the Azure DevOps
   project that the content pipelines can read, then from the unpacked worker folder:

   ```powershell
   git init -b main
   git add -A
   git commit -m "Import ado-markdown-ingestion"
   git remote add origin https://dev.azure.com/<ORG>/<PROJECT>/_git/<WORKER_REPOSITORY>
   git push -u origin main
   ```
2. **Create the worker CI pipeline.** Pipelines, New pipeline, Azure Repos Git, your worker repository,
   Existing Azure Pipelines YAML file, `/pipelines/ci.yml`. It runs the lint, type and offline test
   checks on `windows-latest` and `ubuntu-latest` Microsoft-hosted images. If your organization has no
   hosted parallelism, edit the `pool` in `ci.yml` to a self-hosted pool first.
3. **Protect `main`.** Require pull-request review and add a build-validation policy that runs the CI
   pipeline. Pipelines run this code with write access to the index, so anyone who can push to the
   branch can change what gets written.

**Done when:** CI is green on `main` and `main` requires review.

## Step 3: Create the service connection and grant roles

**Who:** an Azure DevOps project administrator for the connection, and someone who can assign Azure
roles for the script. Do these in order, because the publishing configuration needs the IDs they produce.

1. **Create the service connection.** In the project that runs the content pipeline, create a
   **workload-identity-federated Azure Resource Manager service connection**. Do not choose a
   secret-based connection. The automatic option creates its own app registration and federated
   credential, and assigns a broad role at the scope you pick, so choose the smallest scope that
   contains your resources. Authorize it only for the ingestion pipeline. Record three values: the
   **tenant ID** (`az account show --query tenantId -o tsv`), the application's **client ID** and the
   **service connection ID** (the `resourceId` in the connection's URL).
2. **Grant the four roles** with [scripts/New-IngestionIdentity.ps1](../scripts/New-IngestionIdentity.ps1).
   Passing `-ExistingAppId` makes it use the connection's application instead of creating a second one:

   ```powershell
   $setup = @{
       TenantId = '<TENANT_ID>'
       SubscriptionId = '<SUBSCRIPTION_ID>'
       SearchServiceResourceId = '<SEARCH_SERVICE_RESOURCE_ID>'
       IndexName = '<INDEX_NAME>'
       OpenAIResourceId = '<OPENAI_RESOURCE_ID>'
       StateContainerResourceId = '<STATE_CONTAINER_RESOURCE_ID>'
       ExistingAppId = '<CLIENT_ID_FROM_STEP_3.1>'
   }
   .\scripts\New-IngestionIdentity.ps1 @setup          # preview only: read it carefully
   .\scripts\New-IngestionIdentity.ps1 @setup -Apply   # then apply
   ```

   It assigns exactly these roles: Search Index Data Contributor on the index, Cognitive Services
   OpenAI User on the account, Storage Blob Data Contributor on the container, and a custom
   index-definition reader on the Search service. It reuses only a single-tenant application and
   stops with an error otherwise. It needs PowerShell 7, the Azure CLI and an existing `az` login in
   the right tenant and subscription. Details: [operations](operations.md#admin-owned-identity-setup-script).

   **If it stops with "The existing schema-reader role differs from the exact read-only definition".** The
   custom role name (default `ADO Markdown Index Schema Reader`) is unique per subscription, but the role can
   only be assigned inside the one resource group it was created for. A second environment in another resource
   group therefore finds the first one's role and refuses to change it. Add a distinct name to `$setup` and rerun:

   ```powershell
   $setup.SchemaReaderRoleName = 'ADO Markdown Index Schema Reader - <ENVIRONMENT>'
   ```
3. **Check the roles landed.** Look up the service principal's object ID from its client ID, then list what it
   holds. You expect the four roles above, and nothing on the Search or OpenAI resources means the script has not
   been applied:

   ```powershell
   $oid = az ad sp show --id <CLIENT_ID> --query id -o tsv
   az role assignment list --assignee $oid --all --query "[].{role:roleDefinitionName, scope:scope}" -o table
   ```

   The role names in the output must include `Search Index Data Contributor`, `Cognitive Services OpenAI User`,
   `Storage Blob Data Contributor` and your schema-reader role. The service connection wizard alone does **not**
   grant the first, second and fourth: a pipeline that has only the wizard's roles fails on the first Search call
   with `schema-read failed permanently (status 403)`.
4. **Review the broad role** the connection wizard assigned (often Contributor). The worker needs only
   the four roles above. Remove the broad one only after you have run the Step 9 publication
   successfully, because whether the pipeline can still sign in without any subscription-level role
   depends on your tenant; if it cannot, grant Reader on the resource group instead (see Troubleshooting).

**Done when:** you have the three IDs, and the service principal shows the four roles in the Azure
portal. Role assignments can take several minutes to propagate.

## Step 4: Create the environment and agent access

**Who:** pipeline owners and platform team.

1. **Protected environment.** Pre-create it (Pipelines, Environments) with the approvals and branch
   control you need, so a pipeline run cannot auto-create it.
2. **Repository access.** Give the pipeline's build identity read access to both the content
   repository and the worker repository.
3. **Agent pool.** Authorize a pool for the pipeline. Use a **self-hosted** pool if Search, OpenAI or
   Storage are private-only: the agent must resolve their private addresses and reach them on port 443.
   It needs Python 3.12 and `uv` (for `toolchain: local`), `git`, PowerShell 7 and the `az` CLI.
4. **Offline toolchain (only if the agent cannot reach the package index).** Build the wheel
   directory and set `INGEST_WHEEL_DIR` for the agent's user, as described in
   [operations](operations.md#build-the-wheel-directory), then use `toolchain: local` in Step 8.

**Done when:** the environment exists, the build identity can read both repositories, and a trivial
pipeline job runs on the pool.

## Step 5: Write the reviewed configuration

**Who:** pipeline owners with the content owners.

In this step you create **three files** in a new folder `config/<name>/` of
the **worker** repository, and merge them through a pull request. Nothing is typed into the Azure DevOps
portal in this step. `<name>` is a short label you choose, for example `contoso-handbook`. The pipeline reads
this folder from the worker checkout at run time.

### 5.1 Which file holds what

| File, in the worker repository | What goes in it | Section |
| --- | --- | --- |
| `config/<name>/pipeline-source.toml` | **What to index**: the content repository, branch and folders | [5.3](#53-file-1-pipeline-sourcetoml-what-to-index) |
| `config/<name>/publishing-config.toml` | **Where to publish**: the index, endpoints, identity and the approval waiver | [5.4](#54-file-2-publishing-configtoml-where-to-publish) |
| `config/<name>/<index>.schema.json` | A copy of the index schema, pinned by its hash | [5.5](#55-file-3-the-index-schema-and-its-hash) |

The pipeline YAML that goes into the **content** repository is a different file, covered in
[Step 8](#step-8-add-the-pipeline-to-the-content-repository). Two repositories are involved, so be
clear which one each value refers to:

| Repository | What it holds | Values in this step that refer to it |
| --- | --- | --- |
| **Content repository** (the one with the `.md` files, and the one whose pipeline runs ingestion) | The Markdown to index | `repo_id`, `repo_url`, `source_ref`, `include` and `exclude` |
| **Worker repository** (this code) | `config/<name>/` and the pipeline templates | Nothing is identified by ID here: it only stores the files, and the pipeline checks it out by name |

One `config/<name>/` folder describes **one** content repository and branch. To index another content
repository, add another folder (see [Indexing several content repositories](#indexing-several-content-repositories)).

### 5.2 Create the folder and copy the templates

From a clone of the worker repository, on a new branch:

```powershell
git checkout -b config/<name>
New-Item -ItemType Directory config/<name> | Out-Null
Copy-Item examples/pipeline-source.toml     config/<name>/pipeline-source.toml
Copy-Item examples/publishing-config.toml   config/<name>/publishing-config.toml
Copy-Item <schema-file-from-the-provisioning-repository> config/<name>/<index>.schema.json
```

Edit the copies, not the files in `examples/`. In each file, change only the lines named in the tables
below and leave everything else as it is. Leave `target_tokens`, `max_content_chars`, `max_input_tokens`,
`overlap_tokens`, `inventory_limit` and `visibility_seconds` at the example values: the worker rejects more
than 900 tokens or 3800 characters.

### 5.3 File 1: `pipeline-source.toml` (what to index)

Open `config/<name>/pipeline-source.toml`. Change these lines:

| Line (key) | Put | Where the value comes from |
| --- | --- | --- |
| `repo_id` | The ID (a GUID) of the **content** repository | `az repos show --repository <CONTENT_REPO> --project <PROJECT> --organization https://dev.azure.com/<ORG> --query id -o tsv`. It must equal the ID of the repository whose pipeline runs ingestion, or the run stops |
| `source_ref` | The full branch ref that is indexed, for example `refs/heads/main` | Content owners. It must be the branch the pipeline runs on |
| `repo_url` | `https://dev.azure.com/<ORG>/<PROJECT>/_git/<CONTENT_REPO>` | The content repository's address, with no credentials, query string or trailing path. It becomes the citation link on every indexed section |
| `include` | The folders and files to index, for example `["docs/**/*.md"]` | Content owners. Paths are inside the content repository. Only `.md` files are read; `*.md` matches the root and `**/*.md` includes subfolders |
| `exclude` | What to skip, for example `["docs/drafts/**"]`, or `[]` | Content owners. Exclusions win over inclusions |
| `deployment` | The embedding deployment name | The provisioning repository |
| `contract` | `"../../docs/index-contract.json"` | One more `../` than the example, because the file is now one folder deeper |
| `tokenizer_cache` | `"../../.cache/tiktoken"` | The same reason. The pipeline replaces both paths, so they only matter for the Step 6 rehearsal |

**Leave** `source_root` and `commit` with their `<...>` placeholders (the pipeline fills them in at run time),
and leave `target_tokens`, `max_content_chars`, `max_input_tokens`, `overlap_tokens`, `model` and `dimensions`.

A finished file, with fictional values:

```toml
# Example (fictional values): config/contoso/pipeline-source.toml
source_root = "<AGENT_CONTENT_CHECKOUT>"
repo_id = "11111111-2222-3333-4444-555555555555"
source_ref = "refs/heads/main"
commit = "<EXACT_PIPELINE_SOURCE_COMMIT>"
repo_url = "https://dev.azure.com/contoso/Knowledge/_git/handbook"
include = ["docs/**/*.md"]
exclude = ["docs/drafts/**"]
contract = "../../docs/index-contract.json"
tokenizer_cache = "../../.cache/tiktoken"
target_tokens = 900
max_content_chars = 3800
max_input_tokens = 8000
overlap_tokens = 0
model = "text-embedding-3-large"
deployment = "text-embedding"
dimensions = 3072
```

### 5.4 File 2: `publishing-config.toml` (where to publish)

Open `config/<name>/publishing-config.toml`. Change these lines:

| Line (key) | Put | Where the value comes from |
| --- | --- | --- |
| `search_endpoint` | `https://<search-service>.search.windows.net` | The provisioning repository |
| `index_name` | The existing index name | The provisioning repository |
| `search_schema_api_version` | The REST API version used to create the index. `2025-09-01` is the stable default; a schema with preview-only properties, such as the full example schema, needs a preview version such as `2025-11-01-preview` | The provisioning repository |
| `embedding_endpoint` | `https://<openai-account>.openai.azure.com` | The provisioning repository. It must equal the vectorizer's `resourceUri` in the schema |
| `openai_api_version` | Any date-based version your deployment supports, for example `2025-01-01-preview` | The proof of concept used that one |
| `state_account_url` | `https://<storage-account>.blob.core.windows.net` | The provisioning repository |
| `state_container` | The private container name | The provisioning repository |
| `platform_schema` | The path to the schema file **from the worker repository root**, for example `config/<name>/<index>.schema.json` | The file you add in 5.5. (A local rehearsal resolves it from this file's folder instead) |
| `schema_sha256` | The 64-character SHA-256 of that schema file | Computed in 5.5 |
| `admission_waiver` | A recorded decision sentence of 20+ characters, for example `"Proof of concept: non-serving test index, synthetic content only"` | The owner of the test index. See 5.6 |
| `tenant_id`, `client_id`, `service_connection_id` | The three IDs you recorded | Step 3 |

**Leave** `auth_mode = "pipeline"`, `inventory_limit` and `visibility_seconds`. Content repositories that
publish to the same index can share this file (see
[several content repositories](#indexing-several-content-repositories)).

A finished file, with fictional values and the proof-of-concept waiver:

```toml
# Example (fictional values): config/contoso/publishing-config.toml
search_endpoint = "https://srch-contoso.search.windows.net"
index_name = "contoso-handbook-v1"
search_schema_api_version = "2025-09-01"
embedding_endpoint = "https://oai-contoso.openai.azure.com"
openai_api_version = "2025-01-01-preview"
state_account_url = "https://stcontosostate.blob.core.windows.net"
state_container = "ingestion-state"

platform_schema = "config/contoso/contoso-handbook-v1.schema.json"
schema_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
admission_waiver = "Proof of concept: non-serving test index, synthetic content only"

auth_mode = "pipeline"
tenant_id = "00000000-0000-0000-0000-0000000000aa"
client_id = "00000000-0000-0000-0000-0000000000bb"
service_connection_id = "00000000-0000-0000-0000-0000000000cc"
inventory_limit = 10000
visibility_seconds = 120
```

### 5.5 File 3: the index schema and its hash

Commit the exact schema file the provisioning repository used to create the index as `config/<name>/<index>.schema.json` (or deliver it as an Azure
DevOps Secure File). Its SHA-256 is checked against the file's exact bytes, and Git stores `config/**`
with LF line endings (see `.gitattributes`). A Windows editor may save CRLF, which gives a different hash
and the failure "Pinned artifact integrity mismatch". Hash the committed content:

```powershell
git add config/<name>/<index>.schema.json
python -c "import hashlib,subprocess;print(hashlib.sha256(subprocess.run(['git','show',':config/<name>/<index>.schema.json'],capture_output=True,check=True).stdout).hexdigest())"
```

The worker compares every property the pinned schema declares with the live index before it writes, so
the pinned file must be the one you used to create the index, and nobody should change the live index
afterwards without updating the file and its hash.

### 5.6 Approval

For the proof of concept, the approval is a **waiver**: one line, `admission_waiver`, in
`publishing-config.toml`. It records a decision by the owner of the test index. Write a sentence of 20 or more
characters that says what was decided and why, for example:

```toml
admission_waiver = "Proof of concept: non-serving test index, synthetic content only"
```

Nothing else is needed: no admission file, no hash, no secure file and no variable group. Understand what the
waiver means before you use it:

- It states that the target is a **non-serving** index. The worker records this but cannot check it, so make
  sure no application queries this index.
- It makes **no audience or access claim**. Anyone who can query the index can read what you publish, so use
  synthetic or already-approved content only.
- It allows the first publication of a new repository and branch (bootstrap), and lets a `reconcile` run
  adopt a changed file selection. It does not allow publishing over documents that already exist in the index
  for that scope, and it does not allow resetting the recorded history.
- The placeholder text in the template is rejected, so the waiver cannot be left unfilled by accident.

For a serving index or real content, use an admission, a reviewed approval record bound to the repository, branch,
file selection, target and approved audience. That is outside this guide: see
[operations section 4](operations.md#4-approval-models-per-commit-admission-default-scope-admission-poc-waiver)
and [Step 11](#step-11-move-to-production).

### 5.7 Commit and merge

```powershell
git add config/<name>
git commit -m "Add ingestion configuration for <name>"
git push -u origin config/<name>
```

Open a pull request into `main` of the worker repository, have it reviewed, and merge it.

**Done when:** the pull request containing `config/<name>/` is approved and merged.

## Step 6: Rehearse locally, offline

**Who:** pipeline owners. It finds configuration mistakes before any Azure run and needs no Azure access.

```powershell
uv sync --locked --python 3.12 --group dev
uv run --locked ado-ingest prepare-tokenizer --cache-dir .\.cache\tiktoken
```

If the host cannot reach the package index, see the README section on a hash-verified local wheel
directory. Then clone the content repository locally and run, from the worker folder:

```powershell
$content = '<LOCAL-CONTENT-CLONE>'
$commit  = git -C $content rev-parse HEAD
$sha     = '<schema_sha256 from Step 5.5>'
uv run --locked ado-ingest plan  --config config\<name>\pipeline-source.toml --source-root $content --commit $commit --output plan.json `
    --platform-schema config\<name>\<index>.schema.json --schema-sha256 $sha
uv run --locked ado-ingest check --config config\<name>\pipeline-source.toml --source-root $content --commit $commit
```

`--source-root` and `--commit` are required here because the config holds placeholders for them.
`plan` prints the file and document counts, quarantined files and `azure_calls: 0`, and with `--platform-schema`
it also compares the schema file with the consumer contract offline. `check` exits 0 when the Markdown meets
the chunking invariants. Neither command contacts Azure, so a schema or permission problem with the live
index only shows up at the first publication.

**Done when:** `plan` lists the files you expect (and not the ones you excluded), reports
`schema_validation: offline-artifact-only` and `check` reports 0 problems.

## Step 7: Tag and pin the worker release

**Who:** pipeline owners. Production only: a proof of concept may skip this step and track a branch (see
[operations](operations.md#unpinned-worker-poc)).

The configuration lives in the worker repository, so tag **after** Step 5 is merged:

```powershell
git tag v0.2.0 <reviewed-commit>        # a lightweight tag is the simplest
git push origin v0.2.0
git rev-parse "v0.2.0^{commit}"         # the 40-character value for workerCommit
```

In the content pipeline (Step 8) the repository resource `ref` is `refs/tags/v0.2.0` and `workerCommit`
is that 40-character value, in both stages. They must agree. If they do not, the run stops and the error
shows `pin`, `resource_version` and `checkout_head` so you can see which one differs. Always use
`^{commit}`: for an annotated tag a plain `git rev-parse v0.2.0` returns the tag object, not the commit.

**Every later change to `config/` or the worker code** (a new content repository or a changed include rule)
needs a new reviewed commit, a new tag, and an update to `ref` and `workerCommit` in the content
pipeline. This is the price of a pinned worker.

**Done when:** `git rev-parse "<tag>^{commit}"` prints the commit you reviewed.

## Step 8: Add the pipeline to the content repository

**Who:** pipeline owners.

**Repeat this step for every content repository you want to index.** The worker does not discover or crawl
repositories. Each content repository gets its own copy of one small YAML file and its own pipeline, and each
pipeline is wired to its own `config/<name>/` folder in the worker repository (Step 5). Nothing in this step is
shared between repositories except the worker code, the service connection and the index.

### 8.1 Why each content repository needs its own pipeline

- **The pipeline runs inside the content repository.** It checks that repository out as `self`, so the
  Markdown it indexes is the exact commit that triggered the run, and the trigger and the nightly schedule belong
  to that repository.
- **The identity check is per repository.** At run time the worker compares the `repo_id` and `source_ref` in
  `config/<name>/pipeline-source.toml` with the repository and branch that are actually running. If they differ
  the run stops with "Reviewed source identity/ref differs from the checked-out pipeline repository". A pipeline
  copied into another repository without its own config folder therefore fails closed.
- **Content owners cannot redirect the run.** The YAML only names which worker config to use. What is indexed and
  where it is published is fixed by the reviewed files in the worker repository.

| Per content repository (one set each) | Shared, reused as it is |
| --- | --- |
| `config/<name>/pipeline-source.toml` in the worker repository | The worker code and the pipeline templates |
| `azure-pipelines.yml` in the content repository | The service connection, its identity and the roles (Step 3) |
| One Azure DevOps pipeline created from that file | The environment, the agent pool and the index (Step 4) |
| Its trigger paths and nightly schedule time | `publishing-config.toml` and the schema file, when the repositories publish to the same index |

### 8.2 Before you start

Check each item for this repository. Any missing item shows up later as a failed run, not as a warning.

- [ ] Step 5 is merged: `config/<name>/pipeline-source.toml` exists in the worker repository with this content
      repository's real `repo_id`, `repo_url`, `source_ref` and `include`. The `repo_id` is the **content**
      repository's ID, not the worker's.
- [ ] `publishing-config.toml` parses as TOML and holds the approved endpoints, index name, schema path and hash,
      waiver and the three identity values (Step 5.4).
- [ ] The identity values in `publishing-config.toml` are those of the service connection the pipeline will use.
      Look them up instead of copying them from another document:

  ```powershell
  az devops service-endpoint list --organization https://dev.azure.com/<ORG> --project <CONTENT_PROJECT> `
      --query "[].{name:name, id:id, client:authorization.parameters.serviceprincipalid, tenant:authorization.parameters.tenantid}" -o table
  ```

  `tenant_id`, `client_id` and `service_connection_id` in the publishing config must equal the `tenant`, `client`
  and `id` of the row whose `name` you put in `serviceConnection` (see 8.3). A project often has several
  connections, so a copied ID that belongs to a different connection is the most common cause of "Federated task
  identity does not match the reviewed publishing identity".
- [ ] The service connection exists in the **content** repository's project, or is shared into it, and the
      environment from Step 4 exists in that project.
- [ ] The pipeline's build identity can read the worker repository (Step 4.2).
- [ ] The worker commit you will run is merged to the ref the pipeline checks out (`main` for the proof of
      concept). The pipeline reads `config/<name>/` from that ref, so an unmerged branch is not seen.

### 8.3 Edit the pipeline file

Copy [examples/content-repo/azure-pipelines.yml](../examples/content-repo/azure-pipelines.yml) to the root of the
content repository as `azure-pipelines.yml`, then set these values:

| Where in the YAML | Put | Where the value comes from |
| --- | --- | --- |
| `trigger.paths.include` | The folders you index, plus `azure-pipelines.yml`, for example `docs/**` | Must cover the `include` globs in `pipeline-source.toml`, or content changes will not start a run |
| `schedules[].cron` | A nightly UTC time, different from every other content repository that publishes to the same index | Pipeline owners (see [concurrent runs](#indexing-several-content-repositories)) |
| `parameters.sourceConfig` default | `config/<name>/pipeline-source.toml` | The folder you created in Step 5 |
| `parameters.publishingConfig` default | `config/<name>/publishing-config.toml`, or the shared file | Step 5.4 |
| `resources.repositories[].name` | `<WORKER_PROJECT>/<WORKER_REPOSITORY>`, for example `Knowledge/ado-markdown-ingestion` | Where you hosted the worker (Step 2) |
| `resources.repositories[].ref` | Proof of concept: `refs/heads/main`. Production: `refs/tags/<tag>` | Step 7 |
| `workerCommit` (both stages) | Proof of concept: leave it out or `''`. Production: the 40-character commit | Step 7 |
| `approvedRef` (both stages) | The full branch ref that is indexed, normally `refs/heads/main` | Must equal `source_ref` in `pipeline-source.toml` |
| `environment` | The pre-created environment name | Step 4.1 |
| `serviceConnection` | The **name** of the service connection | Step 3.1 |
| `serviceConnectionId` | The **ID** of that same connection | Step 3.1 and the lookup in 8.2. It must equal `service_connection_id` in the publishing config |
| `pool` and `toolchain` | Only for a self-hosted agent or an offline toolchain, in **both** template calls | Step 4.3 and 4.4 |

The example has no secure-file parameters, because the proof of concept reads the schema and the waiver from the
worker repository. The worker template still accepts `schemaSecureFile` and `admissionSecureFile` (both default to
an empty string) for production; do not add them here unless you follow
[operations section 4](operations.md#4-approval-models-per-commit-admission-default-scope-admission-poc-waiver).

A finished file for the proof of concept, with fictional values (a self-hosted pool, a worker on `main`):

```yaml
trigger:
  branches:
    include: [main]
  paths:
    include:
      - docs/**
      - azure-pipelines.yml
pr: none

schedules:
  - cron: '0 3 * * *'
    displayName: Nightly authoritative reconciliation (UTC)
    branches:
      include: [main]
    always: true

parameters:
  - name: enablePublishing
    displayName: Enable admitted Azure writes (false until separately authorized)
    type: boolean
    default: false
  - name: operation
    type: string
    default: sync
    values: [sync, reconcile, rebuild]
  - name: sourceConfig
    type: string
    default: config/contoso-handbook/pipeline-source.toml
  - name: publishingConfig
    type: string
    default: config/contoso-handbook/publishing-config.toml

resources:
  repositories:
    - repository: ingestionWorker          # this alias is used by the template lines below
      type: git
      name: Knowledge/ado-markdown-ingestion
      ref: refs/heads/main                 # production: refs/tags/<tag> plus workerCommit

stages:
  - stage: Validate
    jobs:
      - template: pipelines/templates/ingest-job.yml@ingestionWorker
        parameters:
          operation: plan
          jobName: OfflineValidation
          approvedRef: refs/heads/main
          sourceConfig: ${{ parameters.sourceConfig }}
  - ${{ if and(eq(parameters.enablePublishing, true), ne(variables['Build.Reason'], 'PullRequest')) }}:
      - stage: Publish
        dependsOn: Validate
        condition: and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'), in(variables['Build.Reason'], 'Manual', 'IndividualCI', 'BatchedCI', 'Schedule'))
        jobs:
          - template: pipelines/templates/ingest-job.yml@ingestionWorker
            parameters:
              jobName: PublishIngestion
              pool:
                name: contoso-agents
              toolchain: local
              approvedRef: refs/heads/main
              ${{ if eq(variables['Build.Reason'], 'Schedule') }}:
                operation: reconcile
              ${{ else }}:
                operation: ${{ parameters.operation }}
              sourceConfig: ${{ parameters.sourceConfig }}
              publishingConfig: ${{ parameters.publishingConfig }}
              environment: ingestion-test
              serviceConnection: SC-CONTOSO
              serviceConnectionId: 00000000-0000-0000-0000-0000000000cc
```

The `Validate` stage does not need a pool on a Microsoft-hosted agent, but add the same `pool` and `toolchain`
to it when Search, OpenAI or Storage are private-only or the agent has no package access.

### 8.4 Create the pipeline and run it

1. Copy [examples/content-repo/azure-pipelines.yml](../examples/content-repo/azure-pipelines.yml) to the root
   of the content repository and replace every `<PLACEHOLDER>` (the table in 8.3 lists them):
   - the worker repository resource, its tag, and `workerCommit` (Step 7). For a proof of concept use
     `ref: refs/heads/main` and `workerCommit: ''`,
   - the `sourceConfig` and `publishingConfig` defaults, for example `config/contoso/pipeline-source.toml`
     (they are passed to both stages; if `sourceConfig` is missing the template falls back to the
     placeholder example and the run fails),
   - the environment, service connection name and ID (Steps 3 and 4),
   - the trigger path filter for the folders you index.
2. **Secure files and variable group.** The example is already set up for the proof of concept: it has no secure-file
   parameters and no variable group, because the schema and the waiver live in the worker repository's
   `config/<name>/` folder (Step 5). Do not add any. If you pass a secure file or variable group name that does
   not exist, the Publish stage fails when it looks for it.
3. **Self-hosted pool or offline toolchain.** Add these to the parameters of **both** `ingest-job.yml`
   template calls (they are template parameters and are not in the example by default):

   ```yaml
   pool:
     name: <SELF_HOSTED_POOL_NAME>
   toolchain: local
   ```
4. **Create the pipeline.** In the content project: Pipelines, New pipeline, Azure Repos Git, the content
   repository, Existing Azure Pipelines YAML file, `/azure-pipelines.yml`. Leave `enablePublishing` **false**.
5. **Run it from `main`.** The first run pauses and asks you to permit the worker repository resource and the
   agent pool. Approve them. The Validate stage runs the worker's offline test suite (it takes several minutes
   and needs `git` and PowerShell 7 on the agent), then `plan` against the exact commit, without calling Azure.
   (On any other branch only the tests run; the `plan` step needs the approved branch.)
6. **Add the pull-request check.** Add a build-validation branch policy to the content repository so pull requests
   run the offline `check` of their Markdown.

**Done when:** the Validate stage is green and its `plan` counts match your Step 6 rehearsal.

### 8.5 What the first run asks you to approve

Azure DevOps blocks a pipeline from using protected resources until someone authorizes it once. Expect these
prompts, and approve each for this pipeline only:

| Prompt | When | Approve if |
| --- | --- | --- |
| Permit access to the worker repository resource `ingestionWorker` | First run (Validate) | It is the reviewed worker repository |
| Permit the agent pool | First run, when `pool` names a pool | It is the intended pool |
| Permit the environment | First publication (Step 9) | It is the pre-created protected environment |
| Permit the service connection | First publication (Step 9) | It is the connection whose ID is in the YAML and the publishing config |
| The environment's own approval or check | Every publication, if configured | The change is the one you expect |

Also confirm that the content project's **build service** identity can read the content and the worker
repositories (Step 4.2). A run that stops with "repository not found" or an authorization error before any of
the worker's own messages is almost always one of these permissions.

### 8.6 Repeat for each content repository

For every additional repository, work through this list. Each row names the file or setting that must be unique
to that repository.

1. **Worker repository, by pull request:** add `config/<name2>/pipeline-source.toml` with the new `repo_id`,
   `repo_url`, `source_ref`, `include` and `exclude`. Reuse the shared `publishing-config.toml` and schema when
   the repositories publish to the same index (Step 5 and
   [Indexing several content repositories](#indexing-several-content-repositories)).
2. **Worker repository, production only:** tag again and note the new 40-character commit (Step 7).
3. **Content repository:** add `azure-pipelines.yml` (8.3) with its own `sourceConfig`, its own trigger paths and
   its own `cron` time. `publishingConfig`, `environment`, `serviceConnection` and `serviceConnectionId` are
   normally the same as the other repositories.
4. **Azure DevOps:** create the pipeline for that repository (8.4) and approve the prompts in 8.5.
5. **Environment:** use one shared protected environment with an Exclusive lock check, so two repositories never
   publish at the same moment.
6. **Validate first:** run with `enablePublishing` off, compare `plan` with a local rehearsal (Step 6), then
   publish (Step 9).

| Value | Same for every repository | Different for each repository |
| --- | --- | --- |
| `repo_id`, `repo_url`, `source_ref`, `include`, `exclude` | | Yes, in its own `pipeline-source.toml` |
| `sourceConfig` | | Yes |
| Trigger paths, `cron` time | | Yes |
| `publishingConfig`, schema, index, state container | Yes, when they share one index | |
| Service connection name and ID, environment, pool | Yes | |
| Worker `ref` and `workerCommit` | Yes, unless an older repository stays on an earlier tag | |

## Step 9: First publication

**Who:** with platform and content-owner sign-off.

1. Choose Run pipeline on `main`, tick **Enable admitted Azure writes** (the `enablePublishing`
   parameter) and set `operation` to `sync`. The Publish stage only exists when it is ticked, and it only runs
   on the approved branch for manual, push and scheduled runs.
2. The first run asks you to permit the environment and the service connection. Approve them, and approve the
   environment's own check if you configured one.
3. The waiver allows the first publication of a new repository and branch (bootstrap). A missing checkpoint never
   means the index is empty: if the index already holds documents for that repository and branch, the first run
   stops rather than overwriting them.
4. Read the final log line, `publication_completed`, for `upserts`, `embedding_calls` and `deletions`.

**Done when:** the run is green and the counts match what `plan` predicted.

## Step 10: Verify and automate

1. **Check the index.** In the Azure portal (Search explorer) confirm the document count and that `title`,
   `heading_path`, `url` and `source_path` are populated. Run a keyword query with a phrase from your content
   and confirm the right section comes back with a working `url`.
2. **Check the state.** Confirm blobs now exist under `indexes/<id>/` in the state container.
3. **Check idempotence.** Run `sync` again with no content change: it should report `upserts: 0` and
   `embedding_calls: 0`.
4. **Check a change.** Edit one Markdown file, merge it, and confirm only that file's documents change.
5. **Automate.** Set the `enablePublishing` default to `true` in the pipeline file (through a reviewed change)
   so push and the nightly `reconcile` schedule publish. Retrieval quality and user access are separate
   checks: see [evaluation](evaluation.md).

**Done when:** all five checks pass.

**To stop ingestion**, untick or default `enablePublishing` to `false`, and remove the trigger and schedule.
The indexed documents and the control state stay as they are. Do not delete the state container or its blobs by
hand; see the [recovery runbook](operations.md#5-failure-and-recovery-runbook).

## Step 11: Move to production

A proof of concept uses the relaxed profile in
[operations section 7](operations.md#7-proof-of-concept-deployment-profile): an approval waiver and, optionally,
an unpinned worker. Before real content or a serving index:

- pin the worker to a tag and full commit (Step 7),
- replace the waiver with an admission, a reviewed approval record that names the approved audience, following
  [operations section 4](operations.md#4-approval-models-per-commit-admission-default-scope-admission-poc-waiver)
  (the templates are in [examples/](../examples)),
- agree schema delivery with the platform team and keep `schema_sha256` current,
- enable protected-environment approvals,
- verify retrieval quality and user access separately ([evaluation](evaluation.md)).

## Indexing several content repositories

Yes, the worker repository holds the `repo_id` and `repo_url` of **every** content repository it ingests,
and there is no wildcard. This is deliberate: the selection and the target are reviewed in the worker
repository, so content owners cannot change what is indexed or where it goes by editing their own repository.
At run time the worker refuses to publish unless the configured `repo_id` equals the repository whose pipeline
is running. A content repository cannot supply its own configuration.

For each additional content repository (and for each branch you index):

1. **Add a folder** `config/<name2>/` with its own `pipeline-source.toml`: that repository's `repo_id`,
   `repo_url`, `source_ref`, `include` and `exclude`. Follow Step 5.
2. **Publishing file.** All repositories that publish to the same index can point at one shared
   `publishing-config.toml` and one schema file. Repositories that publish to different indexes need separate files.
3. **Add the pipeline** to that content repository (Step 8) with `sourceConfig` and `publishingConfig` pointing at
   its own files.
4. **Tag again** if the worker is pinned (Step 7). Existing content pipelines can stay on their current tag,
   because their own files are unchanged; only the new pipeline needs the new tag and `workerCommit`.

A layout for two repositories sharing one index with the waiver:

```text
config/
  shared/publishing-config.toml        # one file for both
  shared/contoso-index.schema.json
  handbook/pipeline-source.toml        # handbook content repository
  policies/pipeline-source.toml        # policies content repository
```

**What is shared and what is separate.** Every indexed document carries its `repo_id` and `source_ref`, and each
run reads, updates and deletes only the documents of its own repository and branch, so one repository's run never
removes another's. All publishers to an index share one state container. The state records each repository and
branch separately, and there is a limit of 10,000 documents per repository and branch.

**Concurrent runs.** The publisher lease is held for the whole index, so only one repository can publish at a
time: a run that starts while another holds the lease retries for roughly 15 to 20 seconds and then fails with a
"retry budget exhausted" error. A failed run is safe to rerun, but avoid the collisions:

- point all the publishing pipelines at **one shared protected environment** and add an **Exclusive lock** check
  to it, so Azure DevOps queues the runs instead of overlapping them (a standard environment check; this repository's
  tests do not exercise it),
- give each pipeline a different nightly `cron` time: the example uses `0 3 * * *`, which would start every
  repository at the same moment.

## Troubleshooting

See the [recovery runbook](operations.md#5-failure-and-recovery-runbook). Common first-run failures:

| Symptom | Likely cause |
| --- | --- |
| "source_root must be an explicit filesystem path" | The rehearsal command lacks `--source-root` (Step 6) |
| "Reviewed source identity/ref differs from the checked-out pipeline repository" | `repo_id` or `source_ref` in the source config does not match the **content** repository or branch whose pipeline is running (a common cause is entering the worker repository's ID) |
| "repo_id must be an explicit immutable repository identifier" | The run used the placeholder example: `sourceConfig` is missing or points at `examples/` |
| Worker pin mismatch | `workerCommit` differs from the tag's commit, or the tag moved |
| "Pinned artifact integrity mismatch" | The schema hash does not match the committed file's bytes (often CRLF versus LF) |
| "Admission waiver needs a recorded, non-placeholder reference" | The waiver line still holds the placeholder text, or is shorter than 20 characters |
| Publish stage fails on a secure file or variable group | `schemaSecureFile`, `admissionSecureFile` or a variable group were added with names that do not exist: remove them (Step 8.3) |
| "Cannot read reviewed publishing configuration" | `publishing-config.toml` is not valid TOML (for example doubled quotes) or the path in `publishingConfig` does not exist at the worker ref the pipeline checked out |
| "Federated task identity does not match the reviewed publishing identity" | `tenant_id`, `client_id` or `service_connection_id` in the publishing config differs from the connection named in `serviceConnection`, or `serviceConnectionId` in the YAML belongs to a different connection. Look them up as shown in Step 8.2 |
| "Deployed schema differs from the pinned platform definition" | The schema file differs from the live index, or `search_schema_api_version` does not return the properties the file declares (preview-only properties need a preview version) |
| "Query/ingestion embedding resource mismatch" | The vectorizer's `resourceUri` in the schema differs from `embedding_endpoint` |
| "publisher-acquire retry budget exhausted" | Another run, often another repository's pipeline, holds the index lease. Rerun after it finishes, and see [several content repositories](#indexing-several-content-repositories) |
| "Bootstrap found existing source documents; explicit admission is required" | The index already holds documents for this repository and branch (for example from an earlier test). The waiver does not allow publishing over them. Use a fresh index for the test, or ask the platform team how to handle the existing documents; do not delete them by hand |
| 401 or 403 from Search | RBAC is not enabled for the Search data plane, or the role assignments have not propagated yet |
| "schema-read failed permanently (status 403)" | The service principal lacks the Search roles. List its roles as in Step 3.3 and run the identity script if they are missing. If they are present, wait a few minutes for propagation. If Search has public access disabled, the agent must reach its private endpoint (Step 4.3) |
| "The existing schema-reader role differs from the exact read-only definition" (identity script) | The default role name already exists for another resource group. Pass a distinct `SchemaReaderRoleName` (Step 3.2) |
| 403 `AuthorizationFailure` from Blob or OpenAI | A private-only account reached from a Microsoft-hosted agent |
| The sign-in step fails with "No subscriptions found" | Possible when the principal has only resource-scoped roles. Grant Reader on the resource group that holds the resources, then retry. This is untested in the proof of concept, so report what you find |
| Tokenizer unavailable | Run `ado-ingest prepare-tokenizer` first, or place `cl100k_base.tiktoken` in the cache directory |

## Appendix A: Agent Landing Zone handoff (optional)

Only for customers on the Agent Landing Zone. For a compatible customer revision, the platform's
`python -m config.deployment.ingestion_handoff` command exports an allowlisted JSON artifact from
explicit successful deployment snapshots and the owner-pinned full index schema. It does not provision
resources, grant access or authorize publication. Its `publishing` fields map to this worker's
publishing TOML; its `embedding` fields supply the deployment, model and dimensions.

Review the artifact, copy the exact customer schema bytes, and set `platform_schema` to their approved
path. Do not copy the JSON wholesale into TOML or treat `publication_authorized=false` as admission.
Tenant, client, service-connection identity and approval inputs remain owner work. Match
`search_schema_api_version` to the actual platform schema: the worker's stable default is not evidence
that a preview platform field is supported. The embedding inference API version, the model revision and
the Search schema API version are distinct. Never reuse another environment's schema hash, endpoints or
roles. The platform's declared serving status is an owner assertion, not a live guarantee: verify the
current source, base and application references before using a non-serving waiver. A `test` substring in
a name grants no safety. Follow the delivered platform revision's customer runbook for everything else.
