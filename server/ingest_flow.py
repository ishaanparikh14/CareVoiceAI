# -*- coding: utf-8 -*-
"""
ingest_flow.py — the single post-pipeline path shared by POST /audio/ingest and
the always-on patient WebSocket.

  pipeline result (transcript + model intent)
        │
        ▼
  triage  ── nurse-bound? ─────────────► nurse dispatch (router skipped)
        │
        ├─ facility (ground support) ──► housekeeping chain (never the nurse)
        └─ food ───────────────────────► Routine nurse-visible food request;
                                          attender / nurse places the order

Triage runs BEFORE the alert row is written, so a DistilBERT mislabel can no
longer store a facility or food request as Critical / Urgent.
"""

from __future__ import annotations

import logging

import aiosqlite

from models import AlertResponse, Intent, Priority, WsAlertPayload

logger = logging.getLogger(__name__)


async def handle_pipeline_result(
    db: aiosqlite.Connection,
    *,
    result,
    room_id: str,
    wav_path: str | None,
    language: str | None,
    broadcast=None,
) -> dict:
    """Triage, persist and dispatch one utterance. Returns a dict the callers use
    to build their responses: alert_id, priority, intent, should_alert,
    request_class, departments, nurse_paged, alert_resp."""
    from database import insert_alert, utcnow
    from pg_database import get_patient_name_by_room
    from triage import triage
    from workflow_config import get_config

    cfg = await get_config(db)
    model_intent = result.intent
    try:
        tri = await triage(
            result.transcript, model_intent, cfg=cfg,
            stt_avg_logprob=getattr(result, "stt_avg_logprob", None),
            stt_corrected=getattr(result, "stt_corrected", False),
        )
    except Exception:
        logger.exception("Triage failed — defaulting to nurse")
        from triage import Triage
        tri = Triage("nurse", model_intent, 1.0, "triage error — nurse", "fallback")

    # Non-clinical requests are stored as Routine. A clinical label that triage
    # kept with the nurse keeps the pipeline's priority unchanged.
    intent = tri.intent
    priority = result.priority
    summary = result.summary
    alert_message = result.alert_message
    should_alert = result.should_alert
    if tri.request_class == "none" and (result.emotion or "").lower() in ("distressed", "panicked"):
        # The words were small talk but the voice sounds distressed: nurse.
        from triage import Triage
        tri = Triage("nurse", model_intent, 1.0, "small talk but distressed voice — nurse", "fallback")
    if tri.request_class == "none":
        # Not a request: kept for audit, hidden from the nurse, nobody paged.
        should_alert = False
        priority = Priority.ROUTINE
        summary = "no request (small talk)"
    elif tri.request_class in ("facility", "food"):
        priority = Priority.ROUTINE
        should_alert = True
        if tri.request_class == "facility":
            summary = "needs room / facility support"
        else:
            summary = "is asking for food or water (order via attender or nurse)"
        patient_name_tmp = await get_patient_name_by_room(room_id)
        who = f"Patient {patient_name_tmp} in Room {room_id}" if patient_name_tmp else f"Room {room_id}"
        alert_message = f"{who} — {summary}."
    elif intent != model_intent:
        # Triage re-labelled a nurse-bound request (e.g. personal care -> Hygiene).
        import priority_engine
        priority = priority_engine.compute_priority(intent, result.transcript)

    patient_name = await get_patient_name_by_room(room_id)
    alert_id = await insert_alert(
        db,
        room_id=room_id, priority=priority.value, intent=intent.value,
        distress_score=result.distress_score, transcript=result.transcript,
        wav_path=wav_path, language=language, patient_name=patient_name,
        summary=summary, emotion=result.emotion, alert_message=alert_message,
        request_class=tri.request_class, model_intent=model_intent.value,
        visible_to_nurse=tri.request_class != "none",
    )
    await db.commit()

    alert_resp = AlertResponse(
        id=alert_id, room_id=room_id, priority=priority, intent=intent,
        distress_score=result.distress_score, transcript=result.transcript,
        wav_path=wav_path, acknowledged=False, created_at=utcnow(), language=language,
        patient_name=patient_name, summary=summary, emotion=result.emotion,
        alert_message=alert_message,
    )

    nurse_primary, departments = True, []
    if tri.request_class == "none":
        nurse_primary = False
    else:
        try:
            from department_dispatch import process_alert_routing
            res = await process_alert_routing(
                db, alert_id=alert_id, room_id=room_id, transcript=result.transcript,
                intent=intent, request_class=tri.request_class, confidence=tri.confidence,
                reason=tri.reason, model_version=tri.model_version,
            )
            nurse_primary = res.get("nurse_primary", True)
            departments = res.get("departments", [])
        except Exception:
            logger.exception("Department routing failed for alert %d — nurse", alert_id)
            nurse_primary = True

    nurse_paged = False
    if nurse_primary and should_alert:
        payload = WsAlertPayload.from_alert_response(alert_resp)
        try:
            from routing import route_alert
            await route_alert(db, room_id=room_id, alert_id=alert_id, payload_json=payload.model_dump_json())
            nurse_paged = True
        except Exception:
            logger.exception("Nurse dispatch failed for alert %d — broadcasting", alert_id)
            if broadcast is not None:
                await broadcast(payload.model_dump_json())
                nurse_paged = True

    logger.info("[INGEST] alert=%d room=%s model=%s -> class=%s intent=%s priority=%s nurse=%s depts=%s (%s)",
                alert_id, room_id, model_intent.value, tri.request_class, intent.value,
                priority.value, nurse_paged, departments, tri.reason)

    # Morale-boosting acknowledgement for the patient (deterministic, instant).
    try:
        from care_agent import supportive_line, _lang_of
        from routers import patient_ws
        if nurse_paged:
            line = await supportive_line(result.emotion, _lang_of(result.transcript))
            await patient_ws.notify_room(room_id, {"event": "care_message", "message": line})
    except Exception:
        pass

    return {
        "alert_id": alert_id, "priority": priority, "intent": intent,
        "should_alert": should_alert, "request_class": tri.request_class,
        "departments": departments, "nurse_paged": nurse_paged,
        "alert_resp": alert_resp,
    }
