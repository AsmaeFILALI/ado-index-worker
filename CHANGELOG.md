# Changelog

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
