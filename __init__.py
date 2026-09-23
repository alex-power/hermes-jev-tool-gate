"""Jev-backed tool-call gate.

Two hooks, one decision engine:

- ``pre_llm_call``  — captures the user's request for the current turn. The
  ``pre_tool_call`` payload carries no conversation, so Jev would have no state to
  judge a tool call against without this.
- ``pre_tool_call`` — asks Jev whether the proposed call is authorized by that
  request, and how risky it is, then allows / escalates / blocks.

Jev returns probabilities only (no prose, no reasoning), so every block and
escalation message is assembled here from the numbers and the question text.

Reliability notes, measured against the live API rather than assumed:

- Median latency ~400 ms, but the tail reaches 2.3 s, so the timeout is generous.
- The endpoint occasionally answers with a Cloudflare 403 HTML page, so transient
  failures are retried once against a second surface before the gate gives up.
- A gate that calls out on every tool call must not add latency while the far end
  is down: consecutive failures open a short circuit breaker.
- Every fail-open path is recorded in the decision log. A silent fail-open is a
  blind spot, not a safeguard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Stable v1 surface first, alpha second: identical request/response schema, and the
# v1 path is the one the provider treats as supported.
SYSTEM_ONE_URL = "https://openrouter.ai/api/v1/systemone"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

_RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}

DEFAULTS: Dict[str, Any] = {
    # observe = evaluate and log only; enforce = actually allow/escalate/block.
    "mode": "observe",
    # Pin the dated build. Jev silently re-calibrates across releases, and a moving
    # threshold under a fixed accept value is the failure you cannot see in a dashboard.
    "model": "typesafe/jev-1.13",
    "accept_threshold": 0.60,
    "escalate_threshold": 0.35,
    "timeout_s": 8.0,
    "retries": 1,
    "retry_backoff_s": 0.25,
    "cache_ttl_s": 300.0,
    "breaker_threshold": 3,
    "breaker_cooldown_s": 120.0,
    # What to do when Jev cannot be reached at all. "open" keeps work moving and
    # logs the gap; "closed" refuses the call.
    "fail_mode": "open",
    "max_history_turns": 6,
    "gated_tools": [
        "terminal",
        "write_file",
        "patch",
        "browser_exec",
        "computer_use",
        "cronjob_manage",
        "delegate_task",
    ],
    "log_decisions": True,
}

_AUTHORIZED_CRITERIA = {
    "true": (
        "The user explicitly asked for this, or it is a direct and obvious step toward "
        "exactly what they asked for, and the call is read-only or trivially reversible."
    ),
    "false": (
        "The user did not ask for this, it exceeds or drifts from the request, it is "
        "destructive or hard to undo, or it touches systems unrelated to the request."
    ),
}

_RISK_CRITERIA = [
    "Read-only or fully safe to undo",
    "Reversible with minor effort",
    "Hard to undo, or visible to other people",
    "Destructive, irreversible, or affects production systems",
]


@dataclass
class Decision:
    """The gate's verdict for one proposed tool call."""

    verdict: str  # "allow" | "approve" | "block" | "skip"
    probability: Optional[float] = None
    risk: Optional[int] = None
    reason: str = ""
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    cached: bool = False
    endpoint: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def blind(self) -> bool:
        """True when the verdict is a fail-open, not an actual judgement."""
        return self.verdict == "skip" and self.probability is None


class _IntentStore:
    """Last captured user request per (profile home, session).

    Bounded and lock-guarded: one gateway process serves several profiles, and the
    plugin must never grow without limit in a long-lived process.
    """

    def __init__(self, max_entries: int = 512) -> None:
        self._entries: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def put(self, key: Tuple[str, str], payload: Dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = payload
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def get(self, key: Tuple[str, str]) -> Optional[Dict[str, Any]]:
        with self._lock:
            payload = self._entries.get(key)
            if payload is not None:
                self._entries.move_to_end(key)
            return payload


class _DecisionCache:
    """Identical repeated calls must not pay the latency twice (agents retry)."""

    def __init__(self, ttl_s: float, max_entries: int = 1024) -> None:
        self._ttl = ttl_s
        self._entries: "OrderedDict[str, Tuple[float, Decision]]" = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def key_for(self, tool_name: str, args: Any, request: str) -> str:
        blob = json.dumps(
            {"t": tool_name, "a": args, "r": request}, sort_keys=True, default=str
        )
        return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()

    def get(self, key: str) -> Optional[Decision]:
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            stamped, decision = hit
            if now - stamped > self._ttl:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return decision

    def put(self, key: str, decision: Decision) -> None:
        if self._ttl <= 0 or decision.blind:
            return
        with self._lock:
            self._entries[key] = (time.monotonic(), decision)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)


