# hermes-jev-tool-gate

> **Status: archived — closed September 2026.** Not recommended for use. It works and
> passes `hermes plugins validate` / `doctor`, but it was closed after checking the field:
> the approve/deny gate it implements is redundant with Hermes's own layered approval path
> (`approval_detection.py` patterns + the injection-hardened `approval_smart.py` reviewer +
> `path_security.py`), and at least two plugins cover the same ground with more capability —
> [`hermes-jev-guard`](https://github.com/rubichandrap/hermes-jev-guard) (same three-band
> escalation, plus a plan, done-check and tier middleware) and
> [`jev-approvals`](https://github.com/anpicasso/hermes-jev-approvals) (the right way: it
> *replaces* the smart reviewer instead of stacking a hook on top).
>
> Measurements that motivated the close are kept below because they are still useful:
> the calibration gap (p≈0.95 authorized vs p≈0.01 unauthorized), ~$0.000018 per call,
> the 7.5 s latency tail, and the ~3% intermittent Cloudflare 403.
>
> If you want a Jev tool-risk gate today, use `hermes-jev-guard`. If you want Jev at all,
> the better shapes are typed classification behind a deterministic floor — see the
> comparison section.

Gate [Hermes Agent](https://github.com/NousResearch/hermes-agent) tool calls with
**Jev**, TypeSafe AI's System One decision model, over the OpenRouter System One /
Decisions API.

Jev is not an LLM. It takes unstructured state plus typed questions and returns typed
answers with probabilities — no prose, no reasoning, no tool calls. That makes it a
good fit for one narrow job: *is this proposed tool call actually authorized by what
the user asked for, and how bad is it if it isn't?*

No new credential: it runs on the `OPENROUTER_API_KEY` you already have. It does not
need a TypeSafe account.

> **Read the comparison section before installing.** This is a small plugin in a crowded
> field. Several Jev plugins for Hermes are more capable than this one, and two of them
> already cover most of what it does. That section is the honest placement, verified
> against the catalog and GitHub rather than assumed.

## How it works

Two hooks, because neither is sufficient alone:

| Hook | Why |
|---|---|
| `pre_llm_call` | Captures the user's request and recent turns. The `pre_tool_call` payload carries **no conversation**, so without this Jev would have no state to judge a call against. |
| `pre_tool_call` | Asks Jev about the proposed call, then returns `allow` (no directive), `{"action": "approve"}` (human approval gate), or `{"action": "block"}`. |

Jev is asked two questions in one round trip:

- `authorized` — a **noul** (boolean probability): does the user's request authorize
  this exact tool call?
- `risk` — a **score** (0–3): how bad is it if this is wrong?

Three-way decision:

```
p >= accept_threshold   (0.60)  -> allow silently
p >= escalate_threshold (0.35)  -> {"action": "approve"} -> Hermes's native approval gate
p <  escalate_threshold         -> {"action": "block"}, numbers included in the tool result
```

The middle band means an ambiguous call pauses for a human rather than being refused —
and an `[a]lways` approval is remembered per tool via `rule_key`.

## Measured behaviour

Numbers are from live calls during development, not from the launch claims.

- **Calibration is very sharp.** Authorized calls scored **p=0.95–0.96**; unauthorized
  ones **p=0.01–0.04**. The gap means the thresholds are not sensitive — you will not be
  tuning 0.55 vs 0.60. (Independently reproduced by
  [pi-jev-auto-mode](https://dev.to/jomatsu/jev-pi-a-probability-gate-for-my-coding-agents-shell-commands-95d),
  which measured the same bimodal 0.77–0.98 / 0.06–0.15 split and put its threshold at
  0.60 — the same value this gate defaults to.)
- **Cost:** ~**$0.000018** per gated call (input metered, output free). A thousand gates
  is about two cents.
- **Latency:** median **~400 ms**, but the tail is real — observed up to **7.5 s**. The
  default timeout is 8 s for that reason. A 3 s timeout measurably failed live calls.
- **Reliability:** ~**3%** of calls come back as a Cloudflare 403 HTML page. It is
  intermittent edge noise, not rate limiting — 20 rapid-fire and 30 spaced calls all
  succeeded. Retried with a small backoff, then against a second endpoint surface.
- **Blocked calls cannot be cleared by retrying.** Jev is near-deterministic for
  identical input, so the block message says so explicitly. Without that, an agent burns
  turns re-issuing the same call — observed on a live run, then fixed.

## Install

```bash
git clone https://github.com/alex-power/hermes-jev-tool-gate
cp -r hermes-jev-tool-gate "$HERMES_HOME/plugins/jev-tool-gate"

hermes plugins validate plugins/jev-tool-gate   # admission checks
hermes plugins doctor  plugins/jev-tool-gate    # real runtime contracts
hermes plugins enable  jev-tool-gate
```

`hermes plugins enable` asks whether to grant tool-override privileges. **Decline** —
this plugin registers hooks only and does not need to intercept built-in tools.

Plugins load at process start, so a running gateway picks this up on its next restart; a
`hermes chat -q ...` invocation picks it up immediately.

Then verify it is live:

```bash
hermes chat -q "Run the shell command: echo jev-gate-test"
tail -f "$HERMES_HOME/logs/jev-tool-gate.jsonl"
```

## Why observe mode is the default

`mode: observe` evaluates and logs every gated call but never blocks or escalates. That
is deliberate: it is how you collect evidence about your own traffic before letting a
probability threshold change agent behaviour. Flip to `enforce` when the log shows the
verdicts you would have made yourself.

Worth knowing before you do: a comparable gate
([hermes-jev-guard](https://github.com/rubichandrap/hermes-jev-guard)) reported 38 scored
tool calls with **0 escalations and 0 blocks, mean risk 0.057** — on real coding traffic
the gate essentially never fires. A quiet log is the expected outcome, not a sign the
plugin is broken.

## Configuration

Settings live under `plugins.entries.jev-tool-gate.settings` in `config.yaml`:

```bash
hermes config set plugins.entries.jev-tool-gate.settings.mode enforce
```

Defaults:

```yaml
mode: observe              # observe | enforce
model: typesafe/jev-1.13   # pin the dated build; jev-latest re-calibrates silently
accept_threshold: 0.60
escalate_threshold: 0.35
timeout_s: 8.0
retries: 1
retry_backoff_s: 0.25
cache_ttl_s: 300.0
breaker_threshold: 3
breaker_cooldown_s: 120.0
fail_mode: open            # open | closed  (behaviour when Jev is unreachable)
max_history_turns: 6
gated_tools:
  - terminal
  - write_file
  - patch
  - browser_exec
  - computer_use
  - cronjob_manage
  - delegate_task
log_decisions: true
```

Only `gated_tools` are ever sent to Jev. Everything else — `web_search`, `read_file`,
`skill_view` — is never evaluated, so read-only traffic pays no latency and no cost.

### What leaves the machine

For a gated call only: the user's latest message, up to `max_history_turns` prior
user/assistant turns (truncated to 800 chars each), and the proposed tool name and
arguments (truncated to 2000 chars). Never the system prompt, never tool results, never
file contents, never credentials. Requests go to `openrouter.ai`, which forwards them to
TypeSafe AI as the model provider.

## The decision log

Every evaluation appends a line to `$HERMES_HOME/logs/jev-tool-gate.jsonl`:

```json
{"ts": 1758633600.1, "mode": "observe", "tool": "terminal", "verdict": "block",
 "probability": 0.01, "risk": 3, "blind": false, "cached": false,
 "cost_usd": 1.8e-05, "latency_ms": 412}
```

`blind: true` marks a fail-open — the gate saw nothing because Jev was unavailable, the
breaker was open, or no request had been captured. **A silent fail-open is a blind spot,
not a safeguard**, so those are logged rather than hidden. Read the log before trusting
`enforce` mode.

## Fail modes

When Jev cannot be reached the gate either:

- `fail_mode: open` (default) — allow the call and record a blind entry. Keeps work
  moving; the gate is a fuzzy admissibility layer, not the security boundary. Hermes's
  own approvals and guardrails still apply underneath.
- `fail_mode: closed` — block the call. Use where an unevaluated call is worse than a
  refused one.

A circuit breaker opens after `breaker_threshold` consecutive failures and stays open for
`breaker_cooldown_s`, so a flaky far end does not add latency to every tool call.

Fail-open is the deliberate default in every comparable implementation, including
QuantDinger's live-trading gate ("the gate is an enhancement layer, and its outage should
not halt the whole trading system") — and it is what this gate does.

## How this compares to other Jev plugins

Plainly: **the architecture here is not novel, and it is not the most capable option.**
The Jev ecosystem around Hermes is large — the live catalog lists 285 entries, 14 of them
Jev/TypeSafe related, and a GitHub search finds 17+ Hermes+Jev repositories. Several do
what this does, and at least two do more.

**The closest match already ships what this plugin's headline feature is.**
[`hermes-jev-guard`](https://github.com/rubichandrap/hermes-jev-guard) has a
`pre_tool_call` risk gate with **`approve_at: 0.7` → human approval prompt and
`block_at: 0.97` → block** — the same three-band escalation, on the same hooks, with the
same 8-second timeout, plus `risk_tools` scoping, a `pre_llm_call` plan injected per turn,
a `pre_verify` done-check, an `llm_request` model-tier middleware, a JSONL flow log, and a
shell-hooks path that works without the plugin. It uses `TYPESAFE_API_KEY`. If you want a
tool-risk gate today, **use that one.**

[`DoGMaTiiC/hermes-jev`](https://github.com/DoGMaTiiC/hermes-jev)'s `jev-judge` is the
simpler twin: a `pre_tool_call` gate on `terminal`/`write_file`/`patch` with
`shadow`/`enforce` modes, fail-open, thresholds in code, JSONL logging, and a single
retry that honours `Retry-After`. Adapts [pi-jev](https://github.com/y0usaf/pi-jev).

[`kennedy-f/hermes-jev-decision-layer`](https://github.com/kennedy-f/hermes-jev-decision-layer)
(plugin `jev-decision-gate`) already uses **OpenRouter's Decisions endpoint, the
`typesafe/jev-1.13` model and `OPENROUTER_API_KEY`** — so OpenRouter transport is not
unique here either. It is deliberately shadow-only: its hooks always return `None` and it
"never changes whether Hermes runs a tool."

**A different, arguably better-integrated approach to the adjacent problem:**
[`anpicasso/hermes-jev-approvals`](https://github.com/anpicasso/hermes-jev-approvals)
(MIT, v0.3.0, 14★) does not add a hook at all. It replaces Hermes's
`auxiliary.approval` reviewer under `approvals.mode: smart`, registering no hooks and
working with core as shipped — so it gates *exactly the commands core already flags*
instead of re-implementing detection. It reports measured results (8.7× faster, 4.4× fewer
prompts on 153 real commands) and asks six typed questions including `self_advocating`, a
prompt-injection check on whether the command argues for its own approval. **If your
interest is approvals, start there.**

**The ecosystem flagship:** [`kerpopule/hermes-jev-skills`](https://github.com/kerpopule/hermes-jev-skills)
(697★) uses Jev for model routing, memory filtering, compaction, skill selection, search,
triage, mail sorting and computer/browser use — via `pre_llm_call`, `llm_request`
middleware, tools and a slash command. It does not do tool-risk gating, so it is adjacent
rather than competing, but it is where the mature Jev-on-Hermes practice lives.

Others worth knowing: `ajensenwaud/hermes-jev-plugin` (four decision tools),
`ourines/hermes-jev` (explicit tools, "no automatic tool gating"), `Pinutss/jev-*`
(skill + MCP routers for agents/models/memory), `rsdkrasen/hermes-jev-router`
(tool-result compaction), `scursel/hermes-jev-fastpath` (`llm_request`/`tool_execution`
middleware fast paths), `litshing/hermes-jev-plugins` (context pruning, memory gating),
`AlphaPerseii3000/jev-effort-router` (`llm_request` model routing).

### What is actually, still, different here

After checking the field, the honest residue is small:

- **OpenRouter credential *plus* enforcement.** Other OpenRouter-transport work is
  shadow-only or advisory; other enforced gates want `TYPESAFE_API_KEY` or
  `AI_GATEWAY_API_KEY`. If you have an OpenRouter key and no TypeSafe account, and you
  want a gate that actually escalates and blocks, this is currently the narrow gap it
  fills.
- **Conversation-aware state.** `pre_llm_call` intent capture feeding the gate, so Jev
  judges a call against the request *and prior turns*, not just the current tool call.
- **Documented reliability engineering**, measured rather than asserted: the 8 s timeout
  (tail latency to 7.5 s), retry-with-backoff for the intermittent Cloudflare 403, the
  circuit breaker, the logged fail-opens, and the no-point-retrying block message.

### What this should adopt from the alternatives

- **Question decomposition.** This plugin asks one authorization question plus a risk
  score. The evidence says that is the weaker design:
  [jev-harness-lab](https://github.com/Aitejiu/jev-harness-lab) measured orthogonal
  decomposition plus code composition beating a single fuzzy question (shell-gate false
  positives 14.5% → 1.8%); `jev-approvals` asks six questions; `hermes-jev-guard` asks one
  sharper one (destructive/irreversible?) rather than a general authorization noul.
  Splitting `authorized` into independent conditions — intent coverage, blast radius,
  secret exposure, outbound transmission, self-advocacy — is the obvious next step, and
  the reason to treat this as v0.1.
- **`self_advocating`.** Nothing here defends against a command that argues for its own
  approval. `jev-approvals` and `kerpopule/hermes-jev-skills` (its injection screen) both
  treat that as first-class.
- **Redaction.** `kerpopule/hermes-jev-skills` redacts emails, phones, tokens and long hex
  before anything leaves the machine, and refuses to send states that look like
  credentials. This plugin clips length but does **not** redact. `hermes-jev-guard`'s
  approach — score one sharp question and scope `risk_tools` tightly — also limits what
  leaves.

## Caveats

- **Jev cannot explain itself.** It returns probabilities only. Every block message is
  assembled here from the numbers and the question text, and says so. The agent sees the
  block as the tool result and can tell the user, who can then confirm.
- **Jev judges, it does not choose.** This constrains calls after the model has chosen
  them. It cannot make the model pick a better tool, and it cannot rewrite arguments. If
  the real problem is bad tool selection, the lever is tool descriptions and skill
  routing, not a post-hoc gate.
- **Not a security boundary.** A calibrated second opinion. Approvals, guardrails and the
  sandbox remain the enforcement layer.
- **It may rarely fire.** A comparable gate measured 0 escalations and 0 blocks across 38
  real tool calls (mean risk 0.057). Expect a quiet log.
- **No redaction.** Bounded length only; see "What leaves the machine".
- **The thresholds are untuned on your traffic.** The calibration gap is wide enough that
  0.60/0.35 handled every case measured here, but that is one machine. Run in `observe`
  and check the log.
- **Plugin settings are read once per process**; changing them needs a restart. Enabling
  affects newly started processes — a running gateway picks it up on its next restart.

## Testing

`test_gate.py` drives the real hook callbacks against the live API — no mocks — covering
destructive commands, out-of-scope writes, credential exfiltration, allowed calls, the
cache, fail-open with the circuit breaker, and fail-closed mode:

```bash
cd "$HERMES_HOME/hermes-agent"
set -a && . "$HERMES_HOME/.env" && set +a
HERMES_HOME="$HERMES_HOME" venv/Scripts/python.exe ../plugins/jev-tool-gate/test_gate.py
```

## License

MIT — see [LICENSE](LICENSE).
