# -*- coding: utf-8 -*-
"""
care_agent.py — Central LLM care agent (self-hosted Ollama).

Capabilities
  1. doctor_summary()   case-history summary for the doctor (fixed sections)
  2. patient_chat()     supportive, emotionally intelligent replies; answers the
                        patient's general questions about their parameters and
                        readings at a surface level, from EMR data (read-only)
  3. shift_summary()    nurse copilot: per-patient summary + suggested next action
  4. trends()           behaviour trends from alert history (deterministic)
  5. translate()        Hindi <-> English translation for voice notes

Guardrails (from the "Central LLM Care Agent" slide: advisory only, safety
override stays final, self-hosted)
  * Advisory only. Never diagnoses, never changes medication or dosage, never
    changes clinical priority.
  * Emergency / critical language from a patient gets "alerting your nurse now"
    immediately (no LLM wait) and the caller raises a normal nurse alert.
  * Output that contains a dosage, a diagnosis statement or "stop taking" advice
    is replaced with a safe reply and flagged.
  * The patient chat only ever sees an allow-listed projection of the record
    (latest vitals/labs, diet, diagnosis label, medication names).
  * Every LLM call is written to llm_audit.
  * Fail-safe: Ollama unreachable -> templated answer from the data, HTTP 200.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request

from config import settings

logger = logging.getLogger(__name__)


# ── LLM transport ─────────────────────────────────────────────────────────────

def _ollama_chat(system: str, user: str, *, json_mode: bool = False, max_tokens: int = 500) -> str | None:
    body = {
        "model": settings.OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
    }
    if json_mode:
        body["format"] = "json"
    req = urllib.request.Request(
        settings.OLLAMA_BASE_URL.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.OLLAMA_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("message") or {}).get("content") or None
    except Exception as exc:
        logger.warning("[CARE] Ollama unavailable (%s)", exc)
        return None


def _groq_chat(system: str, user: str, max_tokens: int = 500, *, allow: bool | None = None) -> str | None:
    allowed = settings.CARE_AGENT_GROQ_FALLBACK if allow is None else allow
    if not (allowed and settings.GROQ_API_KEY):
        return None
    body = {"model": settings.GROQ_MODEL, "temperature": 0.3, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "reasoning_effort": "low"}
    req = urllib.request.Request(
        settings.GROQ_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}", "Content-Type": "application/json",
                 "User-Agent": "CareVoiceAI/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=settings.GROQ_TIMEOUT_SECONDS * 3) as resp:
            return json.loads(resp.read().decode())["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("[CARE] Groq fallback failed (%s)", exc)
        return None


async def _llm(system: str, user: str, *, json_mode=False, max_tokens=500,
               groq_first: bool = False) -> tuple[str | None, str]:
    """Return (text, provider). provider is 'ollama', 'groq' or 'template'.

    groq_first=True (AskCareVoice, set by ASK_CAREVOICE_USE_GROQ): Groq is tried
    first and Ollama only as a fallback, so the patient gets a fast reply even
    where no Ollama server exists. Other features keep the Ollama-first,
    Groq-only-if-CARE_AGENT_GROQ_FALLBACK behaviour."""
    import asyncio
    if groq_first:
        text = await asyncio.to_thread(_groq_chat, system, user, max_tokens, allow=True)
        if text:
            return text, "groq"
    text = await asyncio.to_thread(_ollama_chat, system, user, json_mode=json_mode, max_tokens=max_tokens)
    if text:
        return text, "ollama"
    text = await asyncio.to_thread(_groq_chat, system, user, max_tokens)
    if text:
        return text, "groq"
    return None, "template"


async def _audit(*, user: str | None, role: str | None, purpose: str, provider: str,
                 prompt: str, response: str, flags: list[str], latency_ms: int, status: str) -> None:
    try:
        import aiosqlite
        from database import utcnow
        async with aiosqlite.connect(settings.DB_PATH) as db:
            await db.execute(
                "INSERT INTO llm_audit (username, role, purpose, provider, model, prompt, response, flags, "
                "latency_ms, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (user, role, purpose, provider,
                 settings.OLLAMA_MODEL if provider == "ollama" else (settings.GROQ_MODEL if provider == "groq" else ""),
                 prompt[:4000], (response or "")[:4000], ",".join(flags), latency_ms, status, utcnow()))
            await db.commit()
    except Exception as exc:  # pragma: no cover
        logger.warning("[CARE] audit write failed: %s", exc)


# ── Guardrails ────────────────────────────────────────────────────────────────

def is_emergency(text: str) -> bool:
    import priority_engine
    return priority_engine.has_critical_marker(text or "")


_UNSAFE = [
    re.compile(r"\b\d+(\.\d+)?\s?(mg|mcg|µg|ml|units?|iu|tablets?|puffs?)\b", re.I),
    re.compile(r"\b(stop|quit|skip|double|increase|decrease|reduce)\s+(taking\s+)?(your\s+)?(medicine|medication|tablets?|dose|pills?)", re.I),
    re.compile(r"\byou (have|are suffering from|might have|probably have)\s+(a |an )?(cancer|infection|disease|diabetes|tumou?r|stroke|heart attack|sepsis)", re.I),
    re.compile(r"\b(diagnos(e|is|ed))\b", re.I),
]


def check_output(text: str, *, allow_doses: bool = False) -> tuple[str, list[str]]:
    """Return (safe_text, flags). The doctor summary may mention prescribed doses
    (they come from the chart), patient-facing text may not."""
    flags = []
    for i, pat in enumerate(_UNSAFE):
        if i == 0 and allow_doses:
            continue
        if i == 3 and allow_doses:
            continue
        if pat.search(text or ""):
            flags.append("output_blocked")
            break
    if flags:
        return ("I can't give medical advice on that, but your nurse and doctor can. "
                "Would you like me to let your nurse know you have a question?"), flags
    return text, flags


EMERGENCY_REPLY = {
    "en": "I'm alerting your nurse right now. Please stay calm; help is on the way.",
    "hi": "मैं अभी आपकी नर्स को सूचित कर रहा हूँ। कृपया शांत रहें, मदद आ रही है।",
}


def _lang_of(text: str) -> str:
    return "hi" if any(0x0900 <= ord(c) <= 0x097F for c in text or "") else "en"


# ── Data helpers ──────────────────────────────────────────────────────────────

async def _alert_rows(room: str, limit: int = 200) -> list[dict]:
    import aiosqlite
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, priority, intent, emotion, summary, transcript, created_at, acknowledged, "
            "request_class FROM alerts WHERE room_id = ? AND is_demo = 0 ORDER BY created_at DESC LIMIT ?",
            (room, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]


def trends_from_alerts(rows: list[dict], *, hospital_utc_offset_min: int = 330) -> dict:
    """Deterministic behaviour trends (no LLM): repeat calls, rising distress,
    night-time share. created_at is UTC ISO; night = 22:00-06:00 hospital time."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    parsed = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        parsed.append((ts, r))
    last24 = [r for ts, r in parsed if now - ts <= timedelta(hours=24)]
    nurse24 = [r for r in last24 if (r.get("request_class") or "nurse") == "nurse"]
    night = 0
    for ts, r in parsed:
        local = ts + timedelta(minutes=hospital_utc_offset_min)
        if local.hour >= 22 or local.hour < 6:
            night += 1
    rank = {"calm": 0, "anxious": 1, "distressed": 2, "panicked": 3}
    recent = [rank.get((r.get("emotion") or "calm"), 0) for ts, r in sorted(parsed, key=lambda x: x[0])][-6:]
    rising = len(recent) >= 3 and recent[-1] > recent[0] and recent[-1] >= 2
    flags = []
    if len(nurse24) >= 5:
        flags.append(f"Repeat calls: {len(nurse24)} nurse calls in the last 24 h")
    if rising:
        flags.append("Rising distress in recent calls")
    if parsed and night / len(parsed) >= 0.4 and len(parsed) >= 3:
        flags.append(f"Night-time pattern: {round(100 * night / len(parsed))}% of calls between 22:00 and 06:00")
    return {
        "calls_24h": len(last24),
        "nurse_calls_24h": len(nurse24),
        "night_share": round(night / len(parsed), 2) if parsed else 0.0,
        "rising_distress": rising,
        "flags": flags,
        "total_calls": len(parsed),
    }


