# Limitations

- **Source:** Azure DevOps Git repositories only (`dev.azure.com` and `*.visualstudio.com`
  URLs). Pipelines are Azure Pipelines YAML.
- **Files:** only `.md` files (case-insensitive) matching the include globs are read.
  `.markdown` and `.mdx` files are ignored. A selected symlink or submodule fails the run.
  Files with invalid UTF-8, NUL characters, unsafe paths or content that cannot fit the budget
  are quarantined and reported by path and reason.
- **Embedding model:** fixed to `text-embedding-3-large` with 3072 dimensions by the index contract.
  Other models or dimensions need code changes and a new index generation.
- **Index:** the target must already exist and match the [index contract](index-contract.json).
  The worker never creates or modifies a schema, knowledge source or alias.
- **Access control:** only a uniform-audience corpus is supported. Per-document or per-user
  security is not implemented. Repository access does not imply search access.
- **POC waiver:** the worker cannot tell whether an index serves an application. A waiver on a
  serving POC index rests entirely on the owner's recorded decision and its conditions
  ([deployment Step 5.6](deployment.md#56-approval)). The worker treats a waived target as
  non-serving, so it does not block `rebuild` there.
- **Chunking:** sections are H1-H3. A heading with no body text does not produce a document on its
  own. Leading YAML front matter is removed from the indexed text: only its `title` is used (as
  the document title), so other fields such as `owner` or `tags` are not searchable. The size
  budgets are character and token based, and quarantined files keep their previous
  documents until fixed.
- **Consistency:** Search writes are not transactional, so readers can briefly see a partial
  snapshot during a publish. Strict snapshots need platform-owned blue/green promotion.
- **Scale:** the supported scoped inventory is 10,000 documents.
- **Network:** private-only endpoints are unreachable from Microsoft-hosted agents. A self-hosted
  agent on a VPN-connected machine works only while that connection stays up.
