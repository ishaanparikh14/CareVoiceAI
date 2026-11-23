# -*- coding: utf-8 -*-
"""
Golden-set + invariant tests for department_router.route_departments.

The module under test is PURE (no DB, no GPU, no network), so these run fast and
in isolation. The CORE INVARIANT: critical or clinical content NEVER leaves the
nurse — for every clinical/critical phrase, every low-confidence or Indic-
corrected case, the nurse must receive the FULL request (is_primary nurse
decision present).

Run:  python -m pytest tests/test_department_router.py -v   (from server/)
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Intent  # noqa: E402
from department_router import route_departments, Department  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def depts(decisions):
    return [d.department for d in decisions]


def primary(decisions):
    for d in decisions:
        if d.is_primary:
            return d
    return decisions[0]


def nurse_has_full_request(decisions):
    """Invariant check: the nurse is the primary (owns the full request)."""
    return any(d.department == Department.NURSE and d.is_primary for d in decisions)


def route(transcript, intent, **kw):
    # Default gate values mirror the config defaults.
    kw.setdefault("min_confidence", 0.85)
    kw.setdefault("min_stt_avg_logprob", -0.7)
    return route_departments(transcript, intent, **kw)


# ── SAFETY INVARIANT: clinical / critical content stays with the nurse ────────

CLINICAL_CASES = [
    ("I can't breathe", Intent.EMERGENCY),
    ("chest pain, help", Intent.EMERGENCY),
    ("my chest really hurts", Intent.PAIN),
    ("the pain is unbearable", Intent.PAIN),
    ("I feel like I'm going to faint", Intent.EMERGENCY),
    ("please call the doctor", Intent.INFORMATION),   # urgent marker
    ("I need my painkiller now", Intent.PAIN),         # pain intent → nurse
    ("help me to the toilet", Intent.MOBILITY),
    ("I'm scared and alone", Intent.EMOTIONAL_SUPPORT),
    ("I soiled the bed", Intent.HYGIENE),             # personal care
    ("help me bathe", Intent.HYGIENE),
    ("change my diaper", Intent.HYGIENE),
    # Hindi / German clinical
    ("साँस नहीं आ रही", Intent.EMERGENCY),
    ("kann nicht atmen", Intent.EMERGENCY),
]


@pytest.mark.parametrize("transcript,intent", CLINICAL_CASES)
def test_clinical_content_stays_with_nurse(transcript, intent):
    decisions = route(transcript, intent)
    assert nurse_has_full_request(decisions), (transcript, depts(decisions))


def test_medication_routes_to_pharmacy_as_order():
    # Medication requests go to the PHARMACY as a medicine order; the order flow
    # (create_medicine_order) then applies the general-vs-prescribed split and
    # the nurse-approval gate for prescribed/unknown meds. So routing lands on
    # pharmacy here — the safety gate lives in the order lifecycle, not the router.
    for t in ["give me my medicine", "my tablets are due", "I need my medication"]:
        decisions = route(t, Intent.MEDICATION)
        assert Department.PHARMACY in depts(decisions), t


# ── Low STT confidence / script correction → nurse ────────────────────────────

def test_low_stt_confidence_forces_nurse():
    # Would otherwise be a clean canteen request, but STT is unreliable.
    decisions = route("water please", Intent.FOOD_WATER, diet_restriction="none",
                      stt_avg_logprob=-1.5)
    assert decisions == [decisions[0]]
    assert decisions[0].department == Department.NURSE


def test_indic_correction_forces_nurse():
    decisions = route("bring me water", Intent.FOOD_WATER, diet_restriction="none",
                      stt_corrected=True)
    assert nurse_has_full_request(decisions)
    assert Department.CANTEEN not in depts(decisions)


# ── Pharmacy: ONLY supply/logistics, and nurse is still informed ──────────────

def test_pharmacy_supply_routes_to_pharmacy():
    decisions = route("my prescription hasn't arrived from the pharmacy", Intent.MEDICATION)
    assert Department.PHARMACY in depts(decisions)


def test_refill_is_supply():
    decisions = route("I need a refill of my medicine", Intent.MEDICATION)
    assert Department.PHARMACY in depts(decisions)


# ── Hygiene split: environmental → housekeeping; personal → nurse ─────────────

def test_environmental_hygiene_to_housekeeping():
    decisions = route("the bathroom is dirty, please clean it", Intent.HYGIENE)
    assert Department.HOUSEKEEPING in depts(decisions)
    # Pure environmental with no clinical content → housekeeping is primary.
    assert primary(decisions).department == Department.HOUSEKEEPING


def test_change_bedsheets_to_housekeeping():
    decisions = route("please change the bedsheets", Intent.HYGIENE)
    assert Department.HOUSEKEEPING in depts(decisions)


def test_personal_hygiene_stays_nurse_even_if_env_words_present():
    # "clean" appears, but it's personal care → nurse, not housekeeping.
    decisions = route("I soiled myself, please clean me", Intent.HYGIENE)
    assert nurse_has_full_request(decisions)
    assert Department.HOUSEKEEPING not in depts(decisions)


# ── Facility requests (classify as Other/Information) → housekeeping ──────────

FACILITY_CASES = [
    ("the AC is not working", Intent.OTHER),
    ("the TV isn't working", Intent.OTHER),
    ("the light is out in my room", Intent.INFORMATION),
    ("the fan is broken", Intent.OTHER),
    ("there is a water leak", Intent.OTHER),
]


@pytest.mark.parametrize("transcript,intent", FACILITY_CASES)
def test_facility_requests_route_to_housekeeping(transcript, intent):
    decisions = route(transcript, intent)
    assert Department.HOUSEKEEPING in depts(decisions), (transcript, depts(decisions))


# ── Food/Water diet gate ──────────────────────────────────────────────────────

def test_food_with_no_diet_restriction_goes_to_canteen():
    decisions = route("I'm hungry, can I get some food", Intent.FOOD_WATER, diet_restriction="none")
    # Food routes to the canteen as the primary; nurse approval happens later in
    # the order lifecycle, not as a routing-time nurse slice.
    assert primary(decisions).department == Department.CANTEEN


@pytest.mark.parametrize("diet", ["npo", "diabetic", "dysphagia", "low_sodium", "peanut_allergy"])
def test_food_with_diet_restriction_still_goes_to_canteen(diet):
    # New design: food ALWAYS goes to the canteen as an order. The diet is a
    # menu-filter + nurse-approval input — it no longer diverts the whole request
    # to the nurse. Safety is enforced by the nurse approval step in the order
    # lifecycle (department_dispatch.create_food_order -> pending_approval).
    decisions = route("can I have something to eat", Intent.FOOD_WATER, diet_restriction=diet)
    assert Department.CANTEEN in depts(decisions), diet


# ── Multi-department fan-out ──────────────────────────────────────────────────

def test_mixed_clinical_and_facility_nurse_gets_full_plus_housekeeping():
    decisions = route("my chest hurts and the AC is broken", Intent.PAIN)
    # Clinical → nurse owns the full request.
    assert nurse_has_full_request(decisions)
    # Facility slice still pings housekeeping.
    assert Department.HOUSEKEEPING in depts(decisions)


def test_food_and_housekeeping_fanout_no_clinical():
    decisions = route("bring me water and please clean the bathroom", Intent.FOOD_WATER,
                      diet_restriction="none")
    ds = depts(decisions)
    assert Department.CANTEEN in ds
    assert Department.HOUSEKEEPING in ds


# ── General / undecipherable → nurse ──────────────────────────────────────────

def test_general_request_defaults_to_nurse():
    decisions = route("um, hello, are you there", Intent.OTHER)
    assert decisions[0].department == Department.NURSE
    assert len(decisions) == 1


def test_information_non_facility_goes_to_nurse():
    decisions = route("what time is my appointment", Intent.INFORMATION)
    assert decisions[0].department == Department.NURSE


# ── Audit metadata present on every decision ──────────────────────────────────

def test_every_decision_carries_audit_fields():
    decisions = route("the fan is broken", Intent.OTHER)
    for d in decisions:
        assert d.routing_reason in ("intent_map", "keyword", "model")
        assert d.model_version
        assert 0.0 <= d.confidence <= 1.0
