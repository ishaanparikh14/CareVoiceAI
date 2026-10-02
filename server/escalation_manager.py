"""PostgreSQL-backed Urgent -> Critical escalation worker."""
from __future__ import annotations
import asyncio
import logging
import asyncpg
from models import AlertResponse, WsAlertUpdate, Priority
from database import get_db, get_alert_by_id, get_expired_urgent_ids, update_alert_priority_if_expired

logger = logging.getLogger(__name__)
_broadcast = None

def set_broadcaster(fn) -> None:
    global _broadcast
    _broadcast = fn

async def run_once() -> list[int]:
    escalated_ids: list[int] = []
    # One checked-out PostgreSQL connection per pass; each guarded UPDATE is atomic.
    async for conn in get_db():
        candidate_ids = await get_expired_urgent_ids(conn)
        for alert_id in candidate_ids:
            if await update_alert_priority_if_expired(conn, alert_id):
                escalated_ids.append(alert_id)
        for alert_id in escalated_ids:
            row = await get_alert_by_id(conn, alert_id)
            if row is None:
                continue
            alert = AlertResponse.from_db_row(row)
            logger.warning("Alert %d escalated Urgent -> Critical; room=%s", alert_id, alert.room_id)
            if _broadcast:
                await _broadcast(WsAlertUpdate.from_alert_response(alert).model_dump_json())
        break
    return escalated_ids

async def run_forever() -> None:
    from config import settings
    logger.info("Escalation manager started (timeout=%ss, check interval=%ss)", settings.URGENT_ESCALATION_TIMEOUT_SECONDS, settings.ESCALATION_CHECK_INTERVAL_SECONDS)
    try:
        while True:
            try:
                await run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Escalation pass failed — will retry")
            await asyncio.sleep(settings.ESCALATION_CHECK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Escalation manager stopped")
        raise
