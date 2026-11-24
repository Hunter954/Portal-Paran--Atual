from datetime import timedelta

from app.models import Post, User, db
from app.radar.editorial import run_job
from app.radar.models import Article, ChatCard, ChatDraftReply, CoverSearch, Draft, DraftMedia, Job, Source, Story, now
from app.radar.wa_delivery import send_pending_draft_reply
from test_radar import portal


GROUP = "12345678@g.us"
PHONE = "+5545990000011"


def provision(portal, monkeypatch):
    app, actor = portal
    db.session.get(User, actor).whatsapp = PHONE
    db.session.commit()
    monkeypatch.setenv("RADAR_WHATSAPP_TOKEN", "qa-token")
    monkeypatch.setenv("RADAR_OLLAMA_URL", "http://local.test")
    monkeypatch.setenv("RADAR_ENABLE_PUBLISHING", "1")
    group = {"connected": True, "enabled": True, "available": True, "group_id": GROUP,
             "revision": "r1", "alerts": True, "minimum": 75}
    monkeypatch.setattr("app.radar.whatsapp.gateway", lambda *_a, **_k: group)
    monkeypatch.setattr("app.radar.editorial.report_sources", lambda sources, **_options: (sources, None))
    client = app.test_client()
    headers = {"Authorization": "Bearer qa-token"}
    sent = 0

    def send(text, quote=None, **overrides):
        nonlocal sent
        sent += 1
        event = {"phone": PHONE, "id": f"MESSAGE_TEST_{sent:03}", "text": text, "group_id": GROUP, **overrides}
        if quote:
            event["quote_id"] = quote
        return client.post("/internal/radar/whatsapp", json=event, headers=headers)

    return send, group


def seed(count):
    source = Source(name="Fonte local", url="https://fonte.example.org/feed", kind="rss")
    db.session.add(source)
    db.session.flush()
    for i in range(count):
        entry = Story(title=f"Seminário de educação {i} em Curitiba", location="Curitiba", region="Paraná",
                      category="Educação", score=77 if i % 2 else 52, created=now() - timedelta(days=2))
        entry.articles = [Article(source=source, title=entry.title, url=f"https://fonte.example.org/{i}",
                                  summary="Evento confirmado em Curitiba. A programação prevista inclui aulas gratuitas.",
                                  evidence="O evento ocorre em Curitiba", digest=f"test{i}")]
        db.session.add(entry)
    db.session.commit()


def test_menu_accepts_quantity_replays_cards_and_resumes_requested_pages(portal, monkeypatch):
    send, _ = provision(portal, monkeypatch)
    seed(13)
    assert send("7").json["reply"] is None
    assert "PARANÁ ATUAL | REDAÇÃO" in send("menu").json["reply"]
    assert "Quantas pautas" in send("9").json["reply"]
    first = send("12").json
    assert len(first["messages"]) == 10
    assert "Restam até 2" in first["reply"]
    later = send("MAIS").json
    assert len(later["messages"]) == 2
    assert not set(x["id"] for x in first["messages"]) & set(x["id"] for x in later["messages"])
    replay = send("RADAR REENVIAR").json
    assert [x["text"] for x in replay["messages"]] == [x["text"] for x in first["messages"]]
    assert len({x["id"] for x in replay["messages"]}) == 10
    urgent = send("RADAR URGENTES 4").json
    assert len(urgent["messages"]) == 4
    assert all("77/100" in entry["text"] for entry in urgent["messages"])
    assert "https://paranaatual.com.br/admin" not in send("menu").json["reply"]


