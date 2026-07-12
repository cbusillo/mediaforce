# Advisor Routing and Evaluation

Mediaforce routes bounded advisor work by task instead of treating one model as
a product invariant. The default mapping is checked in, overrideable through
configuration, and activated only when the media-safe evaluation suite passes.

## Authority boundary

Models may interpret ambiguous language, combine supplied evidence, propose an
allow-listed policy fragment, and explain tradeoffs. They never become the
authority for measurements, stream budgets, cadence transforms, target-size
search, quality floors, or operator approval.

The following paths are deterministic and do not invoke a model:

- explicit operator-note patterns that the local parser can classify safely
- non-positive video-budget rejection
- run-verdict summaries derived from typed size and quality results
- structural tuning self-checks and audio-tradeoff guardrails

If a model request fails, Mediaforce returns a non-queueable operator-visible
failure. It never accepts unvalidated text as an executable policy.

## Default routes

| Task | Primary | Bounded fallback | Notes |
| --- | --- | --- | --- |
| Operator-note parse | `gpt-5.6-luna` | `gpt-5.6-terra` | Used only after deterministic extraction cannot classify the note. This low-risk route cannot reach Sol. |
| Seed policy | `gpt-5.6-terra` | `gpt-5.6-sol` | Sol is recorded escalation for a failed Terra attempt, not the default. |
| Note tuning | `gpt-5.6-terra` | `gpt-5.6-sol` | Deterministic policy normalization and review gates remain authoritative. |
| Review-artifact critique | `gpt-5.6-terra` | `gpt-5.6-sol` | Receives only bounded review artifacts and supplied metadata. |

Routes live under `[advisor.routes]` in `config/defaults.toml`. A local config
may replace model identifiers or the Codex Lab command without changing code:

```toml
[advisor]
command = "codex-lab"
telemetry_max_records = 5000
# auth_profile = "mediaforce"

[advisor.routes.operator_note_parse]
models = ["gpt-5.6-luna", "gpt-5.6-terra"]

[advisor.routes.seed_policy]
models = ["gpt-5.6-terra", "gpt-5.6-sol"]
```

Optional `[advisor.model_pricing.<model>]` entries may provide
`input_usd_per_million`, `cached_input_usd_per_million`, and
`output_usd_per_million`. Without explicit pricing, telemetry records token
usage but does not invent a monetary estimate.

The web app resolves this configuration at startup; restart `mediaforce-web`
after changing command, authentication, route, or pricing settings.

## Codex Lab boundary

The adapter runs `codex-lab exec` with an ephemeral session, ignored user
configuration and rules, a read-only empty temporary work directory, JSONL
events, and an output schema for structured tasks. The prompt is sent on stdin.

Review images are copied into the temporary work directory with generic names.
The original machine-local path is never placed in the command. Mediaforce
accepts a result only when all of these conditions hold:

- Codex Lab exits successfully
- a terminal `turn.completed` event is present
- no tool-use item was emitted
- the final agent message is present
- structured tasks return one exact JSON object

Raw non-event output, incomplete turns, tool use, malformed JSON, timeouts, and
provider failures are rejected. Only provider/model response failures can move
to the next configured model. Missing local images, unsupported image formats,
an unavailable command, and local transport failures stop immediately because
another model cannot repair them. A timeout terminates the complete Codex Lab
process group so shell shims or provider children cannot continue in the
background.

`cbusillo/codex-lab#319` tracks a future lightweight structured-request command.
When that command lands, Mediaforce should replace only this adapter and verify
the token and latency improvement; routing policy and advisor contracts should
not change.

## Privacy and retention

Before prompt assembly, Mediaforce removes machine-local path fields, relative
media names, raw fingerprint envelopes, raw prior responses, and image lists.
Absolute paths and email addresses embedded in necessary operator text are
redacted. Images are supplied only as temporary generic copies.

Local advisor telemetry contains task/model/prompt version, status, latency,
token usage, optional estimated cost, fallback reason, image count, input size,
and sanitized evidence field names. It does not contain prompts, operator-note
text, model output, raw frames, audio, image paths, or machine-local media paths.
The bounded JSONL file is written under the configured web-state directory as
`advisor-routing.jsonl` and defaults to the most recent 5,000 attempts.

Codex Lab runs with `--ephemeral`, so Mediaforce does not request local session
persistence. Provider-side request retention is outside Mediaforce and follows
the policy of the configured Codex Lab authentication/provider account.

## Evaluation gate

Run the checked-in synthetic corpus with the default candidate mapping:

```bash
uv run python -m mediaforce.advising.evals \
  --output /tmp/mediaforce-advisor-eval.json
```

Use `--model <model>` to compare one candidate across every model-backed case,
or repeat `--case <case-id>` for a bounded subset. The recommended run exercises
the checked-in primary and bounded fallback route; a model override intentionally
uses only that candidate so failures are not hidden by fallback. The corpus covers ambiguous
intent, runtime and absolute sizing, explicit constraint preservation,
grain/noise, cadence, dark gradients, motion, animation cues, audio priorities,
measured retries, arithmetic infeasibility, multimodal critique, and
deterministic outcome summaries. Synthetic review images are generated at run
time; no runtime media or historical operator text is checked in.

Activation requires a 100% pass rate for the recommended suite. Checks cover
schema-valid completion, required response fields, explicit constraint
preservation, forbidden unsupported claims, deterministic bypass results,
telemetry validity, and usage availability. Latency, token usage, fallback, and
optional configured cost are reported per case but are not converted into
invented monetary values. The summary also reports how many cases needed a
fallback; a nonzero count must be reviewed before changing a primary mapping,
even when the bounded route still passes.

The report stores check outcomes and aggregate telemetry only. It intentionally
does not retain prompts, operator notes, model responses, or generated images.

## Activated evaluation

The recommended suite was last verified at `2026-07-12T03:52:19Z` using Codex
Lab and the checked-in routes. All 11 cases passed, including both deterministic
cases, and no case required fallback.

| Primary model | Cases | Observed latency | Input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: |
| `gpt-5.6-luna` | 2 | 12.4–13.3 s | 28,446 | 323 |
| `gpt-5.6-terra` | 5 | 13.4–30.3 s | 85,351 | 2,963 |
| `gpt-5.6-sol` | 2 | 18.1–23.8 s | 31,965 | 1,008 |

No model pricing was configured, so the report correctly left estimated USD
cost unset. The roughly 14,000–18,000 input tokens per model-backed case include
the full Codex Lab exec harness. This validates the current adapter while also
supporting the lightweight side-channel work tracked in `codex-lab#319`.
