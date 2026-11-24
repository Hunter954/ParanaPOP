import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image

from app.ad_popup import BRASILIA, SETTING_KEY
from app.models import SiteSetting, User, db


@pytest.fixture
def popup_client(app, monkeypatch):
    email = "popup-test@example.org"
    password = "popup-testing-password"
    monkeypatch.setenv("ADMIN_EMAIL", email)
    with app.app_context():
        user = User(email=email, is_active=True, is_admin=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    response = client.post("/admin/login", data={"email": email, "password": password})
    assert response.status_code == 302
    return client


def local_input(instant):
    return instant.astimezone(BRASILIA).strftime("%Y-%m-%dT%H:%M")


def popup_values(**overrides):
    return {
        "title": "Campanha de teste", "link": "https://example.com/campanha", "seconds": "3",
        "scope": "all", "frequency": "session", "enabled": "y", **overrides,
    }


def image_file():
    data = BytesIO()
    with Image.new("RGB", (640, 420), "#18455A") as image:
        image.save(data, "PNG")
    data.seek(0)
    return data, "publicidade.png"


def test_popup_upload_and_campaign_dates(app, popup_client):
    client = popup_client
    assert client.get("/admin/ads").status_code == 200
    assert b"Configurar pop-up" in client.get("/admin/ads").data
    assert b"data-ad-popup" not in client.get("/").data
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now + timedelta(hours=1)
    end = now + timedelta(hours=2)
    response = client.post("/admin/ads/popup", data=popup_values(
        image=image_file(), starts_at=local_input(start), ends_at=local_input(end), scope="home",
    ))
    assert response.status_code == 302
    assert b"Agendado" in client.get("/admin/ads/popup").data
    assert b"data-ad-popup" not in client.get("/").data
    with app.app_context():
        row = SiteSetting.query.filter_by(key=SETTING_KEY).one()
        config = json.loads(row.value)
        assert datetime.fromisoformat(config["starts_at"]) == start
        assert datetime.fromisoformat(config["ends_at"]) == end
        assert config["seconds"] == 3
        config["starts_at"] = (now - timedelta(hours=1)).isoformat()
        row.value = json.dumps(config)
        db.session.commit()
    assert client.get(config["image"]).status_code == 200
    assert b"data-ad-popup" in client.get("/").data
    assert b"https://example.com/campanha" in client.get("/").data
    assert b"data-ad-popup" not in client.get("/buscar?q=parana").data
    with app.app_context():
        row = SiteSetting.query.filter_by(key=SETTING_KEY).one()
        config["ends_at"] = (now - timedelta(minutes=1)).isoformat()
        row.value = json.dumps(config)
        db.session.commit()
    assert b"data-ad-popup" not in client.get("/").data


def test_popup_validates_settings_and_handles_pause(app, popup_client):
    client = popup_client
    assert app.test_client().post("/admin/ads/popup", data=popup_values()).status_code == 302
    assert client.post("/admin/ads/popup", data=popup_values()).status_code == 400
    assert client.post("/admin/ads/popup", data=popup_values(
        link="javascript:alert(1)", image=image_file(),
    )).status_code == 400
    assert client.post("/admin/ads/popup", data=popup_values(
        starts_at="2026-12-05T15:00", ends_at="2026-12-05T14:00", image=image_file(),
    )).status_code == 400
    assert client.post("/admin/ads/popup", data=popup_values(image=image_file())).status_code == 302
    assert client.post("/admin/ads/popup", data=popup_values()).status_code == 409
    with app.app_context():
        row = SiteSetting.query.filter_by(key=SETTING_KEY).one()
        config = json.loads(row.value)
    pause = popup_values(revision=config["revision"], seconds="0")
    del pause["enabled"]
    assert client.post("/admin/ads/popup", data=pause).status_code == 302
    assert b"Pausado" in client.get("/admin/ads/popup").data
    assert b"data-ad-popup" not in client.get("/").data
