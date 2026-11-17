# Retrieval, citation and access evaluation

Ingestion correctness, schema compatibility, retrieval quality and user
authorization are separate verdicts. The worker does not answer chat questions
or implement an audience/ACL policy. A passing ingestion run cannot establish
allowed/denied-user enforcement.

## Original offline fixture

[tests/fixtures/evaluation.json](../tests/fixtures/evaluation.json) contains four
original questions mapped to deterministic expected section/document IDs in
[synthetic-guide.md](../tests/fixtures/markdown/synthetic-guide.md). It fixes an
explicit synthetic repository ID, full ref and source path. These identities
matter: loading the same text under another repo/ref/path produces different
IDs. The duplicate heading cases intentionally resolve to different sections.

Run:

```text
uv run --locked pytest -q tests/test_evaluation.py
```

The test commits the guide in a temporary Git repository, parses the immutable
snapshot and checks exact expected section/document IDs, heading/content
expectations, bounded chunks and commit-pinned citation query parameters.
This proves fixture consistency and deterministic transformation **only**.
It does not query Azure, execute document examples, or demonstrate retrieval
ranking/answer accuracy.

## Separately authorized environment evaluation

Owners must first supply a non-serving target, approved schema/model configuration,
source/admission identity, explicit Azure access, representative corpus questions
and an approved test audience. Use original synthetic content until protected-
document authorization requirements and enforcement are implemented.

1. Publish the exact admitted synthetic source snapshot and verify documents/
   checkpoint/lease outcomes. Record the source identity and expected IDs.
2. Through the actual retrieval runtime (for example a Foundry IQ knowledge base or your
   own query application), issue each approved
   question using authorized test identities. Do not select vectors into the
   model context. Record safe retrieved section IDs/ranks, counts and timings.
3. Compare the returned sections with the approved expected set at the owner-
   selected cutoff. Record unexpected/missing/stale sections as failures, not
   fabricated matches based solely on similar text.
4. Review whether the answer is grounded in the returned source and whether
   unsupported assertions are present. The fixture's expected text is an evidence
   anchor, not permission to claim an answer score without a real runtime result.
5. Open citations with an authorized browser identity. Confirm exact repository,
   encoded path and immutable revision. Check line/section navigation only if the
   runtime actually implements and validates it; do not assume GitHub anchors.
6. Exercise both permitted and denied users, cross-scope documents, changed/
   removed permissions and the same questions after a source edit/rename/delete.
   Admission strings and copied repository metadata are not user enforcement.
7. Test no-change schedules and freshness against an agreed observation window.
   Promotion/cutover/rollback is a separately approved platform exercise.

The application currently normalizes retrieval references to title/link/content;
structured provenance in Search is not proof that those fields reach chat
responses. Capture index-level and user-visible evidence separately.

## Threshold ownership and evidence

Content owners approve question relevance and expected sections. Application/
platform owners approve cutoff, retrieval/grounding thresholds, citation validity,
latency/freshness objectives and sample representativeness. Identity/data owners
approve allowed/denied-user cases and protected-content handling. No production
threshold or benchmark result is invented by this repository.

For each approved test record:

| Evidence | Required contents |
| --- | --- |
| Identity/configuration | Safe run ID, source/worker commits, contract/schema/model/processing fingerprints, explicit target generation and permission-policy reference. |
| Retrieval | Question fixture ID, expected/observed safe section IDs and ranks, selected cutoff and pass/fail criterion. |
| Grounding/citations | Reviewer verdict, citation revision/path validity and unsupported-answer findings. |
| Timing/freshness | Stage/query duration, retry/visibility outcomes and time to verified current source state. |
| Authorization | Allowed/denied identity class, expected/observed result, policy revision and revocation behavior. |

Do not log corpus text, embeddings, tokens or full environments. Sanitize
screenshots or runtime transcripts under the data owner's policy before saving
them. Keep raw production answer evidence outside this public/synthetic fixture.

Current fixture checks are offline expectations, not measured Azure relevance.
Any live retrieval/access/citation verdict remains NOT RUN until the separate
authorized procedure produces evidence.
