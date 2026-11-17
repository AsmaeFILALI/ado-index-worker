# Reviewed configuration

This folder holds one subfolder per content repository (or per index) that the worker publishes. It is
empty in the delivered code: you add your own folders. The full walkthrough, with every value and where it
comes from, is [Step 5 of the deployment guide](../docs/deployment.md#step-5-write-the-reviewed-configuration).

## Layout

```text
config/
  <name>/
    pipeline-source.toml        # what to index: content repository, branch, include/exclude
    publishing-config.toml      # where to publish: index, endpoints, identity, waiver
    <index>.schema.json         # exact copy of the index schema, pinned by schema_sha256
```

`<name>` is a short label you choose, for example `contoso-handbook`. Start from the templates in
[examples/](../examples):

```powershell
New-Item -ItemType Directory config/<name> | Out-Null
Copy-Item examples/pipeline-source.toml   config/<name>/pipeline-source.toml
Copy-Item examples/publishing-config.toml config/<name>/publishing-config.toml
Copy-Item <schema-from-your-provisioning-repository> config/<name>/<index>.schema.json
```

Several content repositories that publish to the same index can share one `publishing-config.toml` and
schema file, each with its own `pipeline-source.toml`. See
[Indexing several content repositories](../docs/deployment.md#indexing-several-content-repositories).

## Rules

- **One `pipeline-source.toml` per content repository and branch.** Its `repo_id` must be the ID of the
  **content** repository whose pipeline runs ingestion, not this repository's ID; otherwise the run stops.
- **Paths are relative to the repository root** in the pipeline: set `platform_schema` to
  `config/<name>/<index>.schema.json`. In `pipeline-source.toml`, `contract` and `tokenizer_cache` use
  `../../` because the file sits two folders deep; the pipeline replaces both at run time.
- **The schema is pinned byte-for-byte.** `schema_sha256` is the lowercase SHA-256 of the committed file.
  [.gitattributes](../.gitattributes) forces LF line endings under `config/` so the hash is the same on
  every platform. Recompute it whenever the file changes:

  ```powershell
  (Get-FileHash config/<name>/<index>.schema.json -Algorithm SHA256).Hash.ToLower()
  ```

- **Merge through a reviewed pull request.** Pipelines read this folder from the worker ref they check out
  (`main`, or a pinned tag in production), so an unmerged branch is not used, and anyone who can change
  these files changes what is indexed and where it is published.
- **No secrets belong here.** The worker uses a federated service connection, so these files hold only
  endpoints, names and IDs. They still describe your environment: keep your fork private, or keep the
  folders out of any public copy.