def _fmt_obs(obs: list[dict]) -> str:
    return "; ".join(f"{o['display']} {o['value']}{(' ' + o['unit']) if o.get('unit') else ''}"
                     f"{' (' + o['flag'] + ')' if o.get('flag') and o['flag'] != 'normal' else ''}"
                     for o in obs) or "none recorded"


# ── 1. Doctor summary ─────────────────────────────────────────────────────────

_DOCTOR_SYSTEM = (
    "You are a clinical documentation assistant. Summarise the patient's record for "
    "the treating doctor using ONLY the data provided. Do not invent findings, do not "
    "make new diagnoses and do not recommend medication changes. Write concise plain "
    "text with exactly these section headings: Presenting problem, Active conditions, "
    "Latest vitals, Abnormal labs, Current medications, Hospital course, Nurse-call "
    "pattern, Points for the doctor's attention."
)


async def doctor_summary(room: str, *, user: str, role: str) -> dict:
    from his_adapter import get_adapter, _latest
    emr = await get_adapter()
    rec = await emr.record(room)
    alerts = await _alert_rows(room)
    tr = trends_from_alerts(alerts)
    p = rec.get("patient") or {}
    vit, lab = _latest(rec["vitals"]), _latest(rec["labs"])
    abnormal = [o for o in lab if o.get("flag") not in (None, "", "normal")]
    data = {
        "patient": {k: p.get(k) for k in ("full_name", "age", "diagnosis", "acuity", "diet_restriction")},
        "conditions": rec["conditions"], "latest_vitals": vit, "abnormal_labs": abnormal,
        "medications": rec["medications"], "encounters": rec["encounters"][:10],
        "nurse_calls": {"last_24h": tr["nurse_calls_24h"], "total": tr["total_calls"], "flags": tr["flags"],
                        "recent": [{"intent": a["intent"], "summary": a["summary"], "at": a["created_at"]}
                                   for a in alerts[:8]]},
    }
    prompt = json.dumps(data, default=str)
    t0 = time.time()
    text, provider = await _llm(_DOCTOR_SYSTEM, prompt, max_tokens=700)
    flags = []
    if text:
        text, flags = check_output(text, allow_doses=True)
    if not text or flags:
        provider = "template" if not text else provider
        text = _doctor_template(p, rec, vit, abnormal, tr, alerts)
    await _audit(user=user, role=role, purpose="doctor_summary", provider=provider, prompt=prompt,
                 response=text, flags=flags, latency_ms=int((time.time() - t0) * 1000), status="ok")
    return {"room": room, "provider": provider, "source": rec["source_label"],
            "banner": "AI-generated draft, requires clinician review.",
            "summary": text, "trends": tr}


