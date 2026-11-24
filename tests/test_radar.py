from datetime import timedelta
import hashlib
import hmac

import pytest

from flask import g

from app import create_app
from app.models import Post, User, db
from app.radar.collector import run_collection, similar, store_item
from app.radar.editorial import EditorialError, publish, save_draft, validate_draft
from app.radar.feeds import Item, date_from, read_xml
from app.radar.fetch import FetchError, Response, canonical_url, public_url
from app.radar.geography import classify
from app.radar.migration import upgrade
from app.radar.models import Article, Run, Source, Story, now


@pytest.fixture
def portal(monkeypatch, tmp_path):
    monkeypatch.setattr("app.Config.SQLALCHEMY_DATABASE_URI", "sqlite://")
    monkeypatch.setattr("app.Config.MEDIA_ROOT", str(tmp_path))
    app = create_app()
    app.config.update(TESTING=True, SECRET_KEY="radar-test-only")
    with app.app_context():
        upgrade()
        user = User(email="qa@example.org", is_admin=True, is_active=True)
        user.set_password("editor-local")
        db.session.add(user)
        db.session.commit()
        yield app, user.id


def authenticated(app, user_id):
    client = app.test_client()
    g.pop("_login_user", None)
    user = db.session.get(User, user_id)
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["admin_password_stamp"] = hmac.new(app.secret_key.encode(), user.password_hash.encode(),
                                                   hashlib.sha256).hexdigest()
    return client


def token(response):
    from bs4 import BeautifulSoup
    return BeautifulSoup(response.data, "html.parser").find("input", attrs={"name": "csrf_token"})["value"]


@pytest.mark.parametrize(("title", "summary", "expected"), [
    ("Prefeitura amplia vacinação em Foz do Iguaçu", "", "Foz do Iguaçu"),
    ("Curitiba tem alagamentos nesta tarde", "", "Curitiba"),
    ("Paraná registra aumento de atendimentos", "", "Paraná"),
    ("Temporal atinge Terra Boa (PR)", "", "Terra Boa"),
    ("Alerta em Palmas", "", None),
    ("Incêndio afeta centro comercial em São Paulo", "O grupo possui lojas no Paraná.", None),
    ("Paraná Clube joga em Santa Catarina", "", None),
    ("Supermercado abre em Campina Grande (PB)", "", None),
    ("Campeonato nacional começa neste domingo", "A rodada terá equipes de doze estados.", None),
    ("Cheia afeta o Rio Paraná", "A cheia aconteceu no Paraguai.", None),
])
def test_geography_requires_evidence(title, summary, expected):
    result = classify(title, summary)
    assert (result.name if result else None) == expected


@pytest.mark.parametrize("url", ["http://127.0.0.1/feed", "https://example.org:8080/", "file:///etc/passwd",
                                "http://169.254.169.254/", "http://a.internal/", "https://user@site.com/rss"])
def test_cannot_configure_internal_url(url):
    with pytest.raises(FetchError):
        public_url(url)


def test_feeds_and_dates():
    rss = b"""<rss version="2.0"><channel><title>Portal</title>
    <item><title>Chuva em Curitiba</title><link>https://site.com.br/a?utm_medium=rss</link>
    <description>&lt;p&gt;Info local&lt;/p&gt;</description><pubDate>2026-11-22T10:00:00-03:00</pubDate></item>
    </channel></rss>"""
    result = read_xml(Response("https://site.com.br/feed", 200, {}, rss))[0]
    assert result.url == "https://site.com.br/a"
    assert result.summary == "Info local"
    assert result.published.hour == 13
    assert date_from("2026-11-22") is None
    assert canonical_url("https://site.com.br/x?id=1&fbclid=x") == "https://site.com.br/x?id=1"
    hostile_xml = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>'
    with pytest.raises(FetchError):
        read_xml(Response(result.url, 200, {}, hostile_xml))


def test_source_panel_crud_and_queue(portal):
    app, user_id = portal
    assert app.test_client().get("/admin/radar/fontes").status_code == 302
    client = authenticated(app, user_id)
    assert client.get("/admin/radar/").status_code == 200
    url = "/admin/radar/fontes/nova"
    page = client.get(url)
    assert page.status_code == 200
    fields = dict(name="Portal de teste", url="https://site.com.br/feed", kind="rss",
                  priority="4", interval="10", active="1")
    assert client.post(url, data=fields).status_code == 400
    assert client.post(url, data={**fields, "csrf_token": token(page)}).status_code == 302
    source = Source.query.one()
    panel = client.get("/admin/radar/fontes")
    assert "Portal de teste".encode() in panel.data
    test_url = f"/admin/radar/fontes/{source.id}/test"
    for _ in range(2):
        assert client.post(test_url, data={"csrf_token": token(panel)}).status_code == 302
    assert Run.query.count() == 1
    assert client.get(f"/admin/radar/fontes/{source.id}/historico").status_code == 200
    assert client.get("/admin/radar/saude").status_code == 200


