# ado-markdown-ingestion

An offline-first Python 3.12 worker that turns immutable Azure DevOps Git Markdown into
searchable sections in an **existing** Azure AI Search index. For each approved commit it
splits Markdown by headings, embeds each section with Azure OpenAI
(`text-embedding-3-large`, 3072 dimensions) and publishes only new, changed and deleted
sections, tracking progress in a private Azure Blob control state. The worker never
provisions infrastructure, changes the index schema, registers knowledge sources or
promotes an index.

```text
Content repo (approved branch) --> Azure Pipeline --uses--> this worker repo
                                     |
          Validate (offline, no Azure):  check / plan the exact commit
          Publish (protected, federated identity):  sync | reconcile | rebuild
                                     |
    Git commit --> sections --> embeddings --> Azure AI Search index
                                     +--> checkpoint / journal / lease in private Blob
```

## Deploy in 11 steps

The full procedure, with commands and a "done when" check per step, is the
[deployment guide](docs/deployment.md). The platform resources (Search index, Azure OpenAI deployment, state
container) come from your provisioning repository; the guide lists the values and requirements to collect from it.
In short:

1. **Decide** the profile: confirm a non-serving test index, network and package access.
2. **Host** this worker repository in Azure DevOps, add its CI pipeline and protect `main`.
3. **Create** the federated service connection, then run `scripts/New-IngestionIdentity.ps1` to grant the four roles.
4. **Create** the protected environment, repository access and the agent pool.
5. **Write** the reviewed `config/<name>/`: source rules, publishing target, pinned schema and the proof-of-concept waiver.
6. **Rehearse** offline: `prepare-tokenizer`, then `plan` and `check` on a local clone of the content repository.
7. **Tag** the worker release and pin it (optional for a proof of concept; the configuration must be merged first).
8. **Add** the pipeline to the content repository with publishing still disabled; confirm the Validate stage is green.
9. **Publish** once manually with `operation: sync` after sign-off.
10. **Verify** the index and state, rerun to confirm no changes, then enable the trigger and nightly `reconcile`.
11. **Harden** for production: pin the worker, replace any waiver with an admission, enable approvals.

## Documentation

- For the Agent Landing Zone private/classic profile, use its delivered
  customer revision and [customer runbook](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/azure-devops-markdown-poc/).
  The offline platform handoff supplies bindings, not publishing approval.
- [Deployment guide](docs/deployment.md): the step-by-step procedure, from prerequisites to first
  publication and production hardening
- [Operations](docs/operations.md): pipeline onboarding, identity, approval models, recovery runbook
- [Limitations](docs/limitations.md): what the worker does not do
- [Index consumer contract](docs/index-contract.json): the fields and settings the target index must provide
  (a requirements baseline, not an index creation payload)
- [Example platform schema](examples/index-schema.example.json): the shape of a compatible index definition
  (the full platform-style definition; start from the
  [minimal schema](examples/index-schema.minimal.example.json), which has only the fields the worker needs)
- [Synthetic evaluation and future live quality/access checks](docs/evaluation.md)
- [Contributing](CONTRIBUTING.md), [security policy](SECURITY.md) and [changelog](CHANGELOG.md)

## Azure DevOps pipeline files

- [Worker CI](pipelines/ci.yml): locked setup and Windows/Linux offline checks.
- [Reusable ingestion job](pipelines/templates/ingest-job.yml): offline validation
  or protected, federated publishing against an existing admitted target. Optional
  `pool`, `toolchain` (`download` or `local`, for self-hosted agents without package
  index access), `workerCommit` (empty means unpinned), `schemaSecureFile`,
  `admissionSecureFile` and `approvalVariableGroup` parameters.
- [Thin content-repository example](examples/content-repo/azure-pipelines.yml):
  pinned worker/template, explicit checkout paths, PR isolation, UTC reconciliation
  schedule and manual rebuild selection. Publishing defaults to **disabled**.