class _Breaker:
    """Stop calling out once the far end is demonstrably down."""

    def __init__(self) -> None:
        self._consecutive = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    def record(self, ok: bool, threshold: int, cooldown_s: float) -> None:
        with self._lock:
            if ok:
                self._consecutive = 0
                self._open_until = 0.0
                return
            self._consecutive += 1
            if threshold > 0 and self._consecutive >= threshold:
                self._open_until = time.monotonic() + cooldown_s
                self._consecutive = 0


def _home_key() -> str:
    """Profile-scoped key. Under multiplex the process serves many homes."""
    try:
        from hermes_constants import hermes_home_key

        return str(hermes_home_key())
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            return str(get_hermes_home())
        except Exception:
            return "default"


def _api_key() -> Optional[str]:
    """Credentials resolve through the profile-aware secret scope, never os.environ."""
    try:
        from agent.secret_scope import get_secret

        return get_secret("OPENROUTER_API_KEY")
    except Exception:
        return None


def _clip(value: Any, limit: int) -> Any:
    """Keep the state well inside Jev's 32k shared budget for state + questions."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…[clipped]"
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, limit) for v in value]
    return value


class JevGate:
    """Decision engine + hook callbacks."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._intents = _IntentStore()
        self._cache = _DecisionCache(ttl_s=0.0)
        self._breaker = _Breaker()
        self._cfg_cache: Optional[Dict[str, Any]] = None
        self._cfg_lock = threading.Lock()

    # ── config ────────────────────────────────────────────────────────────

    def _cfg(self) -> Dict[str, Any]:
        """Read settings once. ``plugins.entries.jev-tool-gate.settings.*`` in config.yaml."""
        with self._cfg_lock:
            if self._cfg_cache is None:
                cfg = dict(DEFAULTS)
                for key, default in DEFAULTS.items():
                    try:
                        value = self.ctx.get_config(key, default)
                    except Exception:
                        value = default
                    cfg[key] = default if value is None else value
                self._cfg_cache = cfg
                self._cache = _DecisionCache(ttl_s=float(cfg["cache_ttl_s"]))
        return self._cfg_cache

    # ── hooks ─────────────────────────────────────────────────────────────

    def on_pre_llm_call(
        self,
        *,
        session_id: str = "",
        user_message: Any = None,
        conversation_history: Any = None,
        turn_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Capture the request this turn's tool calls will be judged against."""
        try:
            cfg = self._cfg()
            text = (
                user_message
                if isinstance(user_message, str)
                else json.dumps(user_message, default=str)
            )
            history: List[str] = []
            for item in reversed(list(conversation_history or [])):
                if len(history) >= int(cfg["max_history_turns"]):
                    break
                if isinstance(item, dict):
                    role = item.get("role")
                    content = item.get("content")
                    if (
                        role in ("user", "assistant")
                        and isinstance(content, str)
                        and content.strip()
                    ):
                        history.append(f"{role}: {_clip(content, 800)}")
            self._intents.put(
                (_home_key(), str(session_id or "")),
                {
                    "request": _clip(text, 4000),
                    "recent_turns": list(reversed(history)),
                    "turn_id": str(turn_id or ""),
                    "stamp": time.time(),
                },
            )
        except Exception as exc:  # capture is best-effort; never disturb the turn
            logger.debug("jev-tool-gate: intent capture failed: %s", exc)
        return None

    def on_pre_tool_call(
        self,
        *,
        tool_name: str = "",
        args: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate the proposed call and return a Hermes directive, or None to proceed."""
        try:
            cfg = self._cfg()
            gated = {str(t) for t in (cfg["gated_tools"] or [])}
            if tool_name not in gated:
                return None

            enforcing = str(cfg["mode"]).lower() == "enforce"
            decision = self._evaluate(tool_name, args, session_id, cfg)
            if not enforcing or decision is None:
                return None
            if decision.verdict in ("allow", "skip"):
                return None

            # An identical repeat is answered from cache, so the verdict cannot change
            # by retrying. Say so, or the agent burns turns looping on the same call.
            reason = decision.reason
            if decision.cached:
                reason += (
                    " (Repeat of a call already evaluated in this session: Jev's answer "
                    "is unchanged, so the retry cannot succeed.)"
                )

            if decision.verdict == "approve":
                # Native approval gate: ambiguous calls pause for a human rather
                # than being refused outright.
                return {
                    "action": "approve",
                    "message": reason,
                    "rule_key": f"jev:{tool_name}",
                }
            return {"action": "block", "message": reason}
        except Exception as exc:
            logger.warning("jev-tool-gate: gate error, allowing call: %s", exc)
            return None

    # ── evaluation ────────────────────────────────────────────────────────

    def _evaluate(
        self, tool_name: str, args: Any, session_id: str, cfg: Dict[str, Any]
    ) -> Optional[Decision]:
        intent = self._intents.get((_home_key(), str(session_id or "")))
        if not intent or not intent.get("request"):
            # No captured request (cron, subagent, resumed session): nothing for Jev
            # to judge against, so the gate stays out of the way.
            return None

        request = str(intent["request"])
        cache_key = self._cache.key_for(tool_name, args, request)
        cached = self._cache.get(cache_key)
        if cached is not None:
            decision = Decision(**{**cached.__dict__, "cached": True})
            self._log(tool_name, args, decision, cfg)
            return decision

        decision = self._call_jev(tool_name, args, intent, cfg)
        self._cache.put(cache_key, decision)
        self._log(tool_name, args, decision, cfg)
        return decision

    def _build_state(
        self, tool_name: str, args: Any, intent: Dict[str, Any]
    ) -> Dict[str, Any]:
        """State is deliberately the *whole* picture Jev judges, nothing else."""
        return {
            "user_request": intent.get("request", ""),
            "recent_turns": intent.get("recent_turns", []),
            "proposed_tool_call": {
                "tool": tool_name,
                "arguments": _clip(args if args is not None else {}, 2000),
            },
        }

    def _endpoints(self) -> List[str]:
        return [SYSTEM_ONE_URL, DECISIONS_URL]

    def _post(self, url: str, body: bytes, key: str, timeout: float) -> str:
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")

    def _call_jev(
        self, tool_name: str, args: Any, intent: Dict[str, Any], cfg: Dict[str, Any]
    ) -> Decision:
        key = _api_key()
        if not key:
            return Decision(
                verdict="skip",
                reason="OPENROUTER_API_KEY is not available in this profile scope",
            )

        if not self._breaker.allow():
            return Decision(
                verdict="skip",
                reason="Jev circuit breaker open after repeated failures; call allowed",
            )

        payload = {
            "model": cfg["model"],
            "state": self._build_state(tool_name, args, intent),
            "questions": {
                "authorized": {
                    "type": "noul",
                    "instructions": "Does the user's request authorize this exact tool call?",
                    "criteria": _AUTHORIZED_CRITERIA,
                },
                "risk": {
                    "type": "score",
                    "instructions": "How risky is this call if it is wrong?",
                    "criteria": _RISK_CRITERIA,
                },
            },
        }
        body = json.dumps(payload).encode("utf-8")
        timeout = float(cfg["timeout_s"])

        started = time.monotonic()
        raw: Optional[str] = None
        used = ""
        last_error = "no response"

        for url in self._endpoints():
            for attempt in range(int(cfg["retries"]) + 1):
                if attempt:
                    # The observed 403 is a Cloudflare edge block that lifts on its
                    # own; an immediate re-hit just gets blocked again.
                    time.sleep(float(cfg["retry_backoff_s"]) * attempt)
                try:
                    raw = self._post(url, body, key, timeout)
                    used = url
                    break
                except urllib.error.HTTPError as exc:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:120]
                    except Exception:
                        pass
                    last_error = f"HTTP {exc.code} {detail}".strip()
                    if exc.code not in _RETRYABLE_STATUS:
                        break
                except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            if raw is not None:
                break

        latency_ms = int((time.monotonic() - started) * 1000)

        if raw is None:
            self._breaker.record(
                False,
                int(cfg["breaker_threshold"]),
                float(cfg["breaker_cooldown_s"]),
            )
            logger.warning("jev-tool-gate: Jev unavailable (%s)", last_error)
            if str(cfg["fail_mode"]).lower() == "closed":
                return Decision(
                    verdict="block",
                    latency_ms=latency_ms,
                    reason=(
                        f"JEV GATE BLOCKED: Jev is unavailable ({last_error}) and this gate "
                        f"is configured fail_mode=closed, so {tool_name} was not run."
                    ),
                )
            return Decision(
                verdict="skip",
                latency_ms=latency_ms,
                reason=f"Jev unavailable ({last_error}); allowed by fail-open",
            )

        self._breaker.record(
            True, int(cfg["breaker_threshold"]), float(cfg["breaker_cooldown_s"])
        )

        try:
            data = json.loads(raw)
            answers = data.get("answers") or {}
            probability = float((answers.get("authorized") or {}).get("noul"))
        except Exception as exc:
            logger.warning("jev-tool-gate: unreadable Jev response (%s)", exc)
            return Decision(
                verdict="skip",
                latency_ms=latency_ms,
                reason="Jev returned an unreadable response; allowed by fail-open",
            )

        risk_value: Optional[int] = None
        try:
            risk_value = int((answers.get("risk") or {}).get("score"))
        except Exception:
            risk_value = None

        cost = None
        try:
            cost = float((data.get("usage") or {}).get("cost"))
        except Exception:
            pass

        accept = float(cfg["accept_threshold"])
        escalate = float(cfg["escalate_threshold"])

        if probability >= accept:
            verdict = "allow"
        elif probability >= escalate:
            verdict = "approve"
        else:
            verdict = "block"

        risk_note = (
            f", risk {risk_value}/3 ({_RISK_CRITERIA[risk_value]})"
            if isinstance(risk_value, int) and 0 <= risk_value < len(_RISK_CRITERIA)
            else ""
        )
        if verdict == "allow":
            reason = f"authorized p={probability:.2f}{risk_note}"
        elif verdict == "approve":
            reason = (
                f"JEV GATE: authorization is ambiguous for {tool_name} "
                f"(p={probability:.2f}, between {escalate:.2f} and {accept:.2f}{risk_note}). "
                f"Needs human approval before it runs."
            )
        else:
            reason = (
                f"JEV GATE BLOCKED: {tool_name} scored p={probability:.2f} for being "
                f"authorized by the user's request (below {escalate:.2f}{risk_note}). "
                f"Jev returns probabilities, not reasons. Retrying will not change this "
                f"verdict, because Jev is deterministic for identical input — the user "
                f"needs to confirm the call explicitly for it to proceed."
            )

        return Decision(
            verdict=verdict,
            probability=probability,
            risk=risk_value,
            reason=reason,
            cost_usd=cost,
            latency_ms=latency_ms,
            endpoint=used,
        )

    # ── logging ───────────────────────────────────────────────────────────

    def _log(
        self, tool_name: str, args: Any, decision: Decision, cfg: Dict[str, Any]
    ) -> None:
        if not decision.cached:
            logger.info(
                "jev-tool-gate[%s] %s -> %s (p=%s, risk=%s, %sms, $%s)",
                cfg["mode"],
                tool_name,
                decision.verdict,
                "n/a" if decision.probability is None else f"{decision.probability:.2f}",
                decision.risk,
                decision.latency_ms,
                decision.cost_usd,
            )
        if not cfg.get("log_decisions", True):
            return
        try:
            from hermes_constants import get_hermes_home

            path: Path = get_hermes_home() / "logs" / "jev-tool-gate.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.time(),
                "mode": cfg["mode"],
                "model": cfg["model"],
                "tool": tool_name,
                "args": _clip(args if args is not None else {}, 2000),
                "verdict": decision.verdict,
                "probability": decision.probability,
                "risk": decision.risk,
                # A blind entry means the gate saw nothing: fail-open or unavailable.
                "blind": decision.blind,
                "reason": decision.reason,
                "endpoint": decision.endpoint,
                "cost_usd": decision.cost_usd,
                "latency_ms": decision.latency_ms,
                "cached": decision.cached,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.debug("jev-tool-gate: decision log failed: %s", exc)


def register(ctx: Any) -> None:
    """Plugin entry point."""
    gate = JevGate(ctx)
    ctx.register_hook("pre_llm_call", gate.on_pre_llm_call)
    ctx.register_hook("pre_tool_call", gate.on_pre_tool_call)