def test_quote_postar_prepares_draft_sends_it_and_publishes_only_current_review(portal, monkeypatch):
    send, group = provision(portal, monkeypatch)
    seed(1)
    article = Article.query.one()
    original = send("RADAR LISTAR 1").json["messages"][0]
    result = send("POSTAR", original["id"]).json
    assert "lendo a reportagem" in result["reply"]
    assert Post.query.count() == 0
    request = ChatDraftReply.query.one()
    assert Job.query.one().state == "pending"
    monkeypatch.setattr("app.radar.editorial.generate", lambda sources: {
        "titulo": "Feira em Curitiba", "resumo": "Aulas gratuitas", "corpo": "A programação terá aulas.",
        "fatos": [{"fonte": article.id, "texto": "Aulas gratuitas"}], "pendencias": []})
    photo = {"path": "/media/radar/capa.jpg", "credit": "Fotógrafa", "description": "Feira no centro",
             "url": "https://fonte.example.org/capa.jpg"}
    run_job(Job.query.one())
    monkeypatch.setattr("app.radar.wa_delivery.enabled", lambda: True)
    monkeypatch.setattr("app.radar.wa_delivery.group_settings", lambda: group)
    deliveries = []

    def deliver(method, path, **data):
        assert path == "/radar/group/reply"
        assert data["json"]["group_id"] == GROUP
        assert CoverSearch.query.count() == 0
        deliveries.append(data["json"]["messages"])
        return {"accepted": True}

    monkeypatch.setattr("app.radar.wa_delivery.gateway", deliver)
    send_pending_draft_reply()
    send_pending_draft_reply()
    assert len(deliveries) == 1 and request.state == "sent"
    draft = deliveries[0][-1]
    assert "PUBLICAR" in draft["text"] and "REFAZER" in draft["text"]
    assert not any(message.get("cover") for message in deliveries[0])
    assert CoverSearch.query.one().target == {"story": Job.query.one().story_id, "version": 1}
    db.session.add(DraftMedia(draft_id=Draft.query.one().id, **photo))
    db.session.commit()
    assert ChatCard.query.filter_by(message_id=draft["id"]).one().version == 1
    updated = send("TÍTULO Oficina em Curitiba", draft["id"]).json["messages"][-1]
    assert ChatCard.query.filter_by(message_id=updated["id"]).one().version == 2
    assert "mudou" in send("POSTAR", draft["id"]).json["reply"]
    assert Post.query.count() == 0
    assert "Foto de referência" in updated["text"]
    assert "Informe quem autorizou" in send("FOTO", updated["id"]).json["reply"]
    authorized = send("FOTO AUTORIZADA Assessoria do evento", updated["id"]).json["messages"][-1]
    assert "Foto de referência" not in authorized["text"]
    event = "MESSAGE_PUBLICAR_TESTE"
    public = send("PUBLICAR", authorized["id"], id=event).json["reply"]
    assert "publicada" in public and "https://paranaatual.com.br/p/" in public
    assert Post.query.one().source == "radar"
    assert Post.query.one().featured_image == photo["path"]
    assert Post.query.one().image_credit == photo["credit"]
    assert send("PUBLICAR", authorized["id"], id=event).json["reply"] is None
    assert Post.query.count() == 1


def test_private_quotes_are_scoped_and_pending_facts_appear_in_preview(portal, monkeypatch):
    send, _ = provision(portal, monkeypatch)
    seed(1)
    group_card = send("RADAR LISTAR 1").json["messages"][0]
    assert "informe o ID" in send("POSTAR", group_card["id"], group_id=None).json["reply"]
    assert send("POSTAR", group_card["id"], phone="+5545990000022").status_code == 403
    assert "lendo a reportagem" in send("POSTAR", group_card["id"]).json["reply"]
    monkeypatch.setattr("app.radar.editorial.generate", lambda sources: {
        "titulo": "Seminário em Curitiba", "resumo": "Rubrica", "corpo": "Aulas gratuitas.",
        "fatos": [], "pendencias": ["Confirme quem organiza."]})
    run_job(Job.query.one())
    preview = send(f"RADAR VER {Story.query.one().id}").json["messages"][-1]
    assert "A conferir: Confirme quem organiza." in preview["text"]
    assert Post.query.count() == 0
    assert "publicada" in send("PUBLICAR", preview["id"]).json["reply"]
    assert Post.query.one().featured_image == "/static/img/logo-parana-atual.svg"