- [Example configurations](examples): source rules, publishing target (with the proof-of-concept
  waiver), a platform schema example, and admission templates for production use. Copy them into a reviewed
  `config/<name>/` folder in your fork.
- [Identity script](scripts/New-IngestionIdentity.ps1): admin-run, preview-first app,
  service principal and scoped RBAC setup (see [operations](docs/operations.md)).

The examples contain owner-required placeholders and are not ready to run against
an arbitrary environment. Before remote execution, follow the
[deployment guide](docs/deployment.md): host the worker repository, configure
cross-repository access, create protected environment/service-connection resources,
approve the test target with the proof-of-concept waiver and separately authorize the first
non-serving pipeline test. The waiver makes no audience or access claim; a serving index or real
content needs a reviewed admission instead (see [operations](docs/operations.md)).
PR validation runs the original synthetic tests and the offline `check` job over the pull
request's Markdown, without publishing resources or credentials.

The content repository owns approved documents, the platform owns schema and
retrieval infrastructure, and this project owns deterministic ingestion
transformations. Repository access does not establish application-user access.

Local configurations, admission records and run evidence belong in `.local/`,
which is excluded by [.gitignore](.gitignore), along with environments, caches,
build/test outputs and common credential/key files. Keep `uv.lock`, source,
tests, pipeline definitions and placeholder examples in source control.
Ignore rules do not remove files already tracked by Git or replace a credential
review before publishing a repository.

## Setup and checks

Use Python 3.12 and uv (CI pins uv 0.7.19):

```powershell
uv sync --locked --python 3.12 --group dev
uv run --locked ado-ingest prepare-tokenizer --cache-dir .\.cache\tiktoken
uv run --locked ado-ingest --help
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src tests
uv run --locked pytest -q
uv build
```

`prepare-tokenizer` is explicit dependency preparation, not ingestion: it can
download one public cl100k tokenizer asset over verified HTTPS. The SHA-256 is
`223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`,
as declared by the resolved tiktoken distribution. It never authenticates to
customer Azure resources. An already prepared cache is verified and reused.
For disconnected hosts, transfer the approved asset as `cl100k_base.tiktoken`
inside the configured tokenizer cache directory (for the example above, the
file is `.cache/tiktoken/cl100k_base.tiktoken`).
Plan mode reads this file locally and fails explicitly on absence/corruption;
it never invokes tiktoken's lazy network downloader or changes encoding.
If an existing asset is corrupt, choose a fresh cache or explicitly remove only
that named asset before preparing again; the worker never silently replaces it.

The lock pins runtime and development packages. The build backend is pinned in
project metadata. Dependency preparation may require approved outbound access;
subsequent worker planning needs no network or Azure login.

