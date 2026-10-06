"""
scheduling.py — nurse selection logic for alert routing (failover scheduler).

PURE module: no database, WebSocket, or network dependencies, so it is fully
deterministic and unit-testable. The live alert path (routing.py) gathers the
current state and hands it to `choose_nurse` to pick the right nurse.

Routing rules
-------------
A request carries a priority (Critical/Urgent/Routine) and, from the patient,
an acuity (ESI 1–5, 1 = most acute). The caller derives:
  • required_competency — a skill the nurse MUST have for this request
    (e.g. "medication_qualified" for a medication request; "icu_certified" for
    a Critical / high-acuity request).
  • time_critical — Critical priority or high acuity (ESI 1–2).

Selection, over the patient's assigned nurses in priority order:
  1. Skip a nurse who lacks the required competency.
  2. Skip a nurse who is OFF DUTY always, and ON BREAK when the request is
     time-critical (Dr. Sandhya: check live status before time-critical work).
  3. Skip a nurse who is offline or busy (on a call / holding an unacked alert /
     manually marked busy).
  4. The FIRST assigned nurse who passes is the primary target.

Escalation (handled by the caller via `exclude` + supervisor list):
  primary → paired secondary (next assigned) → department supervisor → broadcast.
  `choose_nurse` is called with the already-tried nurses excluded; when no
  assigned nurse qualifies, the caller tries `choose_supervisor`, then broadcast
  so a request is never silently dropped.

Statuses
--------
  available        — on the floor, assignable
  in_patient_room  — with a patient but reachable; still assignable
  on_break         — not assignable for time-critical requests
  off_duty         — never assignable
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# Live nurse status values.
STATUS_AVAILABLE = "available"
STATUS_IN_ROOM = "in_patient_room"
STATUS_ON_BREAK = "on_break"
STATUS_OFF_DUTY = "off_duty"

# Statuses that can take a time-critical request (on-floor presence).
_TIME_CRITICAL_OK = {STATUS_AVAILABLE, STATUS_IN_ROOM}
# Statuses that can take a routine request.
_ROUTINE_OK = {STATUS_AVAILABLE, STATUS_IN_ROOM, STATUS_ON_BREAK}


@dataclass(frozen=True)
class NurseState:
    """Snapshot of one nurse's availability at routing time."""
    username: str
    online: bool = True                       # has a live nurse WebSocket
    busy: bool = False                         # on a call / holding alert / manual
    active_alerts: int = 0                     # unacked alerts already routed
    status: str = STATUS_AVAILABLE             # live location/status
    competencies: frozenset[str] = frozenset() # skills (icu_certified, etc.)
    is_supervisor: bool = False                # department supervisor

    @property
    def available(self) -> bool:
        """Base availability: online and not busy (ignores status/competency)."""
        return self.online and not self.busy

    def status_ok(self, time_critical: bool) -> bool:
        allowed = _TIME_CRITICAL_OK if time_critical else _ROUTINE_OK
        return self.status in allowed

    def has_competency(self, required: str | None) -> bool:
        return required is None or required in self.competencies

    def assignable(self, required_competency: str | None, time_critical: bool) -> bool:
        return (
            self.available
            and self.status_ok(time_critical)
            and self.has_competency(required_competency)
        )


@dataclass
class RoutingDecision:
    """Result of a routing attempt — carries the 'why' for logging/UI."""
    chosen: str | None               # username to deliver to, or None
    fell_back: bool                  # True => caller should broadcast to all
    reason: str                      # human-readable explanation
    considered: list[str] = field(default_factory=list)   # candidates examined


def _skip_reason(st: NurseState | None, required_competency: str | None,
                 time_critical: bool) -> str | None:
    """Return why a nurse is NOT assignable, or None if they are."""
    if st is None:
        return "no live snapshot"
    if not st.online:
        return "offline"
    if st.busy:
        return "busy"
    if not st.status_ok(time_critical):
        return f"status {st.status}" + (" (time-critical)" if time_critical else "")
    if not st.has_competency(required_competency):
        return f"lacks {required_competency}"
    return None


def choose_nurse(
    assigned_in_order: Sequence[str],
    states: dict[str, NurseState],
    *,
    required_competency: str | None = None,
    time_critical: bool = False,
    exclude: Iterable[str] = (),
) -> RoutingDecision:
    """
    Pick the next assignable nurse for a request.

    assigned_in_order : the patient's assigned nurse usernames, primary first.
    states            : username -> NurseState (missing = treated unavailable).
    required_competency : a skill the nurse must have, or None.
    time_critical     : Critical priority / high acuity — stricter status rules.
    exclude           : nurses already tried (skipped), for escalation.
    """
    considered: list[str] = []
    excluded = set(exclude)

    if not assigned_in_order:
        return RoutingDecision(
            chosen=None, fell_back=True,
            reason="no nurses assigned to this patient",
            considered=considered,
        )

    for username in assigned_in_order:
        if username in excluded:
            continue
        considered.append(username)
        st = states.get(username)
        if st is not None and st.assignable(required_competency, time_critical):
            return RoutingDecision(
                chosen=username, fell_back=False,
                reason=f"routed to {username} (first assignable in priority order)",
                considered=considered,
            )

    return RoutingDecision(
        chosen=None, fell_back=True,
        reason="no assigned nurse assignable",
        considered=considered,
    )


def choose_supervisor(
    supervisors_in_order: Sequence[str],
    states: dict[str, NurseState],
    *,
    required_competency: str | None = None,
    time_critical: bool = False,
    exclude: Iterable[str] = (),
) -> RoutingDecision:
    """
    Pick an available department supervisor (escalation step before broadcast).
    Supervisors still must meet status; competency is PREFERRED but a supervisor
    without the exact competency is better than a broadcast, so if no
    competency-matched supervisor is free we relax the competency requirement.
    """
    excluded = set(exclude)
    ordered = [u for u in supervisors_in_order if u not in excluded]

    # First pass: competency-matched supervisors.
    for required in (required_competency, None):
        considered: list[str] = []
        for username in ordered:
            considered.append(username)
            st = states.get(username)
            if st is not None and st.assignable(required, time_critical):
                note = "" if required else " (competency relaxed)"
                return RoutingDecision(
                    chosen=username, fell_back=False,
                    reason=f"escalated to supervisor {username}{note}",
                    considered=considered,
                )
        if required_competency is None:
            break  # no need for a second identical pass

    return RoutingDecision(
        chosen=None, fell_back=True,
        reason="no supervisor available",
        considered=list(ordered),
    )


def choose_nurse_grouped(
    priority_groups: Sequence[Iterable[str]],
    states: dict[str, NurseState],
    *,
    required_competency: str | None = None,
    time_critical: bool = False,
) -> RoutingDecision:
    """
    Variant where nurses share priority *tiers*; within a tier, load-balance by
    fewest active alerts, then name. Each candidate must still be assignable
    (competency + status + availability).
    """
    considered: list[str] = []
    for group in priority_groups:
        ranked = sorted(
            (states.get(u, NurseState(u, online=False)) for u in group),
            key=lambda s: (s.active_alerts, s.username),
        )
        for st in ranked:
            considered.append(st.username)
            if st.assignable(required_competency, time_critical):
                return RoutingDecision(
                    chosen=st.username, fell_back=False,
                    reason=f"routed to {st.username} (least-loaded in tier)",
                    considered=considered,
                )

    return RoutingDecision(
        chosen=None, fell_back=True,
        reason="no assignable nurse in any tier",
        considered=considered,
    )
