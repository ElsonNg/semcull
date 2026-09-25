---
name: semcull
description: Classify noisy tool output against explicit expectations with Semcull, returning scoped evidence while keeping bulk text out of the reasoning context. Prefer deterministic parsing for exact checks.
---

# Semcull (Semantic Cull)

## When to use

- Logs: authentication failure, connection failure, or recovery
- Tests: assertion failure, setup failure, or tests not run
- API messages: temporary rejection or request needing correction
- Parsed documents: API-key authentication, OAuth, or both
- Search results: actual instructions or mere mentions
- Batch logs: completion, partial processing, or interruption
- Use when interpreting noisy text helps answer a narrow question, not after every tool call.

## When not to use

- Exact fields, strings, counts, or exit codes: use `jq`, `grep`, or direct comparisons.
- Short responses that already answer your question: `{"status":"completed"}`, `3 passed, 0 failed`, or `Error: file not found`. Read these directly; don't call Semcull just to restate them. This does not apply to an isolated line from a longer, mixed output.
- Diagnosis, remediation, summaries, or next-action decisions: keep reasoning in the agent.
- Raw PDFs, images, or binaries: parse first.

## Before piping

- **Credentials:** Export `TYPESAFE_API_KEY`, or set it in `.env` in Semcull's install directory. An exported value takes precedence. Never include the key in source text, intent, or command arguments.
- **Session storage:** Set `SEMCULL_SESSION_ID` to the stable ID supplied by the agent harness for every Semcull call. Keep it constant during the session and unique for each agent session. Without it, captures share the per-user temp store.
- **Privacy:** selected source and intent go to external Jev; automatic full-input mode may send everything. No automatic PII/secret detection or redaction.
- Check for personal identifiers, emails, account IDs, credentials, and confidential content. Filter/redact upstream. Follow data policy; ask if permission is unclear. Use local parsing when external processing is prohibited.
- **GB-scale output:** limit retrieval upstream using bounded pages plus cursors, or targeted ranges. Do not automatically fetch every page.
- Semcull saves all supplied bytes locally. Tail/window limits only bound inspection; count-only retention does not bound disk usage. `--only next` is not an API cursor.

## Check

```bash
some-command 2>&1 | semcull check \
  --question "What deployment outcome is reported?" \
  --expect complete="Deployment completed successfully." \
  --expect rolled_back="Deployment failed and rollback completed."

# Classify an existing test log.
semcull check --file test.log \
  --question "What failure does the output report?" \
  --expect assertion="A test assertion failed." \
  --expect setup="Test setup failed before execution."

# Inspect one more window using IDs returned by the previous check.
semcull check OBS_ID --eval EVAL_ID --only next
```

- Supply one question and distinct expectations; expected failures are valid findings.
- Alternatively use `--spec intent.json` containing `question` and an `expectations` map. Do not combine both forms.
- Built-ins: `insufficient_evidence`, `ambiguous`, `other`. Do not redefine them.
- Semcull does not execute the producer or infer its exit status.

## Inspect further

- Next window: `semcull check OBS_ID --eval EVAL_ID --only next`. Moves backward through unexamined text; continue using the newly returned evaluation ID.
- Revisit: `semcull check OBS_ID --eval EVAL_ID --only lines:200-400`.
- Raw source: `semcull show OBS_ID --lines 200:400` or `--bytes 0:4096`. Lines are one-based/inclusive; bytes zero-based/end-exclusive. Follow truncation suggestions; `--all` is uncapped.
- Saved results: `semcull result OBS_ID --eval EVAL_ID`. Uncapped JSON; retrieve when compact results omit details.
- Cleanup: `semcull delete OBS_ID`. `delete --all` removes every observation in the selected store. With `SEMCULL_SESSION_ID`, that is this session's store; without it, the per-user store is shared.

## Interpret correctly

- Read outcome, evidence, and coverage together. Partial inspection cannot establish whole-source absence; a captured page is not the entire dataset.
- `completed` means requested work finished, not producer success or resolved uncertainty. Uncertain evaluations count as examined; revisit explicitly, not until a preferred answer appears.
- Provider errors are not source outcomes. Do not silently rerun a producer to recover missing observations.
- Evidence refers to supplied text, including any upstream redaction. Classification is not independent verification or authorization to act.
