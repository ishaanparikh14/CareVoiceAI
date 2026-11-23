# -*- coding: utf-8 -*-
"""
workflow_config.py — hospital-configurable workflow (admin editable).

Each hospital chooses which parts of the CareVoice workflow to integrate. The
settings live in SQLite (workflow_config) so changes apply on the next request
without a restart and survive restarts; every change is written to
workflow_config_log (who / key / old / new / when).

Keys (match the "Configurable workflow, set by the hospital" slide):

  canteen_routing              canteen orders (attender / nurse) enabled
  housekeeping_routing         ground-support requests go to the floor manager
  pharmacy_orders_nurse_only   nurse may place medicine orders to the pharmacy
                               (patients can NEVER request medicine from pharmacy)
  dietician_augments           dietician recommendations/clearance enforced on food
  attender_can_order           attender may order food from the patient dashboard
  nurse_approval_food          food orders wait for nurse approval before cooking
  housekeeping_nurse_fallback  a facility request the router cannot place at all
                               (no staff on the floor, router off) goes to the nurse
  tau_r                        routing confidence threshold (below -> nurse)
  t_esc_seconds                seconds before a housekeeping request moves up the chain
  languages                    languages the hospital supports
  llm_classifier               use the LLM classifier when a key is configured
  minutes_saved_per_task       assumption used for the "nurse time saved" estimate
  shadow                       log routing decisions only; everything goes to the nurse
  his_adapter                  local | fhir

Safety: if the store cannot be read, routing is treated as OFF (every request goes
to the nurse) and an error is logged, so a broken config can never hide a request.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

# key -> (type, default, constraints)
SCHEMA: dict[str, dict] = {
    "canteen_routing":             {"type": bool,  "default": True},
    "housekeeping_routing":        {"type": bool,  "default": True},
    "pharmacy_orders_nurse_only":  {"type": bool,  "default": False},
    "dietician_augments":          {"type": bool,  "default": True},
    "attender_can_order":          {"type": bool,  "default": True},
    "nurse_approval_food":         {"type": bool,  "default": False},
    "housekeeping_nurse_fallback": {"type": bool,  "default": True},
    "tau_r":                       {"type": float, "default": 0.6,   "min": 0.0, "max": 1.0},
    "t_esc_seconds":               {"type": int,   "default": 150,   "min": 10,  "max": 3600},
    "languages":                   {"type": list,  "default": ["en", "hi", "kn", "de"]},
    "llm_classifier":              {"type": bool,  "default": True},
    "minutes_saved_per_task":      {"type": float, "default": 4.0,   "min": 0.0, "max": 120.0},
    "shadow":                      {"type": bool,  "default": False},
    "his_adapter":                 {"type": str,   "default": "local", "choices": ["local", "fhir"]},
}

_ALLOWED_LANGS = {"en", "hi", "kn", "de"}

PRESETS: dict[str, dict] = {
    # Nurse-only workflow; canteen on, dietician and floor managers off.
    "small_clinic": {
        "canteen_routing": True,
        "housekeeping_routing": False,
        "pharmacy_orders_nurse_only": False,
        "dietician_augments": False,
        "attender_can_order": True,
        "nurse_approval_food": False,
    },
    # All departments on, dietician augmentation, one floor manager per floor.
    "multi_specialty": {
        "canteen_routing": True,
        "housekeeping_routing": True,
        "pharmacy_orders_nurse_only": True,
        "dietician_augments": True,
        "attender_can_order": True,
        "nurse_approval_food": False,
    },
}


def defaults() -> dict:
    out = {k: v["default"] for k, v in SCHEMA.items()}
    out["shadow"] = bool(settings.DEPARTMENT_ROUTING_SHADOW)
    out["llm_classifier"] = bool(settings.USE_LLM_CLASSIFIER)
    return out


def validate(key: str, value):
    """Return the coerced value or raise ValueError with a readable message."""
    if key not in SCHEMA:
        raise ValueError(f"unknown setting '{key}'")
    spec = SCHEMA[key]
    t = spec["type"]
    if t is bool:
        if not isinstance(value, bool):
            raise ValueError(f"'{key}' must be true or false")
        return value
    if t in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"'{key}' must be a number")
        v = t(value)
        if "min" in spec and v < spec["min"]:
            raise ValueError(f"'{key}' must be >= {spec['min']}")
        if "max" in spec and v > spec["max"]:
            raise ValueError(f"'{key}' must be <= {spec['max']}")
        return v
    if t is str:
        if not isinstance(value, str) or value not in spec.get("choices", [value]):
            raise ValueError(f"'{key}' must be one of {spec.get('choices')}")
        return value
    if t is list:
        if not isinstance(value, list) or not value:
            raise ValueError(f"'{key}' must be a non-empty list")
        bad = [x for x in value if x not in _ALLOWED_LANGS]
        if bad:
            raise ValueError(f"unsupported language(s): {bad}")
        return list(dict.fromkeys(value))
    raise ValueError(f"unsupported type for '{key}'")


async def get_config(db: aiosqlite.Connection) -> dict:
    """Effective configuration: defaults, then legacy department_flags (so the
    switches an admin already flipped before v3 stay in force), then workflow_config.

    Never raises: on a storage error routing is switched OFF (safe fallback)."""
    cfg = defaults()
    try:
        # Legacy overrides written by /admin/department/config before v3.
        try:
            async with db.execute("SELECT key, value FROM department_flags") as cur:
                legacy = {r["key"]: bool(r["value"]) for r in await cur.fetchall()}
        except Exception:
            legacy = {}
        if "shadow" in legacy:
            cfg["shadow"] = legacy["shadow"]
        if "canteen" in legacy:
            cfg["canteen_routing"] = legacy["canteen"]
        if "housekeeping" in legacy:
            cfg["housekeeping_routing"] = legacy["housekeeping"]
        if "pharmacy" in legacy:
            cfg["pharmacy_orders_nurse_only"] = legacy["pharmacy"]

        async with db.execute("SELECT key, value FROM workflow_config") as cur:
            rows = await cur.fetchall()
        for r in rows:
            if r["key"] in SCHEMA:
                try:
                    cfg[r["key"]] = validate(r["key"], json.loads(r["value"]))
                except Exception:
                    logger.warning("[WORKFLOW] ignoring invalid stored value for %s", r["key"])
    except Exception as exc:
        logger.error("[WORKFLOW] could not read config (%s) — routing OFF, all requests go to the nurse", exc)
        cfg["canteen_routing"] = False
        cfg["housekeeping_routing"] = False
        cfg["pharmacy_orders_nurse_only"] = False
        cfg["attender_can_order"] = False
        cfg["_degraded"] = True
    return cfg


async def set_many(db: aiosqlite.Connection, changes: dict, by: str | None) -> dict:
    """Validate and persist several settings, logging each real change.
    Raises ValueError (nothing is written) if any value is invalid."""
    from database import utcnow
    clean = {k: validate(k, v) for k, v in changes.items()}
    current = await get_config(db)
    now = utcnow()
    for k, v in clean.items():
        old = current.get(k)
        await db.execute(
            """
            INSERT INTO workflow_config (key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                updated_at=excluded.updated_at, updated_by=excluded.updated_by
            """,
            (k, json.dumps(v), now, by),
        )
        if old != v:
            await db.execute(
                "INSERT INTO workflow_config_log (key, old_value, new_value, changed_by, changed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (k, json.dumps(old), json.dumps(v), by, now),
            )
    await db.commit()
    return await get_config(db)


async def apply_preset(db: aiosqlite.Connection, name: str, by: str | None) -> dict:
    """Apply a named preset. Only the department toggles change; tau_r, T_esc,
    languages and the time-saved assumption are left exactly as they are."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset '{name}'")
    return await set_many(db, dict(PRESETS[name]), by)


async def get_log(db: aiosqlite.Connection, limit: int = 50) -> list[dict]:
    async with db.execute(
        "SELECT * FROM workflow_config_log ORDER BY id DESC LIMIT ?", (limit,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]
