from datetime import timedelta

from app.models import Post, User, db
from app.radar.group_delivery import portal_cover, send_pending_group
from app.radar.models import GroupDelivery, now
from test_radar import authenticated, portal, token


GROUP = "12345678@g.us"


def configuration(**updates):
    return {"group_id": GROUP, "group_name": "Redação de teste", "enabled": True, "news": True,
            "alerts": False, "connected": True, "available": True, "revision": "stage-test",
            "minimum": 75, "news_since": now().replace(tzinfo=None).isoformat() + "Z", **updates}


def test_group_form_has_csrf_and_saves_all_controls(portal, monkeypatch):
    app, user_id = portal
    client = authenticated(app, user_id)
    config = configuration()
    monkeypatch.setattr("app.radar.views.group_settings", lambda: config)
    calls = []
    def gateway(method, path, **kwargs):
        if method == "POST":
            calls.append(kwargs["json"])
            return config
        return {"groups": [{"id": GROUP, "name": config["group_name"], "available": True}]}
    monkeypatch.setattr("app.radar.views.gateway", gateway)
    page = client.get("/admin/radar/whatsapp")
    assert page.status_code == 200 and b"12345678@g.us" in page.data
    fields = {"group_id": GROUP, "enabled": "1", "news": "1", "alerts": "1",
              "minimum": "80", "revision": "stage-test"}
    assert client.post("/admin/radar/whatsapp/grupo", data=fields).status_code == 400
    assert client.post("/admin/radar/whatsapp/grupo", data={**fields, "csrf_token": token(page)}).status_code == 302
    assert calls == [{"group_id": GROUP, "enabled": True, "news": True, "alerts": True,
                      "minimum": 80, "revision": "stage-test"}]


def test_group_commands_require_selected_group_and_registered_admin(portal, monkeypatch):
    app, user_id = portal
    db.session.get(User, user_id).whatsapp = "+5545990000011"
    db.session.commit()
    monkeypatch.setenv("RADAR_WHATSAPP_TOKEN", "test-token")
    monkeypatch.setattr("app.radar.whatsapp.gateway", lambda *args: configuration())
    headers = {"Authorization": "Bearer test-token"}
    client = app.test_client()
    data = {"id": "TEST_GROUP_01", "text": "menu", "phone": "+5545990000011", "group_id": GROUP}
    target = "/internal/radar/whatsapp"
    assert client.post(target, json={**data, "group_id": "11111111@g.us"}, headers=headers).status_code == 403
    assert client.post(target, json={**data, "phone": "+5545990000022"}, headers=headers).status_code == 403
    response = client.post(target, json=data, headers=headers)
    assert "PARANÁ ATUAL | REDAÇÃO" in response.json["reply"]
    assert client.post(target, json=data, headers=headers).json["reply"] is None


def test_group_delivery_sends_recent_cms_post_once_without_historical_or_imported_posts(portal, monkeypatch):
    stamp = now()
    config = configuration(news_since=(stamp - timedelta(minutes=5)).isoformat() + "Z")
    new_post = Post(source="local", title="Notícia nova", slug="noticia-nova",
                    published_at=stamp, featured_image="/media/posts/capa.jpg")
    db.session.add_all([new_post,
                        Post(source="local", title="Arquivo", slug="arquivo", published_at=stamp - timedelta(days=5)),
                        Post(source="wp", title="Importação", slug="importacao", published_at=stamp)])
    db.session.commit()
    monkeypatch.setattr("app.radar.group_delivery.enabled", lambda: True)
    monkeypatch.setattr("app.radar.group_delivery.group_settings", lambda: config)
    sends = []
    def send(method, path, json):
        sends.append(json)
        return {"accepted": True}
    monkeypatch.setattr("app.radar.group_delivery.gateway", send)
    send_pending_group()
    send_pending_group()
    assert len(sends) == 1 and sends[0]["kind"] == "article" and sends[0]["group_id"] == GROUP
    assert "https://paranaatual.com.br/p/noticia-nova" in sends[0]["text"]
    assert sends[0]["cover"] == "https://paranaatual.com.br/media/posts/capa.jpg"
    assert GroupDelivery.query.one().state == "sent"
    new_post.featured_image = "https://elsewhere.example/media/test.jpg"
    assert portal_cover(new_post) is None


def test_calendar_limits_home_to_selected_brasilia_date(portal):
    app, user_id = portal
    client = authenticated(app, user_id)
    db.session.add_all([
        Post(title="Edição selecionada", slug="edicao-selecionada", published_at=now() - timedelta(days=2)),
        Post(title="Edição atual", slug="edicao-atual", published_at=now()),
    ])
    db.session.commit()
    from app.portal_tools import LOCAL_TZ
    from datetime import datetime
    day = (datetime.now(LOCAL_TZ).date() - timedelta(days=2)).isoformat()
    target = "/admin/calendario-home"
    assert client.post(target, data={"csrf_token": token(client.get(target)), "enabled": "1", "day": day}).status_code == 302
    assert b"edicao-selecionada" in client.get("/").data
    assert b"edicao-atual" not in client.get("/").data
    assert client.post(target, data={"csrf_token": token(client.get(target))}).status_code == 302
    assert b"edicao-atual" in client.get("/").data