If the host cannot reach the package index (`files.pythonhosted.org`), `uv sync` and `uv build`
fail on download. Build the environment from a hash-verified local wheel directory instead, as
the `toolchain: local` pipeline option does: see "Build the wheel directory" in
[operations](docs/operations.md#build-the-wheel-directory) for the commands. The wheels
must match `uv.lock`, and `uv build` additionally needs the pinned `hatchling` wheel in that
directory (or `python -m hatchling build` from an environment that already has it). Never
disable TLS verification as a workaround.

## Plan an immutable source snapshot

Fill [the placeholder configuration](examples/worker-config.toml) with the
approved checkout, immutable repo ID, full heads/tags ref, full commit ID and
Azure DevOps Git web URL. Keep content and worker checkouts distinct.

```powershell
uv run --locked ado-ingest plan --config .\examples\worker-config.toml
uv run --locked ado-ingest plan --config .\examples\worker-config.toml --output .\run-plan.json
uv run --locked ado-ingest plan --config .\examples\worker-config.toml --documents
uv run --locked ado-ingest plan --help
```

`--documents` explicitly includes canonical source text in the local output;
omit it for routine metadata-only artifacts. Do not publish these artifacts
without applying your content/data policy. Logs on stderr contain only safe
events, run IDs and counts. JSON results go to stdout or the explicit output.
Output may not overwrite the source checkout or input artifacts.

Configuration precedence is CLI options, named `ADO_INGEST_*` nonsecret
environment values, TOML configuration, then documented processing defaults.
For example `ADO_INGEST_TARGET_TOKENS=500`; environment include/exclude lists use
semicolon separators. Unknown settings are errors. Config-relative filesystem
paths resolve beside the TOML file; CLI/environment paths resolve from the
working directory. Use native filesystem paths on each host. Git path/glob
identity always uses `/` and case-sensitive matching: `*.md` matches the root
only, `**/*.md` also matches nested files, and exclusions win.

The worker reads only regular tracked blobs from the selected commit, not dirty
or untracked working-tree files. It rejects matched symlinks, submodules and
incomplete reads. It never follows submodules, executes document code, fetches
document links, or fetches Git history. A selected file with invalid UTF-8, a NUL
character, an unsafe/nonportable path, or content that cannot fit the budget is
**quarantined** instead of aborting the run: it is reported by path and fixed reason
(never text), the other files proceed, and whatever is already indexed or planned for
that file is kept. `--fail-on-quarantine` (or `INGEST_FAIL_ON_QUARANTINE=true` in the
pipeline) turns any quarantined file into exit status 3 after the run completes.
Full history must be available to establish approval/ancestry and compare a
baseline. Missing, superseded or divergent history is an actionable failure.

Pull requests run `ado-ingest check` (pipeline mode `check`): the same parser,
budget and line-coverage invariants over the checked-out commit, offline, with no
secrets, approved ref or Azure access. It exits 1 and lists path and problem for
every quarantined file or violated invariant.

### Draft documents and chunk behavior

Offline drafts have 20 fields. They deliberately omit `contentVector`,
`ingested_at`, and `ingest_run_id`: neither embeddings nor ingestion has happened.
The pure complete-document validator additionally checks all 23 consumer fields,
finite nonzero 3072-dimensional vectors, and UTC publication timestamps. It does
not generate vectors or prove their model origin.

Sections are H1-H3. Headings are real CommonMark heading nodes (plus explicit table
support); HTML blocks and comments are inert, and heading-like text in fences,
indented code, HTML or front matter never becomes a section. H4-H6 stay inside their
section and are preferred cut points when a section is oversized. Leading YAML
front matter (`---` ... `---`/`...`, shaped like a YAML mapping) is removed from the
indexed text; its `title` supplies the document title, then the first nonempty H1,
then the filename. A leading `---` that is not front matter is an ordinary thematic
break. Only LF separates source lines, and line numbers always refer to the original
file. Heading text is plain (markup, HTML tags and zero-width characters removed,
image alt text kept) and clipped at 200 characters. Heading-only and front-matter-only
files produce no documents. Duplicate headings are numbered within their structural parent.
A subsection under 80 tokens is merged into its direct parent's chunk when the result
still fits; the heading line then stays in the body.

Canonical indexed text (and the basis of the embedding text) is exactly:

```text
<Title > H1/H2/H3 breadcrumb, on one line, at most 300 characters>

<source body, with bounded readability wrappers and carried definitions when necessary>
```

Allowed normalizations only: leading UTF-8 BOM removed, CRLF/CR to LF, Unicode NFC,
front matter removed, HTML comments and the `[[_TOC_]]` macro removed (their line
breaks are kept so line numbers stay exact; text inside code fences is untouched).
Preserve body indentation and meaningful whitespace; strip edge blank newlines and
terminate canonical body text with LF. `content` is hashed exactly with SHA-256.
The text sent to the embedding model is `content` with inline `data:...;base64,...`
payloads replaced by `data:[omitted]` (`embedding_text`); its version is part of the
processing fingerprint. A chunk includes the breadcrumb, wrappers and carried
definitions in its token and character budget. Defaults are 900 target tokens,
a conservative 8000-token hard input ceiling, 3800 Unicode code points (the
application truncates at 4000) and no overlap; configuration rejects more than
900 tokens or 3800 characters. Confirm the application's effective character-count
semantics before publishing.

Oversized sections split in this order: H4-H6 heading, line/block boundary (paragraph,
list item, table row, code block), sentence, whitespace, then a Unicode-safe character
cut that never separates a base character from its combining mark and keeps a Markdown
link whole when it fits. Code fences and table headers are repeated for readability
when split (a table header wider than a third of the budget is not repeated). A
reference-style link or footnote whose definition sits in another chunk gets that
definition appended (at most 600 characters). Optional overlap uses real source spans.
Source lines are one-based/inclusive; generated context/wrappers do not pretend
to be source lines. A breadcrumb that leaves no room for source still fails that
file (it is quarantined), never truncating.

IDs hash unambiguous repository/ref/path/structural-section/part identity, not
content or commit; `chunk_id` is derived from the same id (28 bits), not from the
position. Content edits retain those keys when the section partition is unchanged.
Renames, heading changes, repartitioning, merging of small subsections and insertion
of earlier duplicate headings can legitimately change keys. Citation aliases `url` and
`filepath` contain the same encoded, commit-pinned Azure DevOps file URL, without
invented GitHub line anchors. Actual browser/user access remains unverified.

### Trusted local prior inventory

Without a prior inventory, deletion candidates are **unknown**, never zero by
assumption and never obtained from Azure. To compare two local plans:

```powershell
$sha = (Get-FileHash .\previous-plan.json -Algorithm SHA256).Hash.ToLowerInvariant()
uv run --locked ado-ingest plan --config .\examples\worker-config.toml `
  --previous-inventory .\previous-plan.json --previous-sha256 $sha
```

The prior artifact must be pinned by an independently trusted SHA-256 and have a
valid internal integrity hash, complete format, matching repo/ref and processing/
selection fingerprints. Missing/corrupt/incompatible prior input fails explicitly.
A hash proves integrity, not actual publication or permission approval.

The emitted inventory is labeled `offline-projection-not-published`; it is not
a production checkpoint. No-change files retain their projected original
document revision in that inventory, while the top-level snapshot advances.
Explicitly emitted drafts describe the current target snapshot, not records
claimed to have been rewritten in Search. A no-change plan proposes no upserts
or deletions; rename/delete plans report old keys only within the selected scope.

### Optional offline platform schema comparison

Supply a full REST-shaped platform schema JSON and its owner-provided SHA-256:

```powershell
uv run --locked ado-ingest plan --config .\examples\worker-config.toml `
  --platform-schema "<PINNED_PLATFORM_SCHEMA_JSON>" --schema-sha256 "<OWNER_PROVIDED_SHA256>"
```

Comparison permits additional platform fields and checks every declared consumer
property, cosine HNSW, the default Azure OpenAI vectorizer/model/deployment and
semantic priorities including the existing `category` field. It accepts omitted
REST key/retrievability defaults only; other constrained omissions fail rather
than guessing SDK/service defaults. Model/dimension overrides incompatible with
this consumer contract fail. This check is explicitly `offline-artifact-only`:
it cannot establish live resource identity, effective model revision, permissions
or target readiness. No schema creation/update API exists in this worker.

## Publishing engine: separate runtime admission

The `sync`, `reconcile`, and `rebuild` commands use real Azure SDK adapters.
They are not offline emulators. Do not execute them against live resources until
the owners authorize the exact source, identity, network, schema and target.
Offline tests inject SDK transports; there is no production mock-success mode.

Publishing requires all of:

- An immutable approved source configuration.
- An explicit [publishing configuration](examples/publishing-config.toml) with
  existing physical index, embedding and private state endpoints. Endpoints are
  canonicalized to lowercase/default HTTPS port so aliases cannot create another
  index-lock namespace. Use the canonical service hostname with private DNS, not
  a `privatelink` hostname or a custom alias.
- A pinned full platform REST schema artifact and independently supplied SHA-256.
  The worker reads the deployed index definition and checks both the consumer
  requirements and every pinned platform declaration. It never creates schemas.
- `search_schema_api_version` selects the read-only index-definition REST version
  (default `2025-09-01`). If the platform schema declares preview permission
  properties, select its supported preview version, such as `2025-11-01-preview`.
  A stable GET can omit those properties even when the index contains them.
  This setting never changes document-write APIs or relaxes schema comparison.
- A reviewed, externally pinned source/target
  [admission artifact](examples/admission.template.json) (per-commit, `policy_version` 1),
  a [scope admission](examples/admission.scope.template.json) (`policy_version` 2: no
  commit, never `rebuild`), or, for a non-serving POC only, a recorded `admission_waiver`
  in the publishing config (see operations). The templates grant
  nothing: placeholders, missing pins, mismatched source snapshots/selection or
  unresolved access policy fail before client construction.
- The explicit `--authorize-publish` runtime option. This is operator intent,
  not proof of platform approval or end-user authorization.

Only a reviewed `uniform-approved-corpus` access mode is supported. Protected/
item-level content is refused until its actual platform permission contract is
implemented. A pin proves integrity, not the authority of a reviewer or allowed/
denied-user enforcement. Source repository access alone never authorizes indexing.
The POC waiver makes no audience or ACL claim at all.

The per-commit admission record binds repo ID, full ref, exact commit, physical target and
the `selection_fingerprint` from the reviewed offline plan inventory. Reviewers
must approve the include/exclude rules and repository URL behind that fingerprint.
An include/exclude or citation-base change requires a new bound admission and
`reconcile` with explicit `allow_selection_reset`; normal sync fails closed.
Missing successful state requires explicit `allow_bootstrap`. Existing scoped
documents additionally require `allow_bootstrap_existing`; they are not presumed
empty or vector-compatible. The platform owner must confirm index-wide embedding
compatibility and the one common control-state namespace for all publishers.

For a **separately approved** environment only, the command shape is:

```powershell
uv run --locked ado-ingest sync --config "<SOURCE_CONFIG>" `
  --publishing-config "<REVIEWED_PUBLISHING_CONFIG>" --authorize-publish
```

The uv `--offline` option controls dependency downloads, not the worker's service
calls. A `sync` run is live even when launched with `uv run --offline`; only the
worker's `plan` operation guarantees no Azure calls.

Use `reconcile` for authoritative repair without new commits. An explicitly
reviewed divergent-history reset also needs `allow_history_reset`; missing/shallow
history remains a failure requiring operator action. Older ancestral queued
targets are rejected even when a reset is permitted. `rebuild` additionally
requires the target's reviewed `non-serving` designation; it never discovers,
switches, or promotes a serving knowledge source. Incompatible index/model/
processing fingerprints require an explicitly selected new pre-provisioned
generation, not mixed publication into the old target.

### Identity and data handling

`auth_mode = "pipeline"` selects `AzurePipelinesCredential` using explicit tenant,
client and service connection IDs plus job-scoped `SYSTEM_ACCESSTOKEN` in the
process environment. Never write the token to configuration, logs or artifacts.
The eventual pipeline must authorize that federated service connection; examples
here do not establish that authorization.

`auth_mode = "cli"` explicitly selects `AzureCliCredential` for an already
authorized local context and tenant; the worker does not log in for you.
No default credential chain, key, PAT or SAS fallback is implemented. Ambient
OpenAI keys/static AD tokens are rejected rather than overriding the chosen
Entra identity. Identity needs document read/write, embedding inference, private
state/lease access and separately scoped read-only schema access, not schema
administration or promotion rights.

SDK bodies/tokens are not logged. Target summaries contain only explicit safe
resource/source identifiers and fingerprints. Publish results contain counts,
run ID, revision and timing, not source content or embeddings.

### Synchronization and recovery semantics

- One renewable 60-second lease per canonical physical index, shared across
  source scopes; renew approximately every 20 seconds. Loss/expired renewal
  health aborts subsequent actions.
- Immutable Blob manifests plus one leased, ETag-conditional control pointer.
  Checkpoint and pending-pointer clearing occur in the same control write.
  State is partitioned by physical index/repository/ref and never falls back
  to local files. The container must be private and already exist.
- A durable pending journal precedes Search writes. Retry compares successful
  and pending key inventories with scoped live documents, so failed intermediate
  revisions do not leave orphan chunks.
- Skip compatible unchanged files without embedding calls and retain their
  original document source revision/ingestion provenance. Reuse a vector only
  from verified matching input/configuration provenance. State stores hashes,
  not Markdown or vectors; vector hashes use the Search Single representation.
- Complete replacement uploads precede obsolete-key deletion. Check every
  per-document result; retry only failed transient actions. Ownership checks
  prevent foreign source/ref deletion.
- At most 1000 actions and 16000000 **actual SDK-serialized UTF-8 bytes** per
  indexing request, including actions/vectors/envelope; default batches are 100.
  Oversized batches split before transport, while oversized singleton records
  fail without truncation. Embedding batches are bounded to 16 inputs/16000
  tokens and an 8000-token individual ceiling.
- Read-only complete scoped inventory is limited to 10000 records for this
  release; inconsistent, duplicate or incomplete enumeration fails explicitly.
  Reconciliation also checks vectors to repair missing/corrupted vector data.
- Visibility retries verify exact scoped keys, canonical content/provenance
  signatures and obsolete-key absence before checkpoint commit. Transient
  inventory instability is retried only during bounded verification.
- Record reported model revision when exposed; otherwise record unavailable,
  not an invented version. An observed revision change across runs/scopes
  requires another compatible generation.

Publication is **not transactional**. Readers can observe partial snapshots,
and a Blob lease does not fence an already in-flight Search request. On failure,
retain the last valid checkpoint and pending journal, then replay an admitted
compatible run or authoritative reconciliation. Do not erase journals, replace
the index, or broaden deletions as a recovery shortcut. If a final pointer write
or lease release loses its acknowledgement, the caller can fail even though a
fully verified checkpoint committed; inspect/replay rather than assuming rollback.
Strict snapshots and cutover/rollback require platform-owned blue/green handling.

## Implemented boundary and evidence

Implemented: immutable source inventory/history, deterministic parsing/chunking,
draft and full-field validation, local schema comparison, offline change planning,
explicit tokenizer preparation, real SDK adapters, admitted synchronization/
reconciliation/rebuild, leased control state, journal recovery, safe CLI/error
behavior and synthetic tests.

Not implemented: platform provisioning/promotion, chat retrieval or item-level
authorization enforcement. Pipeline files and offline checks are provided. Microsoft-hosted
agents cannot reach private-only state and OpenAI endpoints; use a self-hosted agent that can.
Live edit/delete/fault-recovery tests, browser citations, retrieval quality and
allowed/denied-user enforcement must be verified in your own environment before production
use (see [evaluation](docs/evaluation.md)). Offline tests do not prove Azure DevOps template
expansion, service-connection authorization or hosted agent connectivity.

Exit codes: `0` successful operation, `1` input/processing/artifact/publication failure,
`2` invalid command/options, `3` quarantined files with `--fail-on-quarantine`.

Dependency preparation needs verified HTTPS access to the package index and the public
tokenizer asset, or a hash-checked local wheel directory (`toolchain: local`) on
disconnected agents. Never disable TLS verification or change registry/security policy
as a workaround.
For the already prepared Windows environment, the validated offline build command
is `uv build --offline --no-build-isolation`; it uses the declared backend already
installed during preparation. Normal `uv build` uses isolated backend preparation
and requires approved package access or a prepared build cache.

## License

Licensed under the [MIT License](LICENSE).
