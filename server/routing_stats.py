# -*- coding: utf-8 -*-
"""
routing_stats.py — datapoints that show the value of the AI routing agent.

Every number is computed from logged rows (alerts, department_requests,
dept_events). Demo rows (is_demo = 1) are always excluded. When there is too
little data the result says so (low_data) instead of showing shaky ratios.
"Estimated nurse time saved" uses a configurable, clearly labelled assumption.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from statistics import median

import aiosqlite

LOW_DATA_THRESHOLD = 30


def _ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _ratio(n, d):
    return round(n / d, 3) if d else None


async def compute(db: aiosqlite.Connection, *, days: int = 30, minutes_per_task: float = 4.0,
                  utc_offset_min: int = 330, simulated: bool = False) -> dict:
    """simulated=False: real rows only (default). simulated=True: ONLY the
    flagged SIMULATED rows, for rehearsing the presentation; never mixed."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    demo = 1 if simulated else 0
    async with db.execute(
        "SELECT id, room_id, priority, intent, created_at, ack_at, acknowledged, request_class, "
        "model_intent, dept_routed_to, dept_confidence, dept_shadow, visible_to_nurse "
        "FROM alerts WHERE is_demo = ? AND created_at >= ? AND transcript NOT LIKE '[ORDER]%'", (demo, since)) as cur:
        alerts = [dict(r) for r in await cur.fetchall()]
    async with db.execute(
        "SELECT id, alert_id, department, kind, floor, state, created_at, acknowledged_at, done_at, "
        "escalated_at, confidence, ordered_by_role FROM department_requests "
        "WHERE is_demo = ? AND shadow = 0 AND created_at >= ?", (demo, since)) as cur:
        reqs = [dict(r) for r in await cur.fetchall()]
    async with db.execute(
        "SELECT e.event, e.request_id FROM dept_events e JOIN department_requests d ON d.id = e.request_id "
        "WHERE d.is_demo = ? AND e.created_at >= ?", (demo, since)) as cur:
        events = [dict(r) for r in await cur.fetchall()]

    total = len(alerts)
    by_class = {"nurse": 0, "facility": 0, "food": 0}
    for a in alerts:
        by_class[a.get("request_class") or "nurse"] = by_class.get(a.get("request_class") or "nurse", 0) + 1
    # Small talk is filtered, not routed: count it separately (no time saved claimed).
    small_talk = sum(1 for a in alerts if a.get("request_class") == "none")
    routed_away = sum(1 for a in alerts if not a.get("visible_to_nurse") and a.get("request_class") != "none")
    fallback = sum(1 for a in alerts if (a.get("request_class") or "nurse") == "facility" and a.get("visible_to_nurse"))
    mislabel_fixed = sum(1 for a in alerts if (a.get("request_class") or "nurse") != "nurse"
                         and a.get("model_intent") and a.get("model_intent") in
                         ("Emergency", "Pain", "Medication", "Mobility", "Emotional Support", "Hygiene"))

    per_intent: dict[str, int] = {}
    for a in alerts:
        per_intent[a["intent"]] = per_intent.get(a["intent"], 0) + 1

    conf_buckets = {"<0.6": 0, "0.6-0.75": 0, "0.75-0.9": 0, ">=0.9": 0}
    for a in alerts:
        if (a.get("request_class") or "nurse") == "nurse":
            continue
        c = a.get("dept_confidence")
        if c is None:
            continue
        key = "<0.6" if c < 0.6 else "0.6-0.75" if c < 0.75 else "0.75-0.9" if c < 0.9 else ">=0.9"
        conf_buckets[key] += 1

    def ack_minutes(start, end):
        s, e = _ts(start), _ts(end)
        return (e - s).total_seconds() / 60 if s and e and e >= s else None

    nurse_ack = [m for m in (ack_minutes(a["created_at"], a.get("ack_at")) for a in alerts
                             if a.get("visible_to_nurse") and a.get("acknowledged")) if m is not None]
    hk_ack = [m for m in (ack_minutes(r["created_at"], r.get("acknowledged_at") or r.get("done_at")) for r in reqs
                          if r["department"] == "housekeeping") if m is not None]
    food_done = [m for m in (ack_minutes(r["created_at"], r.get("done_at")) for r in reqs
                             if r["department"] == "canteen") if m is not None]

    overrides = sum(1 for e in events if e["event"] == "override")
    escalations = sum(1 for e in events if e["event"] == "escalated")
    unattended = sum(1 for e in events if e["event"] == "unattended")
    hk_total = sum(1 for r in reqs if r["department"] == "housekeeping")

    per_hour = [0] * 24
    night = 0
    for a in alerts:
        t = _ts(a["created_at"])
        if not t:
            continue
        h = (t + timedelta(minutes=utc_offset_min)).hour
        per_hour[h] += 1
        if h >= 22 or h < 6:
            night += 1

    per_floor: dict[str, int] = {}
    for r in reqs:
        if r["department"] == "housekeeping":
            per_floor[str(r.get("floor") or "?")] = per_floor.get(str(r.get("floor") or "?"), 0) + 1

    food_orders = [r for r in reqs if r["department"] == "canteen"]
    interruptions_avoided = routed_away
    low = total < LOW_DATA_THRESHOLD
    return {
        "simulated": simulated,
        "data_label": "SIMULATED — rehearsal data, not real" if simulated else "Real logged data",
        "window_days": days,
        "low_data": low,
        "low_data_note": (f"Only {total} requests in the last {days} days; ratios are indicative only."
                          if low else None),
        "total_requests": total,
        "by_class": by_class,
        "routed_away_from_nurse": routed_away,
        "small_talk_filtered": small_talk,
        "share_routed_away": _ratio(routed_away, total),
        "nurse_interruptions_avoided": interruptions_avoided,
        "fallback_to_nurse": fallback,
        "fallback_rate": _ratio(fallback, by_class.get("facility", 0)),
        "mislabels_corrected": mislabel_fixed,
        "per_intent": dict(sorted(per_intent.items(), key=lambda kv: -kv[1])),
        "routing_confidence": conf_buckets,
        "override_rate": _ratio(overrides, hk_total),
        "overrides": overrides,
        "escalations": escalations,
        "unattended": unattended,
        "median_minutes_to_ack": {
            "nurse": round(median(nurse_ack), 1) if nurse_ack else None,
            "housekeeping": round(median(hk_ack), 1) if hk_ack else None,
            "canteen_delivery": round(median(food_done), 1) if food_done else None,
        },
        "calls_per_hour": per_hour,
        "night_share": _ratio(night, total),
        "per_floor_housekeeping": per_floor,
        "food_orders": {"total": len(food_orders),
                        "by_attender": sum(1 for r in food_orders if r.get("ordered_by_role") == "attender"),
                        "by_nurse": sum(1 for r in food_orders if r.get("ordered_by_role") == "nurse")},
        "estimated_nurse_minutes_saved": round(interruptions_avoided * minutes_per_task, 1),
        "estimate_assumption": f"estimate (assumption): {minutes_per_task:g} nurse minutes per routed-away task",
    }


