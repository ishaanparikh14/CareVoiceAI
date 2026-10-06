# -*- coding: utf-8 -*-
"""
test_scheduling.py — unit tests for the pure nurse scheduler.

Run:
    python -m pytest server/sim/test_scheduling.py -q
  or (no pytest needed):
    python server/sim/test_scheduling.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from scheduling import NurseState, choose_nurse, choose_nurse_grouped  # noqa: E402


def _st(name, online=True, busy=False, active=0, status="available", is_supervisor=False):
    return NurseState(name, online=online, busy=busy, active_alerts=active,
                      status=status, is_supervisor=is_supervisor)


def test_primary_available_is_chosen():
    states = {"a": _st("a"), "b": _st("b")}
    d = choose_nurse(["a", "b"], states)
    assert d.chosen == "a" and d.fell_back is False


def test_failover_when_primary_busy():
    states = {"a": _st("a", busy=True), "b": _st("b")}
    d = choose_nurse(["a", "b"], states)
    assert d.chosen == "b" and d.fell_back is False


def test_failover_skips_offline():
    states = {"a": _st("a", online=False), "b": _st("b")}
    d = choose_nurse(["a", "b"], states)
    assert d.chosen == "b"


def test_all_busy_falls_back():
    states = {"a": _st("a", busy=True), "b": _st("b", busy=True)}
    d = choose_nurse(["a", "b"], states)
    assert d.chosen is None and d.fell_back is True


def test_no_assignment_falls_back():
    d = choose_nurse([], {})
    assert d.chosen is None and d.fell_back is True


def test_unknown_nurse_is_skipped():
    # 'a' has no state snapshot (never route to a nurse we can't see), 'b' free.
    states = {"b": _st("b")}
    d = choose_nurse(["a", "b"], states)
    assert d.chosen == "b"


def test_never_chooses_unassigned_nurse():
    # 'c' is free but not assigned to this patient -> must not be chosen.
    states = {"a": _st("a", busy=True), "b": _st("b", busy=True), "c": _st("c")}
    d = choose_nurse(["a", "b"], states)
    assert d.chosen is None and d.fell_back is True
    assert "c" not in d.considered


def test_load_balanced_tiebreak_in_group():
    # Equal-priority tier: pick the one with fewer active alerts.
    states = {"a": _st("a", active=3), "b": _st("b", active=1)}
    d = choose_nurse_grouped([["a", "b"]], states)
    assert d.chosen == "b"


def test_reroute_excludes_previous_nurse():
    # Auto-reroute models the no-ack nurse by removing them from the assigned
    # list before calling the scheduler. Here 'a' got the alert but didn't ack;
    # even though 'a' is now "available", excluding them routes to 'b'.
    assigned = ["a", "b"]
    prev = "a"
    filtered = [n for n in assigned if n != prev]
    states = {"a": _st("a"), "b": _st("b")}
    d = choose_nurse(filtered, states)
    assert d.chosen == "b" and d.fell_back is False


def test_reroute_all_others_busy_falls_back():
    # Previous nurse excluded, everyone else busy -> broadcast fallback.
    assigned = ["a", "b"]
    filtered = [n for n in assigned if n != "a"]
    states = {"a": _st("a"), "b": _st("b", busy=True)}
    d = choose_nurse(filtered, states)
    assert d.chosen is None and d.fell_back is True


# ── Competency, status, supervisor, escalation ──────────────────────────────

from scheduling import choose_supervisor, STATUS_ON_BREAK, STATUS_OFF_DUTY  # noqa: E402


def _skilled(name, comps, **kw):
    return NurseState(name, competencies=frozenset(comps), **kw)


def test_requires_competency_skips_unqualified():
    # 'a' lacks the competency; 'b' has it -> b chosen.
    states = {"a": _skilled("a", []), "b": _skilled("b", ["medication_qualified"])}
    d = choose_nurse(["a", "b"], states, required_competency="medication_qualified")
    assert d.chosen == "b"


def test_no_one_has_competency_falls_back():
    states = {"a": _skilled("a", ["iv_start"]), "b": _skilled("b", [])}
    d = choose_nurse(["a", "b"], states, required_competency="icu_certified")
    assert d.chosen is None and d.fell_back is True


def test_on_break_skipped_for_time_critical():
    states = {
        "a": _st("a", status=STATUS_ON_BREAK),
        "b": _st("b"),
    }
    # time-critical: a (on break) skipped -> b
    d = choose_nurse(["a", "b"], states, time_critical=True)
    assert d.chosen == "b"
    # routine: on-break is allowed -> a (first in order)
    d2 = choose_nurse(["a", "b"], states, time_critical=False)
    assert d2.chosen == "a"


def test_off_duty_always_skipped():
    states = {"a": _st("a", status=STATUS_OFF_DUTY), "b": _st("b")}
    assert choose_nurse(["a", "b"], states, time_critical=False).chosen == "b"
    assert choose_nurse(["a", "b"], states, time_critical=True).chosen == "b"


def test_escalation_primary_secondary_supervisor():
    # primary busy, secondary busy -> supervisor picked from the supervisor pool.
    states = {
        "a": _st("a", busy=True),
        "b": _st("b", busy=True),
        "sup": _st("sup", is_supervisor=True),
    }
    # assigned exhausted
    d = choose_nurse(["a", "b"], states)
    assert d.chosen is None and d.fell_back is True
    # supervisor step
    s = choose_supervisor(["sup"], states)
    assert s.chosen == "sup" and s.fell_back is False


def test_supervisor_competency_relaxed():
    # Supervisor lacks the exact competency but is still preferred over broadcast.
    states = {"sup": _skilled("sup", [], is_supervisor=True)}
    s = choose_supervisor(["sup"], states, required_competency="icu_certified")
    assert s.chosen == "sup"


def test_walk_down_priority_with_exclude():
    # Simulates reroute: primary 'a' already tried -> next assignable is 'b'.
    states = {"a": _st("a"), "b": _st("b"), "c": _st("c")}
    d = choose_nurse(["a", "b", "c"], states, exclude={"a"})
    assert d.chosen == "b"
    d2 = choose_nurse(["a", "b", "c"], states, exclude={"a", "b"})
    assert d2.chosen == "c"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
