"""Turnover tests.

The three things this feature has to get right, in order of how much damage
getting them wrong would do:

1. Blockers surface. If "rekey the locks" is buried below "touch up paint",
   the unit gets listed before it's safe to hand over keys.
2. Nothing falls between people. Unassigned work is counted and named.
3. Progress is honest. "80% done" with an open blocker is not ready to rent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import pytest
from sqlalchemy import select

from pine_tracker import turnover as turnlib
from pine_tracker.db import create_all, make_session_factory
from pine_tracker.models import (
    Property,
    TaskPriority,
    TaskStatus,
    Turnover,
    TurnoverItem,
    TurnoverStatus,
    Unit,
)
from pine_tracker.web import create_app


@dataclass
class FakeItem:
    item: str = "Thing"
    area: str = "Kitchen"
    priority: int = TaskPriority.NORMAL
    status: str = TaskStatus.TODO
    assigned_to: Optional[str] = None
    vendor_id: Optional[int] = None
    estimate_cents: int = 0
    tenant_chargeable: bool = False
    id: int = 0


# -- the maths, no database -----------------------------------------------


def test_progress_counts_what_matters():
    items = [
        FakeItem(status=TaskStatus.DONE, estimate_cents=10000),
        FakeItem(status=TaskStatus.DOING, assigned_to="Camilo", estimate_cents=5000),
        FakeItem(status=TaskStatus.BLOCKED, estimate_cents=2000),
        FakeItem(priority=TaskPriority.BLOCKER, estimate_cents=3000),
    ]
    progress = turnlib.progress_for(items)
    assert progress.total == 4
    assert progress.done == 1
    assert progress.percent == 25
    assert progress.blocked == 1
    assert progress.blockers_open == 1
    assert progress.unassigned == 2  # the blocked one and the blocker
    assert progress.estimate_cents == 20000


def test_a_turnover_with_no_items_is_not_ready():
    assert turnlib.progress_for([]).is_ready is False
    assert turnlib.progress_for([]).percent == 0


def test_ready_only_when_everything_is_done():
    nearly = [FakeItem(status=TaskStatus.DONE), FakeItem(status=TaskStatus.DOING)]
    assert turnlib.progress_for(nearly).is_ready is False

    finished = [FakeItem(status=TaskStatus.DONE), FakeItem(status=TaskStatus.DONE)]
    assert turnlib.progress_for(finished).is_ready is True


def test_blockers_sort_above_everything_else():
    items = [
        FakeItem(item="Touch up paint", priority=TaskPriority.NORMAL, id=1),
        FakeItem(item="Rekey locks", priority=TaskPriority.BLOCKER, id=2),
        FakeItem(item="Smoke detector", priority=TaskPriority.BLOCKER,
                 status=TaskStatus.DONE, id=3),
        FakeItem(item="Clean gutters", priority=TaskPriority.OPTIONAL, id=4),
    ]
    order = [i.item for i in sorted(items, key=turnlib.sort_key)]
    assert order[0] == "Rekey locks"
    # Finished work drops to the bottom even when it was a blocker.
    assert order[-1] == "Smoke detector"


def test_chargeable_estimate_is_tracked_separately():
    items = [
        FakeItem(estimate_cents=30000, tenant_chargeable=True),
        FakeItem(estimate_cents=15000, tenant_chargeable=False),
    ]
    progress = turnlib.progress_for(items)
    assert progress.estimate_cents == 45000
    assert progress.chargeable_cents == 30000


def test_grouping_by_person_puts_unassigned_last():
    items = [
        FakeItem(item="Paint", assigned_to="Camilo"),
        FakeItem(item="Carpet"),
        FakeItem(item="Rekey", assigned_to="Wei"),
        FakeItem(item="Done thing", status=TaskStatus.DONE, assigned_to="Wei"),
    ]
    groups = turnlib.by_assignee(items)
    assert list(groups) == ["Camilo", "Wei", "Unassigned"]
    # Finished work isn't anyone's open list.
    assert [i.item for i in groups["Wei"]] == ["Rekey"]


def test_the_standard_list_leads_with_safety():
    first_areas = [row[0] for row in turnlib.STANDARD_WALKTHROUGH[:3]]
    assert first_areas == ["Safety", "Safety", "Safety"]
    assert all(row[2] == TaskPriority.BLOCKER for row in turnlib.STANDARD_WALKTHROUGH[:3])


# -- the screens ----------------------------------------------------------


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    application = create_app(f"sqlite:///{tmp_path/'turn.db'}")
    create_all(application.config["ENGINE"])
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def unit_ids(app, client):
    client.get("/")
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = Property(organization_id=1, street="1650 Frisbie Ct")
        db.add(prop)
        db.flush()
        unit = Unit(organization_id=1, property_id=prop.id, label="Unit 2")
        db.add(unit)
        db.commit()
        return prop.id, unit.id


def test_starting_a_turnover_seeds_the_walkthrough(client, app, unit_ids):
    property_id, unit_id = unit_ids
    client.post(
        f"/property/{property_id}/turnover/new",
        data={"unit_id": str(unit_id), "moved_out_on": "2026-09-30", "seed": "1"},
        follow_redirects=True,
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        turn = db.scalar(select(Turnover))
        items = db.scalars(select(TurnoverItem)).all()

    assert turn.status == TurnoverStatus.WALKTHROUGH
    assert len(items) == len(turnlib.STANDARD_WALKTHROUGH)
    assert any(i.priority == TaskPriority.BLOCKER for i in items)


def test_starting_without_the_seed_leaves_an_empty_list(client, app, unit_ids):
    property_id, unit_id = unit_ids
    client.post(
        f"/property/{property_id}/turnover/new",
        data={"unit_id": str(unit_id), "moved_out_on": "2026-09-30"},
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        assert db.scalars(select(TurnoverItem)).all() == []


@pytest.fixture()
def turnover_id(client, app, unit_ids):
    property_id, unit_id = unit_ids
    client.post(
        f"/property/{property_id}/turnover/new",
        data={"unit_id": str(unit_id), "moved_out_on": "2026-09-30"},
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        return db.scalar(select(Turnover)).id


def test_adding_an_item_with_quantity_and_assignee(client, app, turnover_id):
    client.post(
        f"/turnover/{turnover_id}/item",
        data={
            "item": "Replace carpet", "area": "Bedroom 1",
            "quantity": "14", "unit_label": "sq yd",
            "priority": str(TaskPriority.IMPORTANT),
            "assigned_to": "Camilo", "estimate": "620",
            "tenant_chargeable": "1",
        },
        follow_redirects=True,
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        item = db.scalar(select(TurnoverItem))

    assert item.quantity == "14"
    assert item.unit_label == "sq yd"
    assert item.estimate_cents == 62000
    assert item.tenant_chargeable is True
    # Assigning it moves it off the to-do pile automatically.
    assert item.status == TaskStatus.ASSIGNED


def test_assigning_later_also_advances_the_status(client, app, turnover_id):
    client.post(f"/turnover/{turnover_id}/item", data={"item": "Patch wall"})
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        item_id = db.scalar(select(TurnoverItem)).id

    client.post(f"/turnover/item/{item_id}", data={"assigned_to": "Wei"})
    with Session() as db:
        item = db.get(TurnoverItem, item_id)
    assert item.assigned_to == "Wei"
    assert item.status == TaskStatus.ASSIGNED


def test_marking_done_stamps_the_date_and_undoing_clears_it(client, app, turnover_id):
    client.post(f"/turnover/{turnover_id}/item", data={"item": "Rekey locks"})
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        item_id = db.scalar(select(TurnoverItem)).id

    client.post(f"/turnover/item/{item_id}", data={"status": TaskStatus.DONE})
    with Session() as db:
        assert db.get(TurnoverItem, item_id).done_on == dt.date.today()

    client.post(f"/turnover/item/{item_id}", data={"status": TaskStatus.DOING})
    with Session() as db:
        assert db.get(TurnoverItem, item_id).done_on is None


def test_an_item_can_be_removed(client, app, turnover_id):
    client.post(f"/turnover/{turnover_id}/item", data={"item": "Not actually broken"})
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        item_id = db.scalar(select(TurnoverItem)).id

    client.post(f"/turnover/item/{item_id}", data={"delete": "1"}, follow_redirects=True)
    with Session() as db:
        assert db.scalars(select(TurnoverItem)).all() == []


def test_the_table_shows_quantity_person_and_progress(client, turnover_id):
    client.post(
        f"/turnover/{turnover_id}/item",
        data={"item": "Replace carpet", "area": "Bedroom 1", "quantity": "14",
              "unit_label": "sq yd", "assigned_to": "Camilo", "estimate": "620",
              "priority": str(TaskPriority.BLOCKER)},
    )
    page = client.get(f"/turnover/{turnover_id}").get_data(as_text=True)
    assert "Replace carpet" in page
    assert "14" in page and "sq yd" in page
    assert "Camilo" in page
    assert "$620.00" in page
    assert "Blocker" in page
    assert "Blocking the listing" in page


def test_marking_the_turnover_ready(client, app, turnover_id):
    client.post(f"/turnover/{turnover_id}/status", data={"status": TurnoverStatus.READY})
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        turn = db.get(Turnover, turnover_id)
    assert turn.status == TurnoverStatus.READY
    assert turn.ready_on == dt.date.today()


def test_turnover_is_reachable_from_the_property(client, unit_ids):
    property_id, _ = unit_ids
    page = client.get(f"/property/{property_id}/turnover").get_data(as_text=True)
    assert 'aria-current="page"' in page
    assert "Start a turnover" in page


def test_another_orgs_turnover_is_404(client):
    assert client.get("/turnover/9999").status_code == 404
    assert client.post("/turnover/9999/item", data={"item": "x"}).status_code == 404