def to_csv(stats: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["metric", "value"])

    def emit(prefix, v):
        if isinstance(v, dict):
            for k, x in v.items():
                emit(f"{prefix}.{k}" if prefix else k, x)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                emit(f"{prefix}[{i}]", x)
        else:
            w.writerow([prefix, "" if v is None else v])
    emit("", stats)
    return buf.getvalue()


async def generate_demo(db: aiosqlite.Connection, n: int = 120) -> int:
    """Insert clearly-flagged SIMULATED rows (is_demo = 1) for presentation
    rehearsal. They never appear in real stats or in nurse/admin live lists."""
    import random
    from database import utcnow
    rnd = random.Random(42)
    now = datetime.now(timezone.utc)
    mix = [("nurse", "Pain"), ("nurse", "Medication"), ("nurse", "Emergency"), ("nurse", "Other"),
           ("facility", "Facility"), ("facility", "Facility"), ("food", "Food/Water")]
    made = 0
    for _ in range(n):
        cls, intent = rnd.choice(mix)
        ts = now - timedelta(minutes=rnd.randint(5, 60 * 24 * 14))
        created = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        ack = (ts + timedelta(minutes=rnd.uniform(1, 9))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        await db.execute(
            "INSERT INTO alerts (room_id, priority, intent, distress_score, transcript, created_at, acknowledged, "
            "ack_at, request_class, model_intent, visible_to_nurse, is_demo, dept_routed_to, dept_confidence) "
            "VALUES (?, ?, ?, 0, '[SIMULATED]', ?, 1, ?, ?, ?, ?, 1, ?, ?)",
            (rnd.choice(["4B", "4C", "5A", "5B", "6A"]), "Routine" if cls != "nurse" else "Urgent", intent,
             created, ack, cls, intent, 0 if cls == "facility" else 1,
             {"facility": "housekeeping", "food": "canteen"}.get(cls, "nurse"), rnd.uniform(0.6, 0.99)))
        made += 1
    await db.commit()
    return made


async def clear_demo(db: aiosqlite.Connection) -> int:
    cur = await db.execute("DELETE FROM alerts WHERE is_demo = 1")
    await db.commit()
    return cur.rowcount or 0
