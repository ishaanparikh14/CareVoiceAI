# -*- coding: utf-8 -*-
"""Pure unit tests for the v3 modules: triage, diet rules, workflow config,
care-agent guardrails/trends and routing stats on an empty database."""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import database  # noqa: E402
from models import Intent  # noqa: E402
import triage  # noqa: E402
import diet_rules  # noqa: E402
import workflow_config  # noqa: E402
import care_agent  # noqa: E402


# ── Triage ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,label", [
    ("water spilled on the floor", Intent.MEDICATION),   # DistilBERT mislabel
    ("the AC is not working", Intent.OTHER),
    ("please clean the bathroom", Intent.OTHER),
])
def test_facility_requests_go_to_housekeeping(text, label):
    t = triage.classify_text(text, label)
    assert t.request_class == "facility"
    assert t.intent == Intent.FACILITY


@pytest.mark.parametrize("text,label", [
    ("I can't breathe", Intent.OTHER),
    ("my chest hurts and the AC is broken", Intent.OTHER),
    ("I need my medicine", Intent.MEDICATION),
    ("I am in a lot of pain", Intent.PAIN),
    ("please help me bathe", Intent.HYGIENE),
])
def test_nurse_requests_skip_the_router(text, label):
    assert triage.classify_text(text, label).request_class == "nurse"


def test_food_request_is_food_class():
    t = triage.classify_text("I am hungry, can I get some food", Intent.FOOD_WATER)
    assert t.request_class == "food"


def test_facility_below_tau_r_falls_back_to_nurse():
    t = triage.classify_text("water spilled on the floor", Intent.OTHER, tau_r=0.95)
    assert t.request_class == "nurse"


def test_low_stt_confidence_goes_to_nurse():
    t = triage.classify_text("the AC is not working", Intent.OTHER, stt_avg_logprob=-1.5)
    assert t.request_class == "nurse"


# ── Diet rules ────────────────────────────────────────────────────────────────

VEG = {"name": "Dal rice", "tags": "veg_only,jain,green_veg"}
CHICKEN = {"name": "Chicken curry", "tags": ""}


def test_hard_diet_blocks_unsuitable_item():
    assert diet_rules.check_item(CHICKEN, "veg_only")["code"] == "diet_violation"
    assert diet_rules.check_item(VEG, "veg_only")["allowed"] is True


def test_npo_needs_clearance():
    assert diet_rules.check_item(VEG, "npo")["code"] == "clearance_required"
    assert diet_rules.check_item(VEG, "npo", {"food_cleared": True})["allowed"] is True


def test_dietician_avoid_and_allowed_lists():
    assert diet_rules.check_item(VEG, "none", {"avoid_items": "dal rice"})["allowed"] is False
    assert diet_rules.check_item(CHICKEN, "none", {"allowed_items": ["dal rice"]})["allowed"] is False


def test_green_veg_recommendation():
    assert diet_rules.check_item(VEG, "needs_green_veg")["recommended"] is True
    r = diet_rules.check_item(CHICKEN, "needs_green_veg")
    assert r["allowed"] is True and r["recommended"] is False


# ── Workflow config ───────────────────────────────────────────────────────────

def test_validate_rejects_bad_values():
    with pytest.raises(ValueError):
        workflow_config.validate("tau_r", 1.5)
    with pytest.raises(ValueError):
        workflow_config.validate("canteen_routing", "yes")
    with pytest.raises(ValueError):
        workflow_config.validate("languages", ["fr"])
    with pytest.raises(ValueError):
        workflow_config.validate("nope", True)
    assert workflow_config.validate("t_esc_seconds", 60) == 60