def test_sources_and_scheduling_can_be_managed_in_chat(portal, monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from app.radar.models import Run
    send, _ = provision(portal, monkeypatch)
    seed(1)
    result = send("RADAR FONTE ADICIONAR Prefeitura | https://prefeitura.example.org/noticias.xml | rss").json["reply"]
    assert "cadastrada" in result
    source = Source.query.filter_by(name="Prefeitura").one()
    assert "Prefeitura" in send("RADAR FONTES").json["reply"]
    assert "na fila" in send(f"RADAR COLETAR {source.id}").json["reply"]
    assert Run.query.filter_by(source_id=source.id).count() == 1
    assert "pausada" in send(f"RADAR FONTE PAUSAR {source.id}").json["reply"]
    assert "Ative a fonte" in send(f"RADAR COLETAR {source.id}").json["reply"]
    assert "ativada" in send(f"RADAR FONTE ATIVAR {source.id}").json["reply"]
    card = send("RADAR LISTAR 1").json["messages"][0]
    send("POSTAR", card["id"])
    monkeypatch.setattr("app.radar.editorial.generate", lambda sources: {
        "titulo": "Aulas em Curitiba", "resumo": "Curso", "corpo": "Inscrições gratuitas.",
        "fatos": [], "pendencias": []})
    run_job(Job.query.one())
    story = Story.query.one()
    preview = send(f"RADAR VER {story.id}").json["messages"][0]
    when = datetime.now(ZoneInfo("America/Sao_Paulo")) + timedelta(days=1)
    assert "agendada" in send(f"RADAR AGENDAR {when:%d/%m/%Y %H:%M}", preview["id"]).json["reply"]
    assert story.state == "scheduled" and Post.query.count() == 0
    assert "cancelado" in send(f"RADAR CANCELAR {story.id}").json["reply"]
    assert story.state == "review" and story.scheduled is None


def test_standard_post_adds_photo_and_publishes_from_quoted_preview(portal, monkeypatch, tmp_path):
    import base64

    app, _ = portal
    app.config["MEDIA_ROOT"] = str(tmp_path)
    send, _ = provision(portal, monkeypatch)
    assert "Matérias" in send("MENU").json["reply"]
    assert "Nova matéria" in send("2").json["reply"]
    assert "linha em branco" in send("1").json["reply"]
    assert "texto completo" in send("Feira em Guarapuava").json["reply"]
    post_card = send("A feira começou no domingo.\n\nA entrada foi gratuita.").json["messages"][-1]
    assert "PUBLICAR" in post_card["text"]
    assert send("", post_card["id"], image_offer=True).json["need_image"]
    assert not send("", image_offer=True).json["need_image"]
    image = {"mime": "image/jpeg", "data": base64.b64encode(b"\xff\xd8\xfflocal-cover").decode()}
    updated = send("Crédito da redação", post_card["id"], image=image).json["messages"]
    assert updated[0]["cover"].startswith("https://paranaatual.com.br/media/radar/")
    assert "Crédito da redação" in updated[0]["text"]
    assert "mudou" in send("PUBLICAR", post_card["id"]).json["reply"]
    assert "publicada" in send("PUBLICAR", updated[-1]["id"]).json["reply"]
    assert Post.query.one().published_at
    assert Post.query.one().image_credit == "Crédito da redação"
    assert app.test_client().get(Post.query.one().featured_image).status_code == 200


def test_topic_image_search_stays_in_chat_and_updates_selected_post(portal, monkeypatch):
    from app.radar.models import CoverSearch
    from app.radar.wa_cover import run_cover_search

    send, group = provision(portal, monkeypatch)
    card = send("CMS NOVA Mostra em Curitiba | Haverá oficinas abertas.").json["messages"][-1]
    assert "Buscando fotos" in send("BUSCAR FOTO Parque Barigui", card["id"]).json["reply"]
    assert CoverSearch.query.one().term == "Parque Barigui"
    photo = {"path": "/media/radar/photo.jpg", "credit": "Autora", "description": "Parque Barigui",
             "url": "https://example.org/photo.jpg", "license": "CC BY 4.0"}
    monkeypatch.setattr("app.radar.wa_cover.enabled", lambda: True)
    monkeypatch.setattr("app.radar.wa_cover.group_settings", lambda: group)
    monkeypatch.setattr("app.radar.wa_cover.search_images", lambda *_: (
        "Wikimedia Commons", [(photo["url"], photo["credit"], photo["description"], photo["license"], "")]))
    monkeypatch.setattr("app.radar.wa_cover.save_photo", lambda *_: photo)
    delivered = []

    def gateway_result(_method, _path, **params):
        delivered.extend(params["json"]["messages"])
        return {"accepted": True}

    monkeypatch.setattr("app.radar.wa_cover.gateway", gateway_result)
    run_cover_search()
    assert "Wikimedia Commons" in delivered[0]["text"]
    updated = send("USAR FOTO", delivered[0]["id"]).json["messages"]
    assert updated[0]["cover"].endswith(photo["path"])
    assert Post.query.one().featured_image == photo["path"]


def test_editorial_interruption_keeps_sources_available_and_blocks_empty_publication(portal, monkeypatch):
    from app.radar.editorial import EditorialError, latest_draft

    send, _ = provision(portal, monkeypatch)
    seed(1)
    story = Story.query.one()
    assert "Envio o texto primeiro" in send(f"RADAR VER {story.id}").json["reply"]

    def timeout(_sources):
        raise EditorialError("O modelo local demorou mais que o limite.")

    monkeypatch.setattr("app.radar.editorial.generate", timeout)
    run_job(Job.query.one())
    draft = latest_draft(story.id)
    assert draft.body == "" and draft.facts
    preview = send(f"RADAR VER {story.id}").json["messages"][-1]
    assert "Fontes da pauta" in preview["text"] and "Responda *TEXTO*" in preview["text"]
    assert "Envie o texto" in send("PUBLICAR", preview["id"]).json["reply"]
    edited = send("TEXTO A programação tem aulas gratuitas em Curitiba.", preview["id"]).json["messages"][-1]
    assert "publicada" in send("PUBLICAR", edited["id"]).json["reply"]


def test_prefetch_uses_the_idle_writer_then_prioritizes_a_chat_request(portal, monkeypatch):
    from app.radar.editorial import queue_draft
    from app.radar.worker import editorial_step

    _, actor = portal
    monkeypatch.setenv("RADAR_OLLAMA_URL", "http://local.test")
    seed(2)
    recent, earlier = Story.query.order_by(Story.id.desc()).all()
    queue_draft(recent, None)
    queue_draft(earlier, actor)
    db.session.commit()
    order = []

    def take_job(job):
        order.append(job.story_id)
        job.state = "done"
        db.session.commit()

    monkeypatch.setattr("app.radar.worker.run_job", take_job)
    monkeypatch.setattr("app.radar.worker.send_pending_draft_reply", lambda: None)
    assert editorial_step()
    assert editorial_step()
    assert order == [earlier.id, recent.id]

