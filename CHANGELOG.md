# Changelog

## Unreleased

- Documentation aligned with the Agent Landing Zone private Azure DevOps Markdown POC runbook
  (docs and examples only; no behavior change):
  - the POC waiver may approve a serving POC index as a recorded owner decision when every
    stated condition applies; the non-serving test index stays the recommended default, and an
    admission must replace the waiver before real or restricted content or production users;
  - `search_schema_api_version` must match the version the schema was exported with
    (`2025-05-01-preview` for platform schemas with preview permission properties);
  - `embedding_endpoint` must equal the schema vectorizer's `resourceUri`, copied from the handoff;
  - reuse of a platform-created service connection, and a handoff-to-identity-script parameter map;
  - pins name commits of the worker repository hosted in the customer's Azure DevOps project;
  - self-hosted agent options for private endpoints (VPN machine or agent-subnet VM) and their egress.

## 0.2.0

- Deployment guide rewritten as 11 ordered steps with prerequisites, a glossary, the handoff from the
  provisioning repository, Azure
  DevOps UI steps and guidance for every value; configuration is written and rehearsed before the
  worker is tagged and pinned. Added a minimal example index schema (consumer fields plus `category`).
- The content-repository example now passes `sourceConfig` to both stages.
- Added `SECURITY.md`.

- Deterministic, heading-based chunking of Azure DevOps Markdown with line-exact citations,
  YAML front-matter handling and quarantine of unprocessable files.
- Offline `plan` and pull-request `check` operations; admitted `sync`, `reconcile` and
  `rebuild` publication to an existing Azure AI Search index with leased Blob control state.
- Azure Pipelines templates for offline validation and protected federated publishing.
- The index consumer contract moved to `docs/index-contract.json`; example configurations and
  a platform schema example live in `examples/`.