def test_presets_and_log():
    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            config.settings.DB_PATH = Path(os.path.join(tmp, "wf.db"))
            await database.init_db()
            async with aiosqlite.connect(config.settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                await workflow_config.set_many(db, {"tau_r": 0.7}, "admin")
                cfg = await workflow_config.apply_preset(db, "small_clinic", "admin")
                assert cfg["housekeeping_routing"] is False
                assert cfg["tau_r"] == 0.7          # preset leaves tunables alone
                cfg = await workflow_config.apply_preset(db, "multi_specialty", "admin")
                assert cfg["housekeeping_routing"] is True
                assert cfg["pharmacy_orders_nurse_only"] is True
                log = await workflow_config.get_log(db)
                assert any(r["key"] == "housekeeping_routing" for r in log)
                with pytest.raises(ValueError):
                    await workflow_config.apply_preset(db, "unknown", "admin")
    asyncio.run(body())


# ── Care agent guardrails ─────────────────────────────────────────────────────

def test_check_output_blocks_doses_and_diagnoses_for_patients():
    _, flags = care_agent.check_output("Take 500 mg of paracetamol.")
    assert flags == ["output_blocked"]
    _, flags = care_agent.check_output("You should stop taking your medicine.")
    assert flags == ["output_blocked"]
    text, flags = care_agent.check_output("You are doing well today. Keep resting.")
    assert flags == [] and text.startswith("You are doing well")


def test_check_output_allows_chart_doses_for_doctor():
    _, flags = care_agent.check_output("Metformin 500 mg twice daily.", allow_doses=True)
    assert flags == []


def test_is_emergency():
    assert care_agent.is_emergency("I can't breathe")
    assert not care_agent.is_emergency("what is my blood pressure")


def test_trends_from_alerts():
    now = datetime.now(timezone.utc)
    rows = [{"created_at": (now - timedelta(minutes=10 * i)).isoformat(),
             "request_class": "nurse",
             "emotion": ["calm", "anxious", "distressed", "panicked", "panicked", "panicked"][5 - i]}
            for i in range(6)]
    t = care_agent.trends_from_alerts(rows)
    assert t["nurse_calls_24h"] == 6
    assert t["rising_distress"] is True
    assert any("Repeat calls" in f for f in t["flags"])
    assert care_agent.trends_from_alerts([])["total_calls"] == 0


# ── Routing stats ─────────────────────────────────────────────────────────────

def test_routing_stats_empty_db_and_simulated():
    import routing_stats

    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            config.settings.DB_PATH = Path(os.path.join(tmp, "rs.db"))
            await database.init_db()
            async with aiosqlite.connect(config.settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                s = await routing_stats.compute(db)
                assert isinstance(s, dict)
                assert routing_stats.to_csv(s)
                n = await routing_stats.generate_demo(db, n=20)
                assert n > 0
                real = await routing_stats.compute(db)
                sim = await routing_stats.compute(db, simulated=True)
                assert real == s or real.get("total_calls", 0) == s.get("total_calls", 0)
                assert sim != real
                assert await routing_stats.clear_demo(db) > 0
    asyncio.run(body())


# ── Small talk (not a request) ───────────────────────────────────────────────

NOT_REQ = {"is_request": False, "department": "nurse", "needs_nurse": False,
           "confidence": 0.95, "reason": "small talk"}


def test_small_talk_is_dropped_when_safe():
    t = triage.from_llm("Nice weather!", Intent.EMOTIONAL_SUPPORT, NOT_REQ)
    assert t.request_class == "none"


@pytest.mark.parametrize("text,label", [
    ("Nice weather, but my chest hurts", Intent.EMOTIONAL_SUPPORT),   # clinical word
    ("I feel so lonely today", Intent.EMOTIONAL_SUPPORT),              # feeling
    ("mujhe ghabrahat ho rahi hai", Intent.EMOTIONAL_SUPPORT),         # Hindi feeling
    ("Nice weather", Intent.PAIN),                                     # pain label
    ("I can't breathe", Intent.OTHER),                                 # critical
])
def test_small_talk_gate_keeps_nurse(text, label):
    assert triage.from_llm(text, label, NOT_REQ).request_class == "nurse"


def test_small_talk_needs_confident_llm():
    low = dict(NOT_REQ, confidence=0.6)
    assert triage.from_llm("Nice weather!", Intent.EMOTIONAL_SUPPORT, low).request_class == "nurse"


def test_missing_is_request_means_request():
    llm = {"department": "nurse", "needs_nurse": False, "confidence": 0.95}
    assert triage.from_llm("Nice weather!", Intent.EMOTIONAL_SUPPORT, llm).request_class == "nurse"


def test_small_talk_with_borderline_stt_is_dropped():
    # Real clip: "Nice weather." scored -0.78 on prod Whisper.
    t = triage.from_llm("Nice weather.", Intent.EMOTIONAL_SUPPORT, NOT_REQ, stt_avg_logprob=-0.78)
    assert t.request_class == "none"


def test_small_talk_with_very_low_stt_goes_to_nurse():
    t = triage.from_llm("Nice weather.", Intent.EMOTIONAL_SUPPORT, NOT_REQ, stt_avg_logprob=-1.4)
    assert t.request_class == "nurse"


def test_low_stt_routable_request_still_goes_to_nurse():
    llm = {"is_request": True, "department": "housekeeping", "needs_nurse": False, "confidence": 0.95}
    t = triage.from_llm("my AC is not working", Intent.OTHER, llm, stt_avg_logprob=-0.82)
    assert t.request_class == "nurse"


# ── v3.1: per-nurse dashboard visibility (dispatch rules) ────────────────────

def _vis(nurse, row, *, free=True, rooms=("4B",), room="4B"):
    import dispatch
    return asyncio.run(dispatch.nurse_can_see(None, nurse, set(rooms), {"room_id": room}, row,
                                              i_am_free=free))


def test_primary_offer_only_for_primary():
    row = {"state": "pending_primary", "assignee": "anna", "primary_nurse": "anna"}
    assert _vis("anna", row)
    assert not _vis("ben", row, rooms=("5A",))


def test_busy_primary_does_not_see_broadcast_free_nurse_does():
    row = {"state": "broadcasting", "assignee": None, "primary_nurse": "anna"}
    assert not _vis("anna", row, free=False)            # engaged primary: not hers
    assert _vis("ben", row, rooms=("5A",))              # free nurse may take it
    assert not _vis("carol", row, rooms=("6A",), free=False)   # busy nurse: no


def test_needs_reassignment_stays_visible_to_free_nurses():
    row = {"state": "needs_admin", "assignee": None, "primary_nurse": "anna"}
    assert not _vis("anna", row, free=False)            # busy: not hers
    assert _vis("ben", row, rooms=("5A",))              # free: still sees it, can take it
    assert _vis("anna", row)                            # primary once free again


def test_primary_assigned_request_stays_until_ack():
    row = {"state": "assigned", "assignee": "anna", "primary_nurse": "anna"}
    assert _vis("anna", row, free=False)                # holding it makes her busy — still shown
    assert not _vis("ben", row, rooms=("5A",))


def test_assigned_to_other_hidden_even_if_my_patient():
    row = {"state": "assigned", "assignee": "ben", "primary_nurse": "anna"}
    assert not _vis("anna", row)
    assert _vis("ben", row, rooms=("5A",))              # temporarily mapped


def test_unpaged_alert_only_in_own_rooms():
    assert _vis("anna", None)
    assert not _vis("ben", None, rooms=("5A",))