def _doctor_template(p, rec, vit, abnormal, tr, alerts) -> str:
    meds = "; ".join(f"{m['name']} ({m['dosage']})" if m.get("dosage") else m["name"]
                     for m in rec["medications"]) or "none listed"
    course = "\n".join(f"- {e['kind']}: {e['summary']}" for e in rec["encounters"][:6]) or "- no entries"
    conds = "; ".join(c["display"] for c in rec["conditions"]) or "none recorded"
    points = tr["flags"] or ["No behaviour flags in recent calls."]
    if abnormal:
        points = points + [f"Abnormal: {_fmt_obs(abnormal)}"]
    return (
        f"Presenting problem: {p.get('diagnosis') or 'not recorded'}\n"
        f"Active conditions: {conds}\n"
        f"Latest vitals: {_fmt_obs(vit)}\n"
        f"Abnormal labs: {_fmt_obs(abnormal)}\n"
        f"Current medications: {meds}\n"
        f"Hospital course:\n{course}\n"
        f"Nurse-call pattern: {tr['nurse_calls_24h']} nurse calls in 24 h, {tr['total_calls']} total.\n"
        f"Points for the doctor's attention:\n" + "\n".join(f"- {x}" for x in points)
    )


# ── 2/3. Patient chat (support + surface-level Q&A) ───────────────────────────

_PATIENT_SYSTEM = {
    "en": (
        "You are CareVoice, a warm, encouraging bedside companion for a hospital patient. "
        "Speak simply, kindly and briefly (at most 4 sentences). Acknowledge feelings first. "
        "You may explain the patient's own readings at a surface level using ONLY the data "
        "given (say whether a value is in the usual range). You must NOT diagnose, must NOT "
        "suggest starting, stopping or changing any medicine or dose, and must NOT give "
        "treatment plans. For anything clinical, encourage them to ask their nurse or doctor. "
        "Your reply may be read aloud: plain spoken sentences only, no lists, no markdown, "
        "no symbols. Detected mood: {emotion}."
    ),
    "hi": (
        "आप CareVoice हैं, अस्पताल में भर्ती मरीज़ के लिए एक स्नेही और हौसला बढ़ाने वाले साथी। "
        "सरल, दयालु और छोटे (अधिकतम 4 वाक्य) उत्तर दें। पहले भावनाओं को स्वीकार करें। "
        "दिए गए डेटा से ही मरीज़ की रीडिंग को सरल भाषा में समझाएँ। निदान न करें, किसी दवा या "
        "खुराक को शुरू/बंद/बदलने की सलाह न दें। चिकित्सा संबंधी बातों के लिए नर्स या डॉक्टर से "
        "पूछने को कहें। उत्तर बोलकर सुनाया जा सकता है: सीधे वाक्य, कोई सूची या चिह्न नहीं। "
        "मरीज़ का मूड: {emotion}।"
    ),
}


