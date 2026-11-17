# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Report it privately to the maintainers
of the repository you received this from (for example through its private security advisory or
the maintainers' contact named in your delivery), with the affected version, a description and,
where possible, steps to reproduce. Never include real document content, credentials or tokens in a
report.

## Security model in brief

- The worker never creates or changes Azure resources, schemas or role assignments.
- Publishing needs an explicit admission, a pinned schema hash, and a federated identity
  (no keys, secrets or personal access tokens). Ambient OpenAI keys are rejected.
- Pipeline runs for pull requests execute only offline checks, with no Azure access or secrets.
- Source Markdown is treated as data: its code is never executed and its links and includes are
  never fetched. Logs contain run IDs and counts, not document text, vectors or credentials.
- Control state is stored in a private Blob container and holds only IDs, hashes, commits and counts.

## Operator responsibilities

- Protect the worker repository's main branch with required review: pipelines run its code with
  write access to the search index.
- Grant the identity only the four scoped roles described in the
  [deployment guide](docs/deployment.md) and remove unrelated roles.
- Keep local configuration, admission records and run evidence out of source control
  (`.local/` is ignored).
- Confirm that everyone who can search the index may read the indexed content; repository access
  does not establish search access.
