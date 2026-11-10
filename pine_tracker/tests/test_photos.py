"""Tests for repair jobs, photos with comments, and receipt images.

Uploads are the easiest thing to break silently: a file saves but the record
doesn't, or the record saves pointing at nothing. Each test here checks both
sides.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from pine_tracker.db import create_all, make_session_factory
from pine_tracker.models import ExpenseLine, Job, JobStatus, Photo, PhotoKind, Property, Receipt
from pine_tracker.storage import MAX_PIXELS, media_root, resolve
from pine_tracker.web import create_app


def a_photo(width: int = 3000, height: int = 2250) -> io.BytesIO:
    """A JPEG the size a phone actually produces."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 120, 140)).save(buffer, format="JPEG")
    buffer.seek(0)
    return buffer


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    application = create_app(f"sqlite:///{tmp_path/'photos.db'}")
    create_all(application.config["ENGINE"])
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def property_id(app, client):
    client.get("/")  # creates the organization
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        prop = Property(organization_id=1, street="1650 Frisbie Ct", short_name="Frisbie")
        db.add(prop)
        db.commit()
        return prop.id


def test_logging_a_repair_with_a_before_photo(client, app, property_id):
    response = client.post(
        "/jobs/new",
        data={
            "property_id": str(property_id),
            "title": "Kitchen faucet drips",
            "description": "",
            "priority": "3",
            "unit_id": "",
            "photo": (a_photo(), "before.jpg"),
            "caption": "Drip at the base",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        job = db.scalar(select(Job))
        photo = db.scalar(select(Photo))

    assert job.title == "Kitchen faucet drips"
    assert job.status == JobStatus.REPORTED
    assert photo.job_id == job.id
    assert photo.kind == PhotoKind.BEFORE_REPAIR
    assert photo.caption == "Drip at the base"
    # The file is really on disk, not just a row pointing nowhere.
    assert resolve(photo.image_key).is_file()


def test_big_phone_photos_are_downscaled(client, property_id, app):
    client.post(
        "/jobs/new",
        data={
            "property_id": str(property_id), "title": "Roof leak", "description": "",
            "priority": "3", "unit_id": "", "caption": "",
            "photo": (a_photo(4032, 3024), "huge.jpg"),
        },
        content_type="multipart/form-data",
    )
    from PIL import Image

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        photo = db.scalar(select(Photo))

    with Image.open(resolve(photo.image_key)) as stored:
        assert max(stored.size) == MAX_PIXELS


def test_after_photo_and_caption_edit(client, app, property_id):
    client.post(
        "/jobs/new",
        data={"property_id": str(property_id), "title": "Tile crack", "description": "",
              "priority": "3", "unit_id": "", "caption": ""},
        content_type="multipart/form-data",
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        job_id = db.scalar(select(Job)).id

    client.post(
        f"/jobs/{job_id}/photo",
        data={"photo": (a_photo(), "after.jpg"), "caption": "Regrouted",
              "kind": PhotoKind.AFTER_REPAIR},
        content_type="multipart/form-data",
    )
    with Session() as db:
        photo = db.scalar(select(Photo).where(Photo.kind == PhotoKind.AFTER_REPAIR))
    assert photo.caption == "Regrouted"

    client.post(f"/photo/{photo.id}/caption",
                data={"caption": "Regrouted and sealed", "kind": PhotoKind.AFTER_REPAIR})
    with Session() as db:
        assert db.get(Photo, photo.id).caption == "Regrouted and sealed"


def test_job_status_moves_and_records_completion(client, app, property_id):
    client.post(
        "/jobs/new",
        data={"property_id": str(property_id), "title": "Water heater", "description": "",
              "priority": "1", "unit_id": "", "caption": ""},
        content_type="multipart/form-data",
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        job_id = db.scalar(select(Job)).id

    client.post(f"/jobs/{job_id}/status", data={"status": JobStatus.DONE})
    with Session() as db:
        job = db.get(Job, job_id)
    assert job.status == JobStatus.DONE
    assert job.completed_on is not None

    # Reopening clears the completion date rather than leaving a stale one.
    client.post(f"/jobs/{job_id}/status", data={"status": JobStatus.IN_PROGRESS})
    with Session() as db:
        assert db.get(Job, job_id).completed_on is None


def test_open_and_done_lists_are_separate(client, app, property_id):
    for title, status in [("Open one", None), ("Closed one", JobStatus.DONE)]:
        client.post(
            "/jobs/new",
            data={"property_id": str(property_id), "title": title, "description": "",
                  "priority": "3", "unit_id": "", "caption": ""},
            content_type="multipart/form-data",
        )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        closed = db.scalar(select(Job).where(Job.title == "Closed one"))
    client.post(f"/jobs/{closed.id}/status", data={"status": JobStatus.DONE})

    open_page = client.get("/jobs").get_data(as_text=True)
    assert "Open one" in open_page and "Closed one" not in open_page

    done_page = client.get("/jobs?show=done").get_data(as_text=True)
    assert "Closed one" in done_page and "Open one" not in done_page


def test_expense_saved_with_a_receipt_photo(client, app, property_id):
    client.post(
        "/expense/new",
        data={
            "property_id": str(property_id), "amount": "48.62",
            "description": "Wax ring", "incurred_on": "2026-09-27",
            "unit_id": "", "category": "", "treatment": "",
            "vendor_id": "", "paid_by_member_id": "",
            "receipt": (a_photo(1200, 1600), "receipt.jpg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        line = db.scalar(select(ExpenseLine))
        receipt = db.scalar(select(Receipt))

    assert line.receipt_id == receipt.id
    assert resolve(receipt.image_key).is_file()


def test_receipt_can_be_added_to_an_existing_expense(client, app, property_id):
    client.post(
        "/expense/new",
        data={"property_id": str(property_id), "amount": "20.00",
              "description": "Caulk", "incurred_on": "2026-09-27",
              "unit_id": "", "category": "", "treatment": "",
              "vendor_id": "", "paid_by_member_id": ""},
        content_type="multipart/form-data",
    )
    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        line_id = db.scalar(select(ExpenseLine)).id

    client.post(
        f"/expense/{line_id}/receipt",
        data={"receipt": (a_photo(800, 1200), "late-receipt.jpg")},
        content_type="multipart/form-data",
    )
    with Session() as db:
        line = db.get(ExpenseLine, line_id)
        receipt = db.get(Receipt, line.receipt_id)
    assert receipt is not None and receipt.image_key


def test_video_and_other_junk_is_rejected(client, app, property_id):
    page = client.post(
        "/jobs/new",
        data={"property_id": str(property_id), "title": "Leak", "description": "",
              "priority": "3", "unit_id": "", "caption": "",
              "photo": (io.BytesIO(b"not really a movie"), "clip.mov")},
        content_type="multipart/form-data",
        follow_redirects=True,
    ).get_data(as_text=True)
    # Jinja escapes the apostrophe in "isn't", so match the unambiguous part.
    assert "supported" in page

    Session = make_session_factory(app.config["ENGINE"])
    with Session() as db:
        # The job still saved; only the bad file was refused.
        assert db.scalar(select(Job)) is not None
        assert db.scalar(select(Photo)) is None


def test_media_route_refuses_path_traversal(client, property_id):
    assert client.get("/media/../../etc/passwd").status_code == 404
    assert client.get("/media/jobs/2026/01/nothing-here.jpg").status_code == 404


def test_media_requires_the_password_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    application = create_app(f"sqlite:///{tmp_path/'gated.db'}")
    create_all(application.config["ENGINE"])
    gated = application.test_client()
    # Photos of someone's property shouldn't be reachable without signing in.
    assert gated.get("/media/jobs/2026/01/whatever.jpg").status_code == 302
