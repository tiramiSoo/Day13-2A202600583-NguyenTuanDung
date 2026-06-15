"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}
"""
from __future__ import annotations
import os
import re
import time

from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.cost import cost_from_usage
from telemetry.redact import redact

# Load our improved prompt once at module import
_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "prompt.txt"), encoding="utf-8") as _f:
    _SYSTEM_PROMPT = _f.read().strip()

# Detects injected price instructions hidden in order notes (private phase injection twist)
_NOTE_INJECTION_RE = re.compile(
    r"(ghi\s*ch[uúù][^\n]*\d{5,}|"          # GHI CHÚ with a 5+ digit number (fake price)
    r"price\s*[=:]\s*\d|"                    # price=... English injection
    r"gia\s*(?:sp|san\s*pham)?\s*[=:]\s*\d|"# gia=... Vietnamese injection
    r"ignore\s+(?:previous|above|all)\s+instructions)",
    re.IGNORECASE,
)


def _sanitize(question: str) -> str:
    """Strip injected price/instruction from order notes while preserving real content."""
    # Replace GHI CHÚ lines that contain a suspicious large number (injected price)
    cleaned = re.sub(
        r"((?:GHI\s*CH[ÚUu]|note)[^\n]*\d{5,}[^\n]*)",
        "[GHI CHU: da loc]",
        question,
        flags=re.IGNORECASE,
    )
    return cleaned


def mitigate(call_next, question, config, context):
    cid = new_correlation_id()
    set_correlation_id(cid)

    qid = context.get("qid")
    session_id = context.get("session_id")
    turn_index = context.get("turn_index", 0)
    cache: dict = context.get("cache", {})
    cache_lock = context.get("cache_lock")

    # 1. Sanitize injection in order notes
    clean_q = _sanitize(question)
    if clean_q != question:
        logger.log_event("INJECTION_SANITIZED", {
            "qid": qid, "cid": cid, "session_id": session_id,
        })

    # 2. Cache lookup (thread-safe) — same question should give same answer
    cache_key = clean_q.strip().lower()
    if cache_lock:
        with cache_lock:
            cached = cache.get(cache_key)
        if cached:
            logger.log_event("CACHE_HIT", {
                "qid": qid, "cid": cid, "session_id": session_id,
            })
            return cached

    # 3. Route our improved system prompt for every request
    conf = dict(config)
    conf["system_prompt"] = _SYSTEM_PROMPT

    # 4. Retry loop (respects config retry settings)
    retry_cfg = config.get("retry", {})
    max_attempts = retry_cfg.get("max_attempts", 1) if retry_cfg.get("enabled") else 1
    backoff_ms = retry_cfg.get("backoff_ms", 500)

    result = None
    for attempt in range(max(1, max_attempts)):
        t0 = time.time()
        result = call_next(clean_q, conf)
        wall_ms = int((time.time() - t0) * 1000)

        meta = result.get("meta", {})
        usage = meta.get("usage", {})
        status = result.get("status", "ok")
        answer = result.get("answer") or ""

        # 5. PII redaction: strip any email/phone that slipped into the answer
        redacted_answer, pii_count = redact(answer)
        if pii_count > 0:
            result = dict(result)
            result["answer"] = redacted_answer

        # 6. Observability: the only place latency/cost/tools/PII signals exist
        logger.log_event("AGENT_CALL", {
            "qid": qid,
            "cid": cid,
            "session_id": session_id,
            "turn_index": turn_index,
            "attempt": attempt + 1,
            "status": status,
            "wall_ms": wall_ms,
            "latency_ms": meta.get("latency_ms"),
            "steps": result.get("steps"),
            "tokens": usage,
            "cost_usd": cost_from_usage(meta.get("model", ""), usage),
            "pii_in_answer": pii_count > 0,
            "tools_used": meta.get("tools_used", []),
            "tool_count": len(meta.get("tools_used", [])),
        })

        if status in ("ok", "no_action"):
            break

        # Retry on transient error/loop, not on max_steps (that's a config issue)
        if status in ("loop", "wrapper_error") and attempt < max_attempts - 1:
            logger.log_event("RETRY", {
                "qid": qid, "cid": cid, "attempt": attempt + 1, "status": status,
            })
            time.sleep(backoff_ms / 1000.0)

    # 7. Cache successful results for deduplication
    if result and result.get("status") == "ok" and result.get("answer"):
        if cache_lock:
            with cache_lock:
                cache[cache_key] = result

    return result
