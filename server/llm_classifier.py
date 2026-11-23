# -*- coding: utf-8 -*-
"""
llm_classifier.py — optional Groq LLM department classifier.

A drop-in classification stage for department_router. Given a patient transcript
(any language), it asks a Groq-hosted LLM which department should handle the
request and whether a nurse must be involved. Because it reasons over MEANING,
it is robust where the keyword/DistilBERT stack is brittle:

  • "I have spilled water on the floor"      -> housekeeping (not Medication)
  • "mujhe khana chahiye" (I want food)       -> canteen     (not Hygiene)
  • "my prescription hasn't come"             -> pharmacy
  • "my chest hurts"                          -> nurse (clinical)

Safety + robustness by design:
  • Fail-safe: any problem (no key, disabled, HTTP error, timeout, bad JSON)
    returns None so the router falls back to the keyword classifier. The server
    never breaks because the LLM is unavailable.
  • No new dependency: uses urllib from the stdlib, run in a thread by the async
    caller so the event loop is never blocked.
  • The caller still applies the hard safety gates (critical markers -> nurse,
    low confidence -> nurse). This module only proposes a department.

Enable by setting USE_LLM_CLASSIFIER=true and GROQ_API_KEY in the environment.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from config import settings

logger = logging.getLogger(__name__)

# Valid departments the LLM may choose. "nurse" is the safe catch-all.
_VALID = {"nurse", "canteen", "pharmacy", "housekeeping"}

_SYSTEM_PROMPT = (
    "You are the triage router for a hospital patient-request system. "
    "A patient has spoken a request (the text may be in English, Hindi, Kannada, "
    "or German, and may be imperfectly transcribed). Decide which ONE department "
    "should handle it, and whether a nurse must be involved.\n\n"
    "Departments:\n"
    "- nurse: anything clinical or medical — pain, breathing, chest issues, "
    "emergencies, emotional distress, mobility help, toileting/bathing/personal "
    "care, or anything unclear or ambiguous. This is the safe default.\n"
    "- canteen: food, water, drinks, meals, hunger, thirst.\n"
    "- pharmacy: a request for a specific medicine/tablet/drug, a refill, or a "
    "prescription that hasn't arrived.\n"
    "- housekeeping: the ROOM or FACILITIES — cleaning, spills on the floor, "
    "dirty bathroom, changing bedsheets, full bins, broken AC/fan/TV/light, "
    "leaks, temperature of the room.\n\n"
    "Rules:\n"
    "- Personal care (bathe me, I soiled myself, change my diaper) is the NURSE, "
    "not housekeeping.\n"
    "- Taking/timing/side-effects of medication is the NURSE; only a supply/"
    "dispense request is pharmacy.\n"
    "- If the request mixes clinical and non-clinical, set needs_nurse true and "
    "pick the non-clinical department as 'department'.\n"
    "- If you are unsure, choose nurse.\n\n"
    "Also decide is_request: false ONLY when the patient asks for nothing and "
    "expresses no need or feeling — small talk, a greeting, a remark about the "
    "weather/TV/news, thanks, or talking to someone else in the room. Any pain, "
    "discomfort, fear, sadness, loneliness, worry, question about their care, or "
    "anything you are unsure about is is_request true.\n\n"
    "Reply with ONLY a compact JSON object, no prose:\n"
    '{\"is_request\": true|false, \"department\": \"nurse|canteen|pharmacy|housekeeping\", '
    '\"needs_nurse\": true|false, \"confidence\": 0.0-1.0, \"reason\": \"short\"}'
)


def _call_groq(transcript: str) -> dict | None:
    """Synchronous HTTP call to Groq's OpenAI-compatible endpoint. Returns the
    parsed JSON decision dict, or None on any failure."""
    key = settings.GROQ_API_KEY
    if not key:
        return None
    url = settings.GROQ_BASE_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:500]},
        ],
        "temperature": 0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        # gpt-oss models emit chain-of-thought that can leak into the content and
        # break strict JSON validation; keep reasoning minimal so the reply is
        # just the JSON object.
        "reasoning_effort": "low",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Groq's API is fronted by Cloudflare, which rejects requests with no
            # User-Agent as bots (HTTP 403, error 1010). A real UA avoids that.
            "User-Agent": "CareVoiceAI/1.0 (+https://3-110-163-212.sslip.io)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.GROQ_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        return _extract_json(content)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        # Some models (gpt-oss) occasionally fail Groq's strict JSON validator
        # and return the attempted text in 'failed_generation' — try to salvage
        # the JSON object from it before giving up to the keyword fallback.
        if e.code == 400 and "failed_generation" in detail:
            try:
                fg = json.loads(detail)["error"].get("failed_generation", "")
                salvaged = _extract_json(fg)
                if salvaged:
                    return salvaged
            except Exception:
                pass
        logger.warning("[LLM] Groq HTTP %s — falling back to keywords. %s", e.code, detail[:200])
        return None
    except Exception as exc:
        logger.warning("[LLM] Groq call failed (%s) — falling back to keywords", exc)
        return None


def _extract_json(text: str) -> dict | None:
    """Parse a JSON object from the model's reply, tolerating markdown fences or
    stray prose around it (extract the first balanced {...} block)."""
    if not text:
        return None
    t = text.strip()
    # Fast path: clean JSON.
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Strip ```json fences if present.
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    # Fall back to the first balanced-brace object.
    start = t.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        return None
    return None


def classify_sync(transcript: str) -> dict | None:
    """Return a normalised decision dict or None (fall back to keywords).

    Shape: {"department": str, "needs_nurse": bool, "confidence": float,
            "reason": str}. None whenever the LLM is disabled/unavailable or the
    reply can't be trusted."""
    if not settings.USE_LLM_CLASSIFIER or not settings.GROQ_API_KEY:
        return None
    if not transcript or not transcript.strip():
        return None

    raw = _call_groq(transcript)
    if not isinstance(raw, dict):
        return None

    dept = str(raw.get("department", "")).strip().lower()
    if dept not in _VALID:
        logger.warning("[LLM] invalid department %r — falling back", dept)
        return None
    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    needs_nurse = bool(raw.get("needs_nurse", False))
    reason = str(raw.get("reason", ""))[:160]

    # Missing / non-boolean is_request counts as a request (safe default).
    is_request = raw.get("is_request", True) is not False

    return {
        "is_request": is_request,
        "department": dept,
        "needs_nurse": needs_nurse,
        "confidence": conf,
        "reason": reason,
    }


async def classify(transcript: str) -> dict | None:
    """Async wrapper: runs the blocking HTTP call in a thread so the event loop
    is never blocked. Returns the same shape as classify_sync, or None."""
    if not settings.USE_LLM_CLASSIFIER or not settings.GROQ_API_KEY:
        return None
    import asyncio
    try:
        return await asyncio.to_thread(classify_sync, transcript)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[LLM] classify thread failed (%s) — falling back", exc)
        return None
