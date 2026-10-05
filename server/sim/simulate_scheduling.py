# -*- coding: utf-8 -*-
"""
simulate_scheduling.py — demonstrate the nurse scheduling / failover routing.

Runs the REAL scheduler (server/scheduling.py), not a reimplementation, against
an in-memory scenario. No database or network needed.

Scenario
--------
  Room 4B (patient A, Rajesh): nurses [nurse_anna (primary), nurse_ben]
  Room 4C (patient B, Sara):   nurses [nurse_anna (primary), nurse_carol]

So nurse_anna covers BOTH patients. We then make nurse_anna BUSY (on a call with
patient A in 4B) and have patient B (4C) raise an alert. The scheduler must skip
the busy nurse_anna and route 4C's alert to nurse_carol. Finally we make ALL of
4C's nurses busy and show the broadcast fallback.

Run:
    python server/sim/simulate_scheduling.py
"""

import os
import sys

# Make the server package importable whether run from repo root or server/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from scheduling import NurseState, choose_nurse  # noqa: E402


# ── Assignment (priority order; index 0 = primary) ────────────────────────────
ASSIGNMENTS = {
    "4B": ["nurse_anna", "nurse_ben"],      # patient A
    "4C": ["nurse_anna", "nurse_carol"],    # patient B
}


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_states(states: dict[str, NurseState]) -> None:
    for n, s in states.items():
        flags = []
        if not s.online:
            flags.append("OFFLINE")
        if s.busy:
            flags.append("BUSY")
        if not flags:
            flags.append("available")
        print(f"    - {n:12s} {'/'.join(flags):18s} active_alerts={s.active_alerts}")


def route(room: str, states: dict[str, NurseState]) -> None:
    assigned = ASSIGNMENTS[room]
    print(f"\n  Patient in Room {room} raises an alert.")
    print(f"  Assigned nurses (priority order): {assigned}")
    print("  Nurse states:")
    show_states({n: states[n] for n in assigned})
    decision = choose_nurse(assigned, states)
    print(f"  Considered: {decision.considered}")
    if decision.fell_back:
        print(f"  >>> RESULT: no assigned nurse free — BROADCAST to all nurses.")
    else:
        print(f"  >>> RESULT: alert delivered to **{decision.chosen}**.")
    print(f"      ({decision.reason})")


def main() -> int:
    banner("CareVoice — nurse scheduling / failover simulation")
    print("Assignments:")
    for room, nurses in ASSIGNMENTS.items():
        print(f"    Room {room}: {nurses}")

    # ── Scenario 1: nurse_anna busy with 4B; patient B (4C) alerts ────────────
    banner("Scenario 1 — primary nurse busy, failover to backup")
    states = {
        "nurse_anna":  NurseState("nurse_anna",  online=True, busy=True,  active_alerts=1),  # on call w/ 4B
        "nurse_ben":   NurseState("nurse_ben",   online=True, busy=False, active_alerts=0),
        "nurse_carol": NurseState("nurse_carol", online=True, busy=False, active_alerts=0),
    }
    print("\n  nurse_anna is BUSY (on a call with patient A in Room 4B).")
    route("4C", states)
    assert choose_nurse(ASSIGNMENTS["4C"], states).chosen == "nurse_carol", "expected failover to carol"

    # ── Scenario 2: a 4B alert while anna busy -> ben (anna's backup on 4B) ────
    banner("Scenario 2 — same busy nurse, different patient, different backup")
    print("\n  Still: nurse_anna BUSY. Now patient A in Room 4B alerts.")
    route("4B", states)
    assert choose_nurse(ASSIGNMENTS["4B"], states).chosen == "nurse_ben", "expected failover to ben"

    # ── Scenario 3: all of 4C's nurses busy -> broadcast fallback ─────────────
    banner("Scenario 3 — all assigned nurses busy, broadcast fallback")
    states_all_busy = {
        "nurse_anna":  NurseState("nurse_anna",  online=True, busy=True, active_alerts=1),
        "nurse_carol": NurseState("nurse_carol", online=True, busy=True, active_alerts=2),
    }
    print("\n  Both of Room 4C's nurses (anna, carol) are BUSY.")
    route("4C", states_all_busy)
    assert choose_nurse(ASSIGNMENTS["4C"], states_all_busy).fell_back is True, "expected fallback"

    banner("All scenarios behaved as expected ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
