"""
scheduling.py — nurse selection logic for alert routing (failover scheduler).

This module is PURE: it has no database, WebSocket, or network dependencies so
it is fully deterministic and unit-testable. The live alert path (routers that
create alerts) gathers the current state — which nurses are assigned to the
patient's room, which are busy, and how many active alerts each is holding —
and hands it to `choose_nurse` to pick the next available nurse.

Scheduling rules (as agreed)
----------------------------
1. Many-to-many: a patient (room) has an ORDERED list of assigned nurses. The
   order is the assignment priority (index 0 = primary / attending).
2. Busy nurse: a nurse is skipped if they are in `busy`. "Busy" is defined by
   the caller (on an active call, holding an unacked alert, or a manual toggle);
   this module does not care HOW busy was decided.
3. Selection: walk the assigned nurses in priority order and pick the FIRST that
   is both online and not busy. When several candidates sit at the same priority
   position (ties are possible once the caller groups equals), break the tie by
   fewest active alerts (load balancing), then by name for determinism.
4. Fallback: if NO assigned nurse is available (all busy/offline), return
   (None, fell_back=True) so the caller broadcasts to everyone — an alert is
   never silently dropped.

The common case has a strict priority list, so rule 3's tiebreak only matters
when the caller passes equal-priority groups (see `choose_nurse_grouped`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class NurseState:
    """Snapshot of one nurse's availability at routing time."""
    username: str
    online: bool = True          # has a live nurse WebSocket connection
    busy: bool = False           # on a call / holding an unacked alert / manual
    active_alerts: int = 0       # unacknowledged alerts already routed to them

    @property
    def available(self) -> bool:
        return self.online and not self.busy


@dataclass
class RoutingDecision:
    """Result of a routing attempt — carries the 'why' for logging/UI."""
    chosen: str | None               # username to deliver to, or None
    fell_back: bool                  # True => caller should broadcast to all
    reason: str                      # human-readable explanation
    considered: list[str] = field(default_factory=list)   # candidates examined


def choose_nurse(
    assigned_in_order: Sequence[str],
    states: dict[str, NurseState],
) -> RoutingDecision:
    """
    Pick the next available nurse for a patient.

    Parameters
    ----------
    assigned_in_order
        The patient's assigned nurse usernames, highest priority first.
    states
        Map of username -> NurseState for (at least) every assigned nurse.
        A username missing from `states` is treated as offline+unknown and
        skipped (defensive: we never route to a nurse we have no snapshot for).

    Returns
    -------
    RoutingDecision
        chosen is the username to deliver to, or None. When None, fell_back is
        True and the caller must broadcast to all nurses.
    """
    considered: list[str] = []

    if not assigned_in_order:
        return RoutingDecision(
            chosen=None, fell_back=True,
            reason="no nurses assigned to this patient — broadcasting to all",
            considered=considered,
        )

    for username in assigned_in_order:
        considered.append(username)
        st = states.get(username)
        if st is None:
            continue                      # no snapshot => treat as unavailable
        if st.available:
            return RoutingDecision(
                chosen=username, fell_back=False,
                reason=f"routed to {username} (first available in priority order)",
                considered=considered,
            )

    return RoutingDecision(
        chosen=None, fell_back=True,
        reason="all assigned nurses busy/offline — broadcasting to all",
        considered=considered,
    )


def choose_nurse_grouped(
    priority_groups: Sequence[Iterable[str]],
    states: dict[str, NurseState],
) -> RoutingDecision:
    """
    Variant where nurses share priority *tiers*. `priority_groups` is an ordered
    sequence of groups; nurses within a group are equal priority and the tie is
    broken by fewest active alerts, then alphabetically.

    Use this when two nurses are assigned at the same level and you want load
    balancing between them. `choose_nurse` (strict order) is the common path.
    """
    considered: list[str] = []

    for group in priority_groups:
        # Rank available members of this tier: fewest active alerts, then name.
        ranked = sorted(
            (states.get(u, NurseState(u, online=False)) for u in group),
            key=lambda s: (s.active_alerts, s.username),
        )
        for st in ranked:
            considered.append(st.username)
            if st.available:
                return RoutingDecision(
                    chosen=st.username, fell_back=False,
                    reason=(
                        f"routed to {st.username} "
                        f"(available, fewest active alerts in its priority tier)"
                    ),
                    considered=considered,
                )

    return RoutingDecision(
        chosen=None, fell_back=True,
        reason="all assigned nurses busy/offline — broadcasting to all",
        considered=considered,
    )