async def patient_chat(room: str, message: str, *, user: str, emotion: str | None = None,
                       lang: str | None = None) -> dict:
    lang = lang if lang in ("en", "hi") else _lang_of(message)
    if is_emergency(message):
        reply = EMERGENCY_REPLY[lang]
        await _audit(user=user, role="patient", purpose="patient_chat", provider="guardrail",
                     prompt=message, response=reply, flags=["emergency"], latency_ms=0, status="emergency")
        return {"reply": reply, "provider": "guardrail", "emergency": True, "lang": lang}

    if emotion is None:
        try:
            import nlp_summary
            from models import Intent
            emotion = nlp_summary.analyze(transcript=message, intent=Intent.OTHER, room_id=room,
                                          patient_name=None).get("emotion")
        except Exception:
            emotion = "calm"
    from his_adapter import get_adapter
    kp = await (await get_adapter()).key_parameters(room)
    context = {"my_readings": {"vitals": kp["vitals"], "labs": kp["labs"]},
               "diet": kp["diet_restriction"], "condition_label": kp["diagnosis"],
               "medicine_names": kp["medication_names"]}
    system = _PATIENT_SYSTEM[lang].format(emotion=emotion or "calm")
    prompt = f"Patient data: {json.dumps(context, default=str)}\n\nPatient says: {message}"
    t0 = time.time()
    text, provider = await _llm(system, prompt, max_tokens=260,
                                groq_first=settings.ASK_CAREVOICE_USE_GROQ)
    flags = []
    if text:
        text, flags = check_output(text)
    if not text:
        text = _patient_template(message, kp, emotion, lang)
    await _audit(user=user, role="patient", purpose="patient_chat", provider=provider, prompt=prompt,
                 response=text, flags=flags, latency_ms=int((time.time() - t0) * 1000), status="ok")
    return {"reply": text, "provider": provider, "emergency": False, "emotion": emotion, "lang": lang}


def _patient_template(message: str, kp: dict, emotion: str | None, lang: str) -> str:
    m = (message or "").lower()
    comfort = {
        "anxious": "It's completely understandable to feel worried in hospital. ",
        "distressed": "I'm sorry you're feeling this way; you're not alone and your care team is here. ",
        "panicked": "I hear you. Take a slow breath with me; your nurse is close by. ",
    }.get(emotion or "", "")
    for o in kp["vitals"] + kp["labs"]:
        key = o["display"].lower()
        if any(w in m for w in key.split()) or (("bp" in m or "pressure" in m) and "pressure" in key) \
                or (("sugar" in m or "glucose" in m) and "glucose" in key) \
                or (("oxygen" in m or "spo2" in m) and "oxygen" in key):
            state = "within the usual range" if o["flag"] == "normal" else f"a little {o['flag']} compared with the usual range"
            return (f"{comfort}Your latest {o['display'].lower()} is {o['value']} {o['unit']}, which is {state} "
                    f"({o['ref_range']}). Your nurse or doctor can explain what it means for you.").strip()
    if lang == "hi":
        return "आप बहुत अच्छा कर रहे हैं। आपकी देखभाल टीम आपके साथ है। कोई सवाल हो तो नर्स से ज़रूर पूछें।"
    return (comfort + "You're doing well, and your care team is looking after you. "
            "If you have a question about your treatment, your nurse can help.").strip()


async def supportive_line(emotion: str | None, lang: str = "en") -> str:
    """Short morale-boosting line sent to the patient after a request (no LLM
    needed; deterministic so it is instant and always safe)."""
    lines = {
        "en": {"calm": "Your nurse has your request. You're in good hands.",
               "anxious": "We've got your request. It's okay to feel worried; help is coming.",
               "distressed": "Your nurse has been told and is on the way. You're not alone.",
               "panicked": "Help is on the way right now. Breathe slowly; your nurse is coming."},
        "hi": {"calm": "आपका अनुरोध नर्स तक पहुँच गया है। आप सुरक्षित हाथों में हैं।",
               "anxious": "आपका अनुरोध मिल गया है। चिंता न करें, मदद आ रही है।",
               "distressed": "नर्स को बता दिया गया है और वे आ रही हैं। आप अकेले नहीं हैं।",
               "panicked": "मदद अभी आ रही है। धीरे-धीरे साँस लें, नर्स आ रही हैं।"},
    }
    return lines.get(lang, lines["en"]).get(emotion or "calm", lines["en"]["calm"])


