# Semcull (Semantic Cull)

**Semcull optimizes for token-efficient workflows by routing lengthy, noisy tool output through Jev instead of putting the full output into a higher-cost reasoning model’s context.**

Given the reasoning agent’s question and expected states, Jev classifies selected portions and Semcull returns a compact observation with copied evidence. Subsequent follow ups reuse the same output, allowing for efficient, progressive disclosure.

For long output, Semcull checks the tail first, then earlier windows as requested. The reasoning agent stays in control and decides what to investigate next.

This progressive workflow nature makes it extremely suited for debugging, troubleshooting and classification use cases. [See examples below](#6-examples).

## Contents

- [Semcull (Semantic Cull)](#semcull-semantic-cull)
  - [Contents](#contents)
  - [1. Install](#1-install)
  - [2. Usage](#2-usage)
  - [3. Configuration](#3-configuration)
  - [4. Privacy and storage](#4-privacy-and-storage)
  - [5. Exit codes](#5-exit-codes)
  - [6. Examples](#6-examples)
    - [Debug startup logs](#debug-startup-logs)
    - [Inspect long output progressively](#inspect-long-output-progressively)
    - [Check a third-party API response](#check-a-third-party-api-response)
    - [Investigate a microservice rollout](#investigate-a-microservice-rollout)
    - [Ask increasingly focused questions](#ask-increasingly-focused-questions)
  - [7. Agentic Workflow](#7-agentic-workflow)
  - [Final Thoughts](#final-thoughts)

## 1. Install

Semcull requires Python 3.11+ on macOS or Linux. Install the CLI directly from GitHub:

```bash
uv tool install git+https://github.com/ElsonNg/semcull.git
```

Alternatively, install it from a cloned checkout:

```bash
git clone https://github.com/ElsonNg/semcull.git
cd semcull
uv tool install .
```

You can also use `pipx install git+https://github.com/ElsonNg/semcull.git`, or `python -m pip install .` from a checkout.

Verify the installation:

```bash
semcull --version
semcull --help
```

For development:

```bash
uv sync --group dev
uv run pytest -q
```

## 2. Usage

Semcull has four commands:

- `check` classifies piped, file or saved output against your question and expectations.
- `show` prints saved source text.
- `result` prints a saved evaluation and its evidence.
- `delete` removes one observation or all observations.

Set the Jev key in your shell:

```bash
export TYPESAFE_API_KEY="your-jev-api-key"
```

Or put `TYPESAFE_API_KEY=your-jev-api-key` in `.env` in Semcull's install directory. An exported key takes precedence. Semcull reads only this key from its install `.env`; it does not search the current directory or execute the file as a shell script.

For agent sessions, set a stable `SEMCULL_SESSION_ID` in the environment for every Semcull command in that session. Use a unique value for each agent session. Semcull hashes the ID to select a private session directory; the ID itself is not stored. Without it, manual CLI use keeps the shared per-user temp store.

```bash
some-command 2>&1 | semcull check \
  --question "What deployment outcome does this output report?" \
  --expect complete="Deployment completed successfully." \
  --expect rolled_back="Rollback completed after a deployment failure."
```

**Key behavior:** Small inputs are checked in full. For larger output, the first `check` starts at the tail. Run `--only next` to inspect one earlier, unexamined window, then repeat with the evaluation ID returned by the last check to keep working backward:

```bash
semcull check obs_<id> --eval eval_<id> --only next
```

Use `--file` for UTF-8 text, or `--spec intent.json` instead of inline expectations. Built-in outcomes include `insufficient_evidence`, `ambiguous` and `other`.

Add `--verbose` for this check's input/output tokens and attempts on stderr. Unknown usage is flagged; it is not assumed to be zero.

Semcull reads output, not the producer's exit status. A reported failure can be a successful classification.

Continue inspecting the saved output using the IDs returned by `check`:

```bash
# Examine one earlier, unexamined window.
semcull check obs_<id> --eval eval_<id> --only next

# Inspect a specific range.
semcull check obs_<id> --eval eval_<id> --only lines:200-400
semcull show obs_<id> --lines 200:400

# Retrieve full evaluation details or remove captured data.
semcull result obs_<id> --eval eval_<id>
semcull delete obs_<id>
semcull delete --all
```

- New questions start fresh evaluations: supply new intent without `--eval`.
- `--only next` advances coverage, not reasoning. Uncertain windows still count as examined.
- `show` returns raw text: last 200 lines by default, capped at 16 KiB. `--all` removes the cap.
- `result` returns uncapped saved details. `delete --all` removes every observation in the configured store, without confirmation.

Partial coverage is not a whole-output verdict. For fully examined but inconclusive input, inspect `result` or ask a more focused question.

## 3. Configuration

Defaults work without a config file. Optional TOML: `$XDG_CONFIG_HOME/semcull/config.toml` or `~/.config/semcull/config.toml`.

```toml
[storage]
max_observations = 200

[inspection]
window = 4000
```

Settings can be added under these sections:

| Section and setting | Default | What it controls |
| --- | ---: | --- |
| `storage.directory` | OS temp directory | Optional absolute base path for saved captures; with a session ID, captures go in a per-session subdirectory. |
| `storage.max_observations` | `200` | Capture count target; oldest captures are removed when new input arrives. |
| `output.show_max_bytes` | `16384` | Default maximum raw bytes printed by `show`. |
| `output.check_max_bytes` | `32768` | Maximum JSON bytes printed by `check`; oversized details are omitted with a retrieval hint. |
| `inspection.window` | `4000` bytes | Source bytes per inspection window. |
| `inspection.full_budget` | `16000` tokens | Estimated request size below which Semcull tries the full input first. |
| `inspection.request_budget` | `24000` tokens | Estimated maximum size of one Jev request. |
| `inspection.state_question_budget` | `24000` tokens | Estimated budget for source state and a question in a Jev request. |
| `provider.model` | `jev-1.13.0` | Versioned Jev model to call. |
| `provider.timeout` | `10` seconds | Timeout for each provider request. |
| `provider.min_confidence` | `0.5` | Minimum confidence to accept a classification and its evidence. |
| `limits.max_attempts` | `8` | Total provider attempts in one check, including retries. |
| `limits.parallel` | `4` | Maximum concurrent provider requests. |
| `limits.max_seconds` | `60` seconds | Wall-clock limit for one check. |
| `limits.max_tokens` | `96000` tokens | Estimated aggregate request-token budget for one check. |

Token budgets are conservative estimates, not exact tokenizer counts. Override the config with `semcull --config PATH check ...`.

## 4. Privacy and storage

- Selected text and intent go to [TypeSafe Jev](https://docs.typesafe.ai/api). Small captures may be sent in full. Redact secrets and personal data first. Redaction is not automatic.
- Original output stays in a private OS-temp store. With `SEMCULL_SESSION_ID`, each agent session has its own store; without it, commands share the per-user store. `delete --all` affects only the selected store.
- Retention targets 200 observations per store, evicting oldest captures during new-input checks.
- There is **no capture-size or total disk-size cap**. Limit or paginate gigabyte-scale output upstream.
- Eviction, deletion and OS cleanup can remove saved data. This is a temporary cache, not an archive.
- Copied evidence is traceable to the source, not proof that the source or classification is correct.

## 5. Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Check or command completed, including uncertain outcomes |
| 1 | Internal error |
| 2 | Invalid input/configuration or missing reference |
| 3 | Provider failure or execution limit |
| 4 | Filesystem failure |
| 130 | Interrupted |

`check`, `result` and `delete` emit JSON; `show` emits raw bytes. Diagnostics go to stderr. Exit 0 does not imply full coverage or producer success.

## 6. Examples

These are mocked examples demonstrating potential applications for Semcull. 

### Debug startup logs

Classify startup output and receive copied evidence:

```bash
printf '%s\n' 'ERROR Database authentication failed.' | semcull check \
  --question "What does this log report about why startup failed?" \
  --expect auth_failed="Database authentication failed." \
  --expect unreachable="The database could not be reached." \
  --expect other_failure="Startup failed for another reason."
```

<details>
<summary>Example JSON output</summary>

```json
{
  "schema_version": "1",
  "observation_id": "obs_0123456789abcdef0123456789abcdef",
  "evaluation_id": "eval_fedcba9876543210fedcba9876543210",
  "run_status": "completed",
  "results": [
    {
      "outcome": "auth_failed",
      "examined_bytes": [0, 38],
      "evidence": [
        {
          "bytes": [0, 38],
          "text": "ERROR Database authentication failed.\n"
        }
      ]
    }
  ],
  "coverage": {
    "evaluated_bytes": 38,
    "total_bytes": 38,
    "has_unexamined": false
  }
}
```

</details>

---

### Inspect long output progressively

This [1.7 MB fixture](examples/startup-long.log) puts the cause outside the initial tail window. The first check examines the tail:

```bash
semcull check --file examples/startup-long.log \
  --question "What caused startup to fail?" \
  --expect database_auth_failed="Database credentials were rejected." \
  --expect database_unreachable="The database could not be reached." \
  --expect other_failure="Startup failed for another reason."
```

<details>
<summary>Mock Output: Tail has no cause</summary>

```json
{
  "schema_version": "1",
  "observation_id": "obs_0123456789abcdef0123456789abcdef",
  "evaluation_id": "eval_0123456789abcdef0123456789abcdef",
  "run_status": "completed",
  "results": [
    {
      "outcome": "insufficient_evidence",
      "examined_bytes": [1706376, 1710376],
      "evidence": []
    }
  ],
  "coverage": {
    "evaluated_bytes": 4000,
    "total_bytes": 1710376,
    "has_unexamined": true
  }
}
```

</details>
<br>

Use the returned IDs to inspect the preceding window:

```bash
semcull check obs_<id> --eval eval_<id> --only next
```

<details>
<summary>Mock Output: Earlier window finds the cause</summary>

```json
{
  "schema_version": "1",
  "observation_id": "obs_0123456789abcdef0123456789abcdef",
  "evaluation_id": "eval_fedcba9876543210fedcba9876543210",
  "run_status": "completed",
  "results": [
    {
      "outcome": "database_auth_failed",
      "examined_bytes": [1702376, 1706376],
      "evidence": [
        {
          "bytes": [1704056, 1704296],
          "text": "8a1538295e573771584b2a status=loaded\nINFO Connecting to database host=db.internal port=5432\nERROR Database authentication failed for user checkout: credentials rejected.\nERROR Startup aborted after database initialization failed.\nDEBUG Shut"
        }
      ]
    }
  ],
  "coverage": {
    "evaluated_bytes": 8000,
    "total_bytes": 1710376,
    "has_unexamined": true
  }
}
```

</details>

---

### Check a third-party API response

Pipe a public weather response directly from `curl`. This Open-Meteo example needs no API key and asks whether its current precipitation measurement is zero or above zero. Semcull classifies the response body. It does not interpret the HTTP status or curl's exit code.

```bash
set -o pipefail
curl --silent --show-error --fail-with-body \
  'https://api.open-meteo.com/v1/forecast?latitude=52.52&longitude=13.41&current=precipitation,temperature_2m,weather_code&timezone=Europe%2FBerlin' |
semcull check \
  --question "What does the current weather data report about precipitation?" \
  --expect precipitation="The current precipitation value is greater than zero." \
  --expect no_precipitation="The current precipitation value is zero." \
  --expect unknown="The precipitation field is missing or cannot be interpreted."
```

Note: Avoid sending secrets or personal data to Jev. Semcull does not redact the response automatically.

<details>
<summary>Example JSON output</summary>

```json
{
  "schema_version": "1",
  "observation_id": "obs_abcdef0123456789abcdef0123456789",
  "evaluation_id": "eval_abcdef0123456789abcdef0123456789",
  "run_status": "completed",
  "results": [
    {
      "outcome": "no_precipitation",
      "examined_bytes": [0, 113],
      "evidence": [
        {
          "bytes": [0, 113],
          "text": "{\"current\":{\"time\":\"2026-09-26T12:00\",\"interval\":900,\"temperature_2m\":17.2,\"precipitation\":0.0,\"weather_code\":3}}"
        }
      ]
    }
  ],
  "coverage": {
    "evaluated_bytes": 113,
    "total_bytes": 113,
    "has_unexamined": false
  }
}
```

</details>

---

### Investigate a microservice rollout

Check a [microservice rollout](examples/deployment.log), then investigate database permissions amid authentication and service traffic.

```bash
# Check the outcome.
semcull check --file examples/deployment.log --spec examples/deployment-intent.json --verbose

# Investigate the same capture.
semcull check obs_<id> --spec examples/deployment-diagnosis-intent.json --only lines:109-180 --verbose
```

---

### Ask increasingly focused questions

Reuse a [conversation with billing and login problems](examples/customer-support.log) for progressively focused questions:

```bash
# 1. Identify the main concern.
semcull check --file examples/customer-support.log \
  --question "What concern brought the customer to support?" \
  --expect billing="The customer reports a billing or payment concern." \
  --expect login_problem="The customer cannot access their account." \
  --expect delivery_problem="The customer reports a delivery problem."

# 2. Investigate payment details using the same observation.
SUPPORT_OBS="obs_<id>" # Replace with the observation ID from level 1.
semcull check "$SUPPORT_OBS" \
  --question "What payment-entry statuses does the customer report seeing?" \
  --expect completed_and_pending="One entry is completed and the other is pending." \
  --expect both_completed="Both entries are completed." \
  --expect both_pending="Both entries are pending."

# 3. Establish the requested next step.
semcull check "$SUPPORT_OBS" \
  --question "What next action does the customer explicitly request?" \
  --expect verify_payment="Verify the payment status before deciding on a remedy." \
  --expect refund="Issue a refund now." \
  --expect cancel="Cancel the subscription."
```

Each new question reuses the capture, not previous answers or coverage. The agent chooses the next step.

## 7. Agentic Workflow

Use the optional [agent skill](skills/semcull/SKILL.md) to teach your harness when and how to use Semcull. Installing the package does not register the skill automatically.

## Final Thoughts

Semcull is still new! If you have any suggestions, feel free to reach out or submit a PR.
