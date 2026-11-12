"""Turnover helpers: what to look for, and how far along the work is.

Two jobs here:

1. **Finding problems.** Walking an empty unit, it's easy to miss the things
   that aren't visually obvious — the smoke detector battery, the rekey, the
   drip pan under the water heater. STANDARD_WALKTHROUGH is a starting list
   drawn from what actually comes up on a turn, so the walkthrough is a
   matter of deleting what doesn't apply rather than remembering everything.

2. **Prioritizing.** During a turn the only question that matters is "what's
   stopping me listing this unit". That's what the BLOCKER priority is for,
   and why sorting is by priority first and area second rather than by date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import TaskPriority, TaskStatus

# area, item, priority, default quantity unit
STANDARD_WALKTHROUGH: tuple[tuple[str, str, int, str], ...] = (
    # Safety and legal first: these genuinely block renting the unit.
    ("Safety", "Smoke detectors tested, batteries replaced", TaskPriority.BLOCKER, "each"),
    ("Safety", "Carbon monoxide detector tested", TaskPriority.BLOCKER, "each"),
    ("Safety", "Locks rekeyed, all keys accounted for", TaskPriority.BLOCKER, "each"),
    ("Safety", "Water heater strapped, drip pan clear", TaskPriority.IMPORTANT, "each"),
    # The walk-through itself, room by room.
    ("Kitchen", "Appliances cleaned and working", TaskPriority.IMPORTANT, "each"),
    ("Kitchen", "Under-sink leak check", TaskPriority.IMPORTANT, "each"),
    ("Kitchen", "Cabinets and drawers clean, hardware tight", TaskPriority.NORMAL, "each"),
    ("Bathroom", "Toilet seal and shut-off valve checked", TaskPriority.IMPORTANT, "each"),
    ("Bathroom", "Caulk at tub and sink", TaskPriority.NORMAL, "each"),
    ("Bathroom", "Exhaust fan clean and working", TaskPriority.NORMAL, "each"),
    ("Whole unit", "Walls patched and touched up", TaskPriority.IMPORTANT, "room"),
    ("Whole unit", "Floors cleaned, carpet shampooed or replaced", TaskPriority.IMPORTANT, "sq yd"),
    ("Whole unit", "Windows, screens, and blinds intact", TaskPriority.NORMAL, "each"),
    ("Whole unit", "All light fixtures working, bulbs replaced", TaskPriority.NORMAL, "each"),
    ("Whole unit", "HVAC filter replaced", TaskPriority.NORMAL, "each"),
    ("Whole unit", "Deep clean before showings", TaskPriority.IMPORTANT, "each"),
    ("Exterior", "Yard, gutters, and entry tidy", TaskPriority.NORMAL, "each"),
)


@dataclass(frozen=True)
class Progress:
    """How far along a turnover is, and what's in the way."""

    total: int
    done: int
    blocked: int
    unassigned: int
    blockers_open: int
    estimate_cents: int
    chargeable_cents: int

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        return round(self.done * 100 / self.total)

    @property
    def remaining(self) -> int:
        return self.total - self.done

    @property
    def is_ready(self) -> bool:
        """Every item closed. Not the same as "no blockers left"."""
        return self.total > 0 and self.done == self.total


def progress_for(items: Iterable) -> Progress:
    rows = list(items)
    done = [i for i in rows if i.status == TaskStatus.DONE]
    return Progress(
        total=len(rows),
        done=len(done),
        blocked=sum(1 for i in rows if i.status == TaskStatus.BLOCKED),
        unassigned=sum(
            1
            for i in rows
            if i.status != TaskStatus.DONE and not (i.assigned_to or i.vendor_id)
        ),
        blockers_open=sum(
            1
            for i in rows
            if i.priority == TaskPriority.BLOCKER and i.status != TaskStatus.DONE
        ),
        estimate_cents=sum(i.estimate_cents or 0 for i in rows),
        chargeable_cents=sum(
            i.estimate_cents or 0 for i in rows if i.tenant_chargeable
        ),
    )


def sort_key(item):
    """Open blockers at the top, finished work at the bottom."""
    done_last = 1 if item.status == TaskStatus.DONE else 0
    return (done_last, item.priority, item.area.lower(), item.id or 0)


def by_assignee(items: Iterable) -> dict[str, list]:
    """Group open work by who owns it, so you can text one person one list."""
    groups: dict[str, list] = {}
    for item in items:
        if item.status == TaskStatus.DONE:
            continue
        who = (item.assigned_to or "").strip() or "Unassigned"
        groups.setdefault(who, []).append(item)
    # Unassigned last: it's a prompt, not a person.
    return dict(
        sorted(groups.items(), key=lambda kv: (kv[0] == "Unassigned", kv[0].lower()))
    )
