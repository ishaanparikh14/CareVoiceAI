"""
routers/v3_api.py — CareVoice v3 endpoints.

Admin
    GET   /admin/workflow                 effective workflow settings + schema + presets
    PATCH /admin/workflow                 change settings (validated, logged)
    POST  /admin/workflow/preset/{name}   small_clinic | multi_specialty
    GET   /admin/workflow/log             change history
    GET   /admin/routing-stats            datapoints (?format=json|csv&days=30)
    POST  /admin/routing-stats/demo       insert SIMULATED rows (kept out of real stats)
    DELETE /admin/routing-stats/demo      remove SIMULATED rows
    GET   /admin/llm-audit                care-agent audit log
Food (patients cannot order; attender or nurse orders)
    POST  /attender/unlock                patient device + room PIN -> attender token
    GET   /orders/menu/{room}             menu with diet verdict per item
    POST  /orders/food                    place a canteen order (attender or nurse)
    POST  /orders/medicine                nurse-only pharmacy order (workflow toggle)
    POST  /nurse/attender-pin             nurse/admin sets the room's attender PIN
Dietician
    GET   /dietician/patients ;  POST /dietician/{room}/plan
Doctor
    GET   /doctor/patients ; GET /doctor/patient/{room} ; POST /doctor/patient/{room}/summary
Nurse
    GET   /nurse/emr/{room} ; GET /nurse/copilot
Patient
    GET   /patient/health ; POST /patient/chat ; POST /patient/chat/voice (AskCareVoice by voice)
Any signed-in user
    POST  /translate
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

import aiosqlite
import asyncpg
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from auth import AdminUser, CurrentUser, decode_token
from database import get_db
from pg_database import get_conn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["CareVoice v3"])


# ══════════════════════════════════════════════════════════════════════════════
#  Admin: workflow configuration
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/workflow")
async def get_workflow(_admin: AdminUser, db: aiosqlite.Connection = Depends(get_db)):
    from workflow_config import get_config, SCHEMA, PRESETS
    from config import settings
    cfg = await get_config(db)
    schema = {k: {"type": v["type"].__name__, "default": v["default"],
                  **{x: v[x] for x in ("min", "max", "choices") if x in v}} for k, v in SCHEMA.items()}
    return {"config": cfg, "schema": schema, "presets": PRESETS,
            "notes": {
                "llm_classifier": ("Uses Groq; transcripts leave the hospital network."
                                   if settings.GROQ_API_KEY else "No GROQ_API_KEY set: keyword classifier only."),
                "his_adapter": "local = demo EMR (sample data). fhir = FHIR R4 client (untested against a real vendor)"
                               + ("" if settings.HIS_BASE_URL else "; HIS_BASE_URL not configured"),
            }}


@router.patch("/admin/workflow")
async def patch_workflow(body: dict, admin: AdminUser, db: aiosqlite.Connection = Depends(get_db)):
    from workflow_config import set_many
    from config import settings
    if body.get("his_adapter") == "fhir" and not settings.HIS_BASE_URL:
        raise HTTPException(status_code=422, detail="Set HIS_BASE_URL on the server before choosing the FHIR adapter.")
    try:
        cfg = await set_many(db, body, admin["username"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if "llm_classifier" in body:
        settings.USE_LLM_CLASSIFIER = bool(body["llm_classifier"]) and bool(settings.GROQ_API_KEY)
    logger.info("Admin %s changed workflow: %s", admin["username"], body)
    return {"config": cfg}


@router.post("/admin/workflow/preset/{name}")
async def apply_preset(name: str, admin: AdminUser, db: aiosqlite.Connection = Depends(get_db)):
    from workflow_config import apply_preset as _apply
    try:
        return {"config": await _apply(db, name, admin["username"])}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/workflow/flags")
async def workflow_flags(_user: CurrentUser, db: aiosqlite.Connection = Depends(get_db)):
    """Read-only workflow toggles for any signed-in staff UI (no tunables)."""
    from workflow_config import get_config
    c = await get_config(db)
    return {k: c[k] for k in ("canteen_routing", "housekeeping_routing", "pharmacy_orders_nurse_only",
                              "dietician_augments", "attender_can_order", "nurse_approval_food", "languages")}


@router.get("/admin/workflow/log")
async def workflow_log(_admin: AdminUser, db: aiosqlite.Connection = Depends(get_db)):
    from workflow_config import get_log
    return {"log": await get_log(db)}


# ── Admin: routing statistics ─────────────────────────────────────────────────

@router.get("/admin/routing-stats")
async def routing_stats(
    _admin: AdminUser,
    days: int = Query(default=30, ge=1, le=365),
    format: str = Query(default="json"),
    simulated: bool = Query(default=False, description="Show ONLY the SIMULATED rehearsal rows"),
    db: aiosqlite.Connection = Depends(get_db),
):
    import routing_stats as rs
    from workflow_config import get_config
    cfg = await get_config(db)
    stats = await rs.compute(db, days=days, minutes_per_task=float(cfg.get("minutes_saved_per_task", 4.0)),
                             simulated=simulated)
    if format == "csv":
        return PlainTextResponse(rs.to_csv(stats), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=routing-stats.csv"})
    return stats


@router.post("/admin/routing-stats/demo")
async def make_demo(_admin: AdminUser, n: int = Query(default=120, ge=1, le=2000),
                    db: aiosqlite.Connection = Depends(get_db)):
    import routing_stats as rs
    return {"inserted": await rs.generate_demo(db, n), "label": "SIMULATED — excluded from real statistics"}


@router.delete("/admin/routing-stats/demo")
async def clear_demo(_admin: AdminUser, db: aiosqlite.Connection = Depends(get_db)):
    import routing_stats as rs
    return {"deleted": await rs.clear_demo(db)}


@router.get("/admin/llm-audit")
async def llm_audit(_admin: AdminUser, limit: int = Query(default=100, le=500),
                    db: aiosqlite.Connection = Depends(get_db)):
    async with db.execute(
        "SELECT id, username, role, purpose, provider, model, flags, latency_ms, status, created_at, "
        "substr(prompt,1,300) AS prompt, substr(response,1,500) AS response FROM llm_audit "
        "ORDER BY id DESC LIMIT ?", (limit,)) as cur:
        return {"entries": [dict(r) for r in await cur.fetchall()]}


# ══════════════════════════════════════════════════════════════════════════════
#  Actor resolution (staff JWT or short-lived attender token)
# ══════════════════════════════════════════════════════════════════════════════

def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in required")
    return authorization[7:].strip()


async def actor(authorization: str | None = Header(default=None)) -> dict:
    """Any signed-in user, including an attender token (no DB user row)."""
    payload = decode_token(_bearer(authorization))
    if payload.get("role") == "attender":
        return {"username": payload["sub"], "role": "attender", "room": payload.get("room"),
                "full_name": payload.get("full_name", "Attender")}
    from pg_database import get_user_by_username
    import pg_database
    async with pg_database._pool.acquire() as conn:
        row = await get_user_by_username(conn, payload.get("sub"))
        user = None
        if row is not None:
            user = {"username": row["username"], "role": row["role"], "full_name": row["full_name"],
                    "user_id": row["id"]}
            if row["role"] == "patient":
                p = await conn.fetchrow("SELECT room_number FROM patients WHERE user_id=$1 AND is_discharged=FALSE",
                                        row["id"])
                user["room"] = p["room_number"] if p else None
    if user is None:
        raise HTTPException(status_code=401, detail="User not found or deactivated")
    return user


async def _covers(user: dict, room: str) -> bool:
    if user["role"] in ("admin", "doctor", "dietician"):
        return True
    if user["role"] == "nurse":
        import pg_database
        async with pg_database._pool.acquire() as conn:
            return await pg_database.nurse_covers_room(conn, user["username"], room)
    if user["role"] in ("attender", "patient"):
        return (user.get("room") or "").upper() == room.upper()
    return False


async def _require_covers(user: dict, room: str):
    if not await _covers(user, room):
        raise HTTPException(status_code=403, detail=f"Not permitted for room {room}")


# ══════════════════════════════════════════════════════════════════════════════
#  Attender + food orders
# ══════════════════════════════════════════════════════════════════════════════

class UnlockBody(BaseModel):
    pin: str = Field(..., min_length=4, max_length=8)


@router.post("/attender/unlock")
async def attender_unlock(body: UnlockBody, who: dict = Depends(actor),
                          db: aiosqlite.Connection = Depends(get_db)):
    """Called from the patient's dashboard: the attender types the room PIN set by
    the nurse. Returns a 2-hour attender token scoped to that room."""
    if who["role"] != "patient" or not who.get("room"):
        raise HTTPException(status_code=403, detail="Open this on the patient's device")
    from workflow_config import get_config
    if not (await get_config(db)).get("attender_can_order", True):
        raise HTTPException(status_code=403, detail="Attender ordering is switched off by the hospital")
    from pg_database import verify_attender_pin
    ok, info = await verify_attender_pin(who["room"], body.pin)
    if not ok:
        msg = {"locked": "Too many wrong PINs. Try again in 15 minutes.",
               "no_pin": "No attender PIN set for this room. Ask the nurse."}.get(info, "Wrong PIN")
        raise HTTPException(status_code=403, detail=msg)
    from jose import jwt
    from config import settings
    from datetime import datetime, timezone
    token = jwt.encode({"sub": f"attender:{who['room']}", "role": "attender", "room": who["room"],
                        "full_name": info, "exp": datetime.now(timezone.utc) + timedelta(hours=2)},
                       settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return {"attender_token": token, "room": who["room"], "expires_in": 7200}


class PinBody(BaseModel):
    room: str
    pin: str = Field(..., min_length=4, max_length=8, pattern=r"^\d+$")
    name: str = Field(default="Attender", max_length=60)


@router.post("/nurse/attender-pin")
async def set_attender_pin(body: PinBody, who: dict = Depends(actor)):
    if who["role"] not in ("nurse", "admin"):
        raise HTTPException(status_code=403, detail="Nurse or admin only")
    await _require_covers(who, body.room)
    from pg_database import set_attender_pin as _set
    await _set(body.room, body.pin, body.name, who["username"])
    return {"ok": True, "room": body.room}


@router.get("/orders/menu/{room}")
async def menu_for_room(room: str, who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db)):
    if who["role"] == "patient":
        raise HTTPException(status_code=403, detail="Patients cannot order food. Ask your attender or nurse.")
    await _require_covers(who, room)
    import diet_rules
    from workflow_config import get_config
    from pg_database import get_menu_raw, get_diet_restriction_for_room, get_dietician_plan
    cfg = await get_config(db)
    if who["role"] == "attender":
        # Attender orders his own meal: the whole menu, no patient diet applied.
        menu = [{**m, "allowed": True, "recommended": False, "code": "ok", "reason": ""}
                for m in await get_menu_raw()]
        return {"room": room, "for": "attender", "diet_restriction": None, "diet_label": None,
                "dietician_plan": None, "nurse_approval": False,
                "canteen_enabled": cfg.get("canteen_routing", True), "menu": menu}
    diet = (await get_diet_restriction_for_room(room)) or "none"
    plan = await get_dietician_plan(room) if cfg.get("dietician_augments", True) else None
    return {"room": room, "diet_restriction": diet, "diet_label": diet_rules.DIET_LABELS.get(diet, diet),
            "dietician_plan": plan, "nurse_approval": cfg.get("nurse_approval_food", False),
            "canteen_enabled": cfg.get("canteen_routing", True),
            "menu": diet_rules.annotate_menu(await get_menu_raw(), diet, plan)}


class FoodOrderBody(BaseModel):
    room: str
    item_id: int
    notes: str | None = Field(default=None, max_length=200)


@router.post("/orders/food")
async def order_food(body: FoodOrderBody, who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db)):
    if who["role"] not in ("attender", "nurse", "admin"):
        raise HTTPException(status_code=403, detail="Only the attender or the nurse can order food")
    await _require_covers(who, body.room)
    from department_dispatch import place_food_order, OrderError
    try:
        return await place_food_order(db, room_id=body.room, item_id=body.item_id, by=who["username"],
                                      by_role="nurse" if who["role"] == "admin" else who["role"],
                                      notes=body.notes)
    except OrderError as e:
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


class MedOrderBody(BaseModel):
    room: str
    name: str = Field(..., min_length=1, max_length=80)
    dosage: str = Field(default="", max_length=120)


@router.post("/orders/medicine")
async def order_medicine(body: MedOrderBody, who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db)):
    if who["role"] != "nurse":
        raise HTTPException(status_code=403, detail="Only a nurse can place medicine orders")
    await _require_covers(who, body.room)
    from department_dispatch import place_medicine_order, OrderError
    try:
        return await place_medicine_order(db, room_id=body.room, name=body.name, dosage=body.dosage,
                                          by=who["username"])
    except OrderError as e:
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})


@router.get("/orders/room/{room}")
async def orders_for_room(room: str, who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db)):
    """Order status for the patient dashboard / attender / nurse."""
    await _require_covers(who, room)
    async with db.execute(
        "SELECT id, kind, item_name, order_status, ordered_by_role, created_at, updated_at "
        "FROM department_requests WHERE room_id = ? AND kind IN ('food','medicine') AND is_demo = 0 "
        "ORDER BY created_at DESC LIMIT 20", (room,)) as cur:
        return {"orders": [dict(r) for r in await cur.fetchall()]}


# ══════════════════════════════════════════════════════════════════════════════
#  Dietician
# ══════════════════════════════════════════════════════════════════════════════

def _need(who: dict, *roles):
    if who["role"] not in roles:
        raise HTTPException(status_code=403, detail=f"Requires role: {', '.join(roles)}")


@router.get("/dietician/patients")
async def dietician_patients(who: dict = Depends(actor), conn: asyncpg.Connection = Depends(get_conn)):
    _need(who, "dietician", "admin", "nurse", "doctor")
    rows = await conn.fetch(
        "SELECT p.room_number, p.full_name, p.diagnosis, p.diet_restriction, d.allowed_items, d.avoid_items, "
        "d.notes, d.food_cleared, d.updated_by, d.updated_at FROM patients p "
        "LEFT JOIN dietician_plans d ON d.room_number = p.room_number "
        "WHERE p.is_discharged = FALSE ORDER BY p.room_number")
    from pg_database import get_menu_raw
    import diet_rules
    return {"patients": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
                         for r in rows],
            "menu": await get_menu_raw(), "diet_labels": diet_rules.DIET_LABELS}


class PlanBody(BaseModel):
    allowed_items: list[str] = []
    avoid_items: list[str] = []
    notes: str = Field(default="", max_length=500)
    food_cleared: bool = False
    diet_restriction: str | None = None


@router.post("/dietician/{room}/plan")
async def set_plan(room: str, body: PlanBody, who: dict = Depends(actor)):
    _need(who, "dietician", "admin")
    import diet_rules
    from pg_database import set_dietician_plan
    import pg_database
    if body.diet_restriction:
        if body.diet_restriction not in diet_rules.DIET_LABELS:
            raise HTTPException(status_code=422, detail="Unknown diet")
        async with pg_database._pool.acquire() as conn:
            await pg_database.set_patient_diet_by_room(conn, room, body.diet_restriction)
    plan = await set_dietician_plan(room, allowed_items=",".join(body.allowed_items),
                                    avoid_items=",".join(body.avoid_items), notes=body.notes,
                                    food_cleared=body.food_cleared, by=who["username"])
    return {"ok": True, "plan": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in plan.items()}}


# ══════════════════════════════════════════════════════════════════════════════
#  Doctor + nurse copilot + EMR
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/doctor/patients")
async def doctor_patients(who: dict = Depends(actor), conn: asyncpg.Connection = Depends(get_conn)):
    _need(who, "doctor", "admin")
    rows = await conn.fetch(
        "SELECT room_number, full_name, age, diagnosis, attending, acuity, diet_restriction "
        "FROM patients WHERE is_discharged = FALSE ORDER BY room_number")
    import care_agent
    out = []
    for r in rows:
        d = dict(r)
        d["trends"] = care_agent.trends_from_alerts(await care_agent._alert_rows(r["room_number"]))
        out.append(d)
    return {"patients": out}


@router.get("/doctor/patient/{room}")
async def doctor_record(room: str, who: dict = Depends(actor)):
    _need(who, "doctor", "admin")
    from his_adapter import get_adapter
    import care_agent
    emr = await get_adapter()
    rec = await emr.record(room)
    rec["trends"] = care_agent.trends_from_alerts(await care_agent._alert_rows(room))
    rec["recent_calls"] = (await care_agent._alert_rows(room, 25))
    return rec


class SummaryBody(BaseModel):
    save_to_emr: bool = False


@router.post("/doctor/patient/{room}/summary")
async def doctor_summary(room: str, body: SummaryBody, who: dict = Depends(actor)):
    _need(who, "doctor", "admin")
    import care_agent
    out = await care_agent.doctor_summary(room, user=who["username"], role=who["role"])
    if body.save_to_emr:
        from his_adapter import get_adapter
        out["saved"] = await (await get_adapter()).write_document(
            room, "patient_summary", "Patient summary (AI draft, clinician reviewed)",
            out["summary"], who["full_name"])
    return out


@router.get("/nurse/emr/{room}")
async def nurse_emr(room: str, who: dict = Depends(actor)):
    _need(who, "nurse", "admin")
    await _require_covers(who, room)
    from his_adapter import get_adapter
    emr = await get_adapter()
    kp = await emr.key_parameters(room)
    kp["conditions"] = await emr.conditions(room)
    kp["medications"] = await emr.medications(room)
    return kp


@router.get("/nurse/copilot")
async def nurse_copilot(who: dict = Depends(actor), conn: asyncpg.Connection = Depends(get_conn)):
    _need(who, "nurse", "admin")
    from pg_database import get_patients_for_nurse, get_all_patients
    rows = await (get_all_patients(conn) if who["role"] == "admin" else get_patients_for_nurse(conn, who["username"]))
    import care_agent
    out = await care_agent.shift_summary(
        [{"room_number": r["room_number"], "full_name": r["full_name"]} for r in rows],
        user=who["username"], role=who["role"])
    try:
        from his_adapter import get_adapter
        emr = await get_adapter()
        for p in out["patients"]:
            await emr.write_document(p["room_number"], "shift_summary", "Shift summary (AI draft)",
                                     p["summary"], who["full_name"])
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Patient: key parameters + supportive chat
# ══════════════════════════════════════════════════════════════════════════════

_LAST_CHAT_ALERT: dict[str, float] = {}


@router.get("/patient/health")
async def patient_health(who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db)):
    if who["role"] not in ("patient", "attender"):
        raise HTTPException(status_code=403, detail="Patient only")
    if not who.get("room"):
        raise HTTPException(status_code=404, detail="No active admission")
    from his_adapter import get_adapter
    import diet_rules
    emr = await get_adapter()
    kp = await emr.key_parameters(who["room"])
    kp["diet_label"] = diet_rules.DIET_LABELS.get(kp["diet_restriction"], kp["diet_restriction"])
    kp["source_label"] = emr.label
    async with db.execute(
        "SELECT kind, item_name, order_status, created_at FROM department_requests "
        "WHERE room_id = ? AND kind = 'food' AND COALESCE(ordered_by_role, '') != 'attender' "
        "ORDER BY created_at DESC LIMIT 5", (who["room"],)) as cur:
        kp["food_orders"] = [dict(r) for r in await cur.fetchall()]
    return kp


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=600)


async def _chat_emergency_alert(db, room: str, message: str, out: dict) -> None:
    """Emergency language in the chat raises a normal nurse alert (rate-limited
    to one per room per minute) through the usual dispatch."""
    now = time.time()
    if now - _LAST_CHAT_ALERT.get(room, 0) <= 60:
        return
    _LAST_CHAT_ALERT[room] = now
    from database import insert_alert
    from pg_database import get_patient_name_by_room
    name = await get_patient_name_by_room(room)
    aid = await insert_alert(
        db, room_id=room, priority="Critical", intent="Emergency", distress_score=0.0,
        transcript=f"[CHAT] {message}", patient_name=name,
        summary="emergency language in the care chat",
        alert_message=f"Patient {name or ''} in Room {room} — emergency language in the care chat.",
        emotion="panicked")
    await db.commit()
    try:
        from routing import route_alert
        await route_alert(db, room_id=room, alert_id=aid, payload_json="")
    except Exception:
        logger.exception("Chat emergency dispatch failed")
    out["alert_id"] = aid


@router.post("/patient/chat")
async def patient_chat(body: ChatBody, who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db)):
    if who["role"] != "patient" or not who.get("room"):
        raise HTTPException(status_code=403, detail="Patient only")
    import care_agent
    room = who["room"]
    out = await care_agent.patient_chat(room, body.message, user=who["username"])
    if out.get("emergency"):
        await _chat_emergency_alert(db, room, body.message, out)
    return out


_VOICE_MAX_BYTES = 5 * 1024 * 1024


@router.post("/patient/chat/voice")
async def patient_chat_voice(
    audio: UploadFile = File(..., description="Recorded question (webm/ogg/m4a/wav)"),
    lang: str = Form("", description="Optional language hint: en | hi"),
    who: dict = Depends(actor), db: aiosqlite.Connection = Depends(get_db),
):
    """AskCareVoice by voice: speech-to-text (server Whisper), then the same
    guarded chat as /patient/chat. Returns the transcript and the reply; the page
    reads the reply aloud."""
    if who["role"] != "patient" or not who.get("room"):
        raise HTTPException(status_code=403, detail="Patient only")
    data = await audio.read(_VOICE_MAX_BYTES + 1)
    if len(data) > _VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Recording too long")
    if len(data) < 200:
        raise HTTPException(status_code=400, detail="Recording is empty")
    hint = lang.strip().lower() if lang.strip().lower() in ("en", "hi") else None

    import asyncio as _aio
    from config import settings as _s
    if _s.USE_STUB:
        transcript, detected = "what is my blood pressure", "en"
    else:
        from pipeline import _transcribe
        try:
            transcript, detected, *_ = await _aio.wait_for(
                _aio.to_thread(_transcribe, data, hint), timeout=45)
        except Exception:
            logger.exception("AskCareVoice speech-to-text failed")
            raise HTTPException(status_code=503, detail="Could not understand the recording. Please try again.")
    transcript = (transcript or "").strip()
    if transcript in ("", "[silence]"):
        return {"transcript": "", "reply": "I didn't catch that. Please hold the mic button and speak again.",
                "provider": "none", "emergency": False, "lang": hint or "en", "no_speech": True}

    import care_agent
    room = who["room"]
    out = await care_agent.patient_chat(room, transcript, user=who["username"],
                                        lang=detected if detected in ("en", "hi") else None)
    out["transcript"] = transcript
    if out.get("emergency"):
        await _chat_emergency_alert(db, room, transcript, out)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Translation (voice notes): LLM first, the app falls back to ML Kit
# ══════════════════════════════════════════════════════════════════════════════

class TranslateBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    source: str = Field(..., pattern=r"^(en|hi|de|kn)$")
    target: str = Field(..., pattern=r"^(en|hi|de|kn)$")


@router.post("/translate")
async def translate(body: TranslateBody, who: dict = Depends(actor)):
    import care_agent
    out = await care_agent.translate(body.text, body.source, body.target, user=who["username"])
    if not out.get("text"):
        raise HTTPException(status_code=503, detail="Translation service unavailable")
    return out