def test_collection_queues_only_recent_parana_items(portal, monkeypatch):
    app, user_id = portal
    source = Source(name="Revista nacional", url="https://site.com.br/feed", kind="rss", region="Paraná")
    db.session.add(source)
    db.session.flush()
    run = Run(source_id=source.id, kind="manual")
    db.session.add(run)
    db.session.commit()
    articles = [Item("https://site.com.br/1", "Temporal em Curitiba", "Texto local", now()),
                Item("https://site.com.br/2", "Congresso apresenta projeto nacional", "Alcance federal", now()),
                Item("https://site.com.br/3", "Chuva em Londrina", "", now() - timedelta(days=14))]
    monkeypatch.setattr("app.radar.collector.get_feed",
                        lambda fetcher, source: (Response(source.url, 200, {}, b""), articles))
    run_collection(run)
    assert run.state == "done"
    assert run.received == 3 and run.accepted == 1 and run.excluded == 2
    assert Article.query.one().title == "Temporal em Curitiba"
    client = authenticated(app, user_id)
    assert b"Temporal em Curitiba" in client.get("/admin/radar/?q=temporal").data
    assert b"Congresso apresenta" not in client.get("/admin/radar/?tab=all").data
    story = Story.query.one()
    assert client.get(f"/admin/radar/pautas/{story.id}").status_code == 200


def test_fetch_failure_and_test_mode(portal, monkeypatch):
    source = Source(name="Portal", url="https://site.com.br/feed", kind="rss", active=False)
    db.session.add(source)
    db.session.flush()
    run = Run(source_id=source.id, kind="test")
    db.session.add(run)
    db.session.commit()
    def unavailable(*args):
        raise FetchError("Falha verificada.")
    monkeypatch.setattr("app.radar.collector.get_feed", unavailable)
    run_collection(run)
    assert run.state == "error" and source.failures == 1
    assert Article.query.count() == 0


def test_duplicate_and_publication_require_current_version(portal, monkeypatch):
    app, user_id = portal
    title = "Temporal fecha ruas em Curitiba"
    source = Source(name="Portal", url="https://site.com.br/feed", kind="rss")
    db.session.add(source)
    db.session.flush()
    first = Item("https://site.com.br/x", title, "Há ruas alagadas.", now())
    second = Item("https://portal.com.br/x", title, "Há ruas alagadas.", now())
    assert store_item(source, first, classify(first.title, first.summary))
    assert not store_item(source, first, classify(first.title, first.summary))
    assert store_item(source, second, classify(second.title, second.summary))
    assert Story.query.count() == 1 and Article.query.count() == 2
    assert not similar("Temporal mata 2 pessoas em Curitiba", "Temporal mata 4 pessoas em Curitiba")
    story = Story.query.one()
    draft = save_draft(story, {"titulo": title, "resumo": first.summary, "corpo": "Redação original."}, user_id)
    monkeypatch.setenv("RADAR_ENABLE_PUBLISHING", "1")
    with pytest.raises(EditorialError):
        publish(story, draft.version, user_id)
    story.approved_draft_id = draft.id
    with pytest.raises(EditorialError):
        publish(story, draft.version + 1, user_id)
    post = publish(story, draft.version, user_id)
    db.session.commit()
    assert post.id == publish(story, draft.version, user_id).id
    assert Post.query.count() == 1
    from flask import url_for
    with app.test_request_context():
        public = url_for("site.post", slug=post.slug)
    assert app.test_client().get(public).status_code == 200


def test_generated_draft_rejects_invented_attribution():
    with pytest.raises(EditorialError):
        validate_draft({"titulo": "Teste", "resumo": "", "corpo": "Texto.", "pendencias": [],
                        "fatos": [{"texto": "Citação", "fonte": 2}]}, {1})



def test_site_listing_checks_origin():
    from bs4 import BeautifulSoup
    from app.radar.feeds import read_listing

    document = BeautifulSoup("""
       <article><h2><a href="/noticias/1">Temporal fecha ruas em Curitiba</a></h2>
       <time datetime="2026-11-24T10:00:00-03:00"></time><p>Chuvas no bairro.</p></article>
       <article><h3><a href="https://outside.net/1">Uma manchete de outro domínio</a></h3></article>
    """, "html.parser")
    result = read_listing(Response("https://site.com.br/", 200, {}, b""), document)
    assert len(result) == 1 and result[0].published.hour == 13


def test_whatsapp_rejects_unauthorized_and_duplicate_commands(portal, monkeypatch):
    from app.radar.models import InboundMessage, WhatsAppPreference

    app, user_id = portal
    user = db.session.get(User, user_id)
    user.whatsapp = "+5545000000000"
    db.session.add(WhatsAppPreference(user_id=user.id, phone=user.whatsapp, enabled=True))
    db.session.commit()
    monkeypatch.setenv("RADAR_WHATSAPP_TOKEN", "test-wa-scoped")
    url = "/internal/radar/whatsapp"
    headers = {"Authorization": "Bearer test-wa-scoped"}
    message = {"phone": user.whatsapp, "id": "TEST9000", "text": "RADAR AJUDA"}
    client = app.test_client()
    assert client.post(url, json=message).status_code == 401
    assert client.post(url, json={**message, "phone": "+5545000000011"}, headers=headers).status_code == 403
    assert "RADAR URGENTES" in client.post(url, json=message, headers=headers).json["reply"]
    assert client.post(url, json=message, headers=headers).json["reply"] is None
    assert InboundMessage.query.count() == 1