# ── 4. Nurse copilot ──────────────────────────────────────────────────────────

async def shift_summary(rooms: list[dict], *, user: str, role: str) -> dict:
    """rooms: [{"room_number", "full_name"}]. Returns per-patient summary + next action."""
    from his_adapter import get_adapter, _latest
    emr = await get_adapter()
    out = []
    for r in rooms:
        room = r["room_number"]
        rec = await emr.record(room)
        alerts = await _alert_rows(room, 50)
        tr = trends_from_alerts(alerts)
        vit = _latest(rec["vitals"])
        abnormal = [o for o in vit + _latest(rec["labs"]) if o.get("flag") not in (None, "", "normal")]
        open_alerts = [a for a in alerts if not a["acknowledged"] and (a.get("request_class") or "nurse") == "nurse"]
        data = {"patient": r.get("full_name"), "diagnosis": (rec.get("patient") or {}).get("diagnosis"),
                "abnormal": abnormal, "open_calls": [a["summary"] for a in open_alerts[:5]],
                "trend_flags": tr["flags"]}
        prompt = json.dumps(data, default=str)
        t0 = time.time()
        text, provider = await _llm(
            "You are a nurse shift-handover assistant. In 2 short sentences summarise this "
            "patient's status using only the data, then give ONE suggested next nursing action "
            "starting with 'Suggested next action:'. Never suggest medication changes.",
            prompt, max_tokens=160)
        flags = []
        if text:
            text, flags = check_output(text, allow_doses=True)
        if not text or flags:
            provider = "template" if not text else provider
            nxt = ("Review open call(s): " + "; ".join(data["open_calls"][:2])) if data["open_calls"] else \
                  (f"Recheck {abnormal[0]['display'].lower()}" if abnormal else "Routine observation round")
            text = (f"{r.get('full_name')} ({data['diagnosis'] or 'no diagnosis recorded'}). "
                    f"Abnormal: {_fmt_obs(abnormal)}. {'; '.join(tr['flags']) or 'No behaviour flags.'}\n"
                    f"Suggested next action: {nxt}.")
        await _audit(user=user, role=role, purpose="shift_summary", provider=provider, prompt=prompt,
                     response=text, flags=flags, latency_ms=int((time.time() - t0) * 1000), status="ok")
        out.append({"room_number": room, "full_name": r.get("full_name"), "summary": text,
                    "provider": provider, "trends": tr})
    return {"patients": out, "banner": "Advisory only. Clinical judgement and safety overrides stay final."}


# ── 5. Translation (voice notes) ──────────────────────────────────────────────

async def translate(text: str, source: str, target: str, *, user: str | None = None) -> dict:
    """LLM translation (better Hindi -> English than on-device ML Kit). Returns
    {"text": None} when no LLM is reachable so the app falls back to ML Kit."""
    names = {"en": "English", "hi": "Hindi", "de": "German", "kn": "Kannada"}
    if not text.strip() or source == target:
        return {"text": text, "provider": "noop"}
    system = (f"Translate the user's text from {names.get(source, source)} to {names.get(target, target)}. "
              "It is a short message between a hospital patient and a nurse and may be informal or "
              "imperfectly transcribed. Keep the meaning, keep it natural, and output ONLY the translation.")
    t0 = time.time()
    out, provider = await _llm(system, text, max_tokens=300)
    if not out:
        # Groq is acceptable for translation when the classifier is already using it.
        import asyncio
        if settings.USE_LLM_CLASSIFIER and settings.GROQ_API_KEY:
            prev = settings.CARE_AGENT_GROQ_FALLBACK
            settings.CARE_AGENT_GROQ_FALLBACK = True
            try:
                out = await asyncio.to_thread(_groq_chat, system, text, 300)
            finally:
                settings.CARE_AGENT_GROQ_FALLBACK = prev
            provider = "groq" if out else "none"
    await _audit(user=user, role=None, purpose="translate", provider=provider, prompt=text,
                 response=out or "", flags=[], latency_ms=int((time.time() - t0) * 1000),
                 status="ok" if out else "unavailable")
    return {"text": (out or "").strip().strip('"') or None, "provider": provider}
