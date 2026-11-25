"""Conversas e cartões editoriais respondíveis no WhatsApp."""

from datetime import timedelta
import re
from secrets import token_hex

from sqlalchemy import select

from ..models import Post, db
from .editorial import PROMPT_VERSION, EditorialError, can_publish, latest_draft, lock_story, publish, queue_draft, save_draft
from .geography import normalize
from .models import ChatCard, ChatDraftReply, ChatSession, Draft, DraftMedia, Story, MANUAL_REGION, audit, now


OPEN_STATES = ("new", "research", "production", "review")
PAGE_SIZE = 10
from .wa_radar_menu import HOME as HELP


def chat_session(actor, group):
    scope = group or ""
    result = db.session.execute(select(ChatSession).where(
        ChatSession.actor_id == actor, ChatSession.scope == scope
    ).with_for_update()).scalar_one_or_none()
    if result is None:
        result = ChatSession(actor_id=actor, scope=scope, mode="idle")
        db.session.add(result)
    elif result.updated < now() - timedelta(minutes=30):
        result.mode, result.before_id, result.remaining = "idle", None, 0
    result.updated = now()
    return result


def reply_card(story, text, actor, group, draft=None):
    identifier = "3EB0" + token_hex(9).upper()
    ref = ChatCard(message_id=identifier, story_id=story.id, actor_id=actor, scope=group or "",
                   version=draft.version if draft else None)
    db.session.add(ref)
    return {"id": identifier, "text": text}


def story_card(story, actor, group):
    article = story.articles[0]
    summary = article.summary[:330].strip()
    label = {
        "new": "Nova", "research": "Em apuração", "production": "Preparando rascunho",
        "review": "Rascunho pronto", "published": "Publicada", "discarded": "Arquivada",
        "scheduled": "Agendada",
    }.get(story.state, story.state)
    text = (f"*Pauta #{story.id} · {label}*\n{story.title}\n"
            f"{story.location} · {story.score}/100 · {article.source.name}\n"
            f"{summary}\n\n*1* Preparar prévia\n*2* Ver fontes\n*0* Menu do Radar")
    return reply_card(story, text, actor, group)


def draft_cards(story, draft, actor, group):
    from .group_delivery import PUBLIC_URL
    photo = db.session.get(DraftMedia, draft.id)
    from .wa_cover import generic_photo
    if photo and generic_photo(photo):
        photo = None
    images = [{"text": f"Foto: {photo.credit}" + (f"\n{photo.description}" if photo.description else ""),
               "cover": PUBLIC_URL + photo.path}] if photo else []
    source = story.articles[0]
    if draft.body.strip():
        full = (f"*Prévia · #{story.id}*\n\n*{draft.title}*\n"
                f"{draft.excerpt}\n\n{draft.body}\n\nFonte: {source.url}")
    else:
        full = f"*Fontes da pauta #{story.id}*\n\n*{draft.title}*\nFonte: {source.url}"
        first_fact = draft.facts[0].get("texto", "") if draft.facts else ""
        words = first_fact.split()
        if words:
            snippet = " ".join(words[:24])
            full += "\n\nTrecho da fonte\n" + snippet + ("…" if len(words) > 24 else "")
    if draft.body.strip() and not photo:
        full += "\nFoto: buscando na reportagem original."
    if draft.pending:
        full += "\n\nA conferir: " + " · ".join(draft.pending[:2])
    from .wa_radar_menu import PREVIEW, remember_preview
    confirmation = PREVIEW
    if len(full) + len(confirmation) + 2 <= 3100:
        card = reply_card(story, full + "\n\n" + confirmation, actor, group, draft)
        remember_preview(story,draft,actor,group,card['id'])
        return [*images, card]
    chunks = []
    remaining = full
    while remaining:
        candidate = remaining[:3000]
        boundary = candidate.rfind("\n")
        size = boundary if len(remaining) > 3000 and boundary > 1600 else len(candidate)
        chunks.append({"text": remaining[:size]})
        remaining = remaining[size:].lstrip()
    card = reply_card(story,confirmation,actor,group,draft)
    remember_preview(story,draft,actor,group,card['id'])
    return [*images, *chunks, card]


def open_radar(actor, group):
    from .wa_radar_menu import enter,state_for
    return enter(state_for(actor,group),actor,group)


def list_stories(actor, group, count=None, *, urgent=False, more=False):
    session = chat_session(actor, group)
    session.mode = "radar"
    if not more:
        session.page_limit, session.remaining = count or 10, count or 10
        session.before_id, session.urgent = None, urgent
    if not session.remaining:
        return "A lista terminou. Digite *RADAR LISTAR 10* ou escolha outra quantidade."
    query = Story.query.filter(Story.region != MANUAL_REGION, Story.state.in_(OPEN_STATES), Story.articles.any())
    if session.urgent:
        query = query.filter(Story.score >= 75)
    if session.before_id:
        query = query.filter(Story.id < session.before_id)
    size = min(PAGE_SIZE, session.remaining)
    rows = query.order_by(Story.id.desc()).limit(size + 1).all()
    stories = rows[:size]
    if not stories:
        session.remaining = 0
        return "Nenhuma pauta nessa fila agora."
    session.before_id = stories[-1].id
    session.remaining = session.remaining - len(stories) if len(rows) > size else 0
    title = f"*{len(stories)} pautas {'urgentes' if session.urgent else 'mais recentes'} do Radar*"
    if session.remaining:
        title += f"\nDigite *MAIS* para continuar. Restam até {session.remaining} nesta solicitação."
    return {"reply": title, "messages": [story_card(story, actor, group) for story in stories]}


def quoted_card(quote_id, actor, group):
    if not quote_id:
        return None
    card = db.session.get(ChatCard, quote_id)
    if (not card or card.scope != (group or "") or (not group and card.actor_id != actor)
            or card.created < now() - timedelta(days=30)):
        return None
    return card


def legacy_draft(draft):
    origin = Draft.query.filter(Draft.story_id == draft.story_id, Draft.version <= draft.version,
                                Draft.model.isnot(None)).order_by(Draft.version.desc()).first()
    return bool(origin and not origin.model.endswith((":radar-pt-br-v3", ":" + PROMPT_VERSION)))


def request_draft(story, actor, group, *, fresh=False):
    latest = latest_draft(story.id)
    if latest and story.state == "review" and not fresh and not legacy_draft(latest):
        from .wa_cover import queue_draft_cover
        queue_draft_cover(story, latest, actor, group)
        return {"messages": draft_cards(story, latest, actor, group)}
    job = queue_draft(story, actor)
    db.session.flush()
    previous = ChatDraftReply.query.filter_by(job_id=job.id, actor_id=actor, scope=group or "").first()
    if not previous:
        db.session.add(ChatDraftReply(job_id=job.id, actor_id=actor, scope=group or ""))
    return f"Estou lendo a reportagem da pauta *#{story.id}*. Envio o texto primeiro e as fotos em seguida."


def confirm_story(story, version, actor, group=None):
    existing = db.session.get(Post, story.post_id) if story.post_id else None
    if existing:
        from .group_delivery import post_url
        return f"A pauta *#{story.id}* já está publicada.\n{post_url(existing)}"
    latest = latest_draft(story.id)
    if not latest or latest.version != version or story.state != "review":
        raise EditorialError("Essa prévia mudou ou saiu da fila de revisão. Responda *VER* para receber a versão atual.")
    if story.region != MANUAL_REGION and not any(article.evidence for article in story.articles):
        raise EditorialError("A pauta precisa de uma fonte com o vínculo documentado com o Paraná.")
    if legacy_draft(latest):
        return request_draft(story, actor, group, fresh=True)
    with db.session.begin_nested():
        story.approved_draft_id = latest.id
        post = publish(story, version, actor)
        audit("whatsapp_publicada", actor=actor, story=story.id, details=f"Versão {version}")
    from .group_delivery import post_url
    return f"✅ *Pauta #{story.id} publicada*\n{post.title}\n{post_url(post)}"


def edit_draft(story, version, action, value, actor, group):
    latest = latest_draft(story.id)
    if not latest or latest.version != version or story.state != "review":
        raise EditorialError("Responda na prévia da versão atual para editar.")
    fields = {"titulo": ("titulo", 500), "resumo": ("resumo", 2000), "texto": ("corpo", 10000)}
    field, limit = fields[action]
    if not value or len(value) > limit:
        raise EditorialError(f"Envie de 1 a {limit} caracteres para este campo.")
    values = {"titulo": latest.title, "resumo": latest.excerpt, "corpo": latest.body}
    values[field] = value
    result = save_draft(story, values, actor)
    return {"messages": draft_cards(story, result, actor, group)}


def authorize_cover(story, card, raw, actor, group):
    draft = latest_draft(story.id)
    if not can_publish(actor) or not card or not draft or card.version != draft.version or story.state != "review":
        raise EditorialError("Responda na prévia atual com *FOTO AUTORIZADA* e a origem da autorização.")
    photo = db.session.get(DraftMedia, draft.id)
    parts = raw.split(maxsplit=2)
    reference = parts[2].strip() if len(parts) == 3 else ""
    if not photo or not 3 <= len(reference) <= 250 or normalize(parts[1]) != "autorizada":
        raise EditorialError("Informe quem autorizou a foto, por exemplo *FOTO AUTORIZADA Assessoria do evento*.")
    values = {"titulo": draft.title, "resumo": draft.excerpt, "corpo": draft.body}
    saved = save_draft(story, values, actor, photo={"path": photo.path, "credit": photo.credit,
                      "description": photo.description, "url": photo.url, "license": reference})
    audit("foto_autorizada", actor=actor, story=story.id, details=f"Versão {saved.version} · {reference}")
    return {"messages": draft_cards(story, saved, actor, group)}


def save_cover_from_message(card, actor, group, text, image):
    from .wa_cover import media_from_editor
    require_draft = lock_story(card.story_id)
    draft = latest_draft(card.story_id)
    if not can_publish(actor) or not draft or draft.version != card.version or require_draft.state != "review":
        raise EditorialError("Envie a foto na prévia atual da pauta.")
    photo = media_from_editor(f"|{text}" if text else "", image)
    saved = save_draft(require_draft, {"titulo": draft.title, "resumo": draft.excerpt, "corpo": draft.body}, actor, photo=photo)
    audit("whatsapp_radar_foto_enviada", actor=actor, story=require_draft.id)
    return {"messages": draft_cards(require_draft, saved, actor, group)}

def radar_status():
    total = Story.query.filter(Story.region != MANUAL_REGION,Story.state.in_(OPEN_STATES)).count()
    urgent = Story.query.filter(Story.region != MANUAL_REGION,Story.state.in_(OPEN_STATES), Story.score >= 75).count()
    from .group_delivery import group_settings
    try:
        settings = group_settings()
    except EditorialError:
        settings = {}
    label = (f"Alertas automáticos a partir de {settings.get('minimum', 75)}/100, "
             "para pautas novas após a ativação.") if settings.get("alerts") else "Alertas automáticos desligados."
    return f"{total} pautas abertas · {urgent} urgentes.\n{label}\nUse *RADAR LISTAR 10* para buscar o histórico recente."


def radar_command(text, actor, group, quote_id=None):
    raw = text.strip()
    plain = normalize(raw)
    words = plain.split()
    if not words:
        return HELP
    explicit = words[0] == "radar"
    if explicit:
        prefix = re.match(r"\S+\s*", raw)
        raw = raw[prefix.end():]
        plain = normalize(raw)
        words = plain.split()
    if not words or plain in {"ajuda", "menu"}:
        return open_radar(actor, group)
    action = words[0]
    session = chat_session(actor, group)
    if plain.isdecimal():
        if not explicit and session.mode != "radar":
            return None
        return list_stories(actor, group, min(9999, int(plain))) if int(plain) > 0 else HELP
    if action in {"listar", "ultimas", "pendentes", "urgentes", "reenviar"}:
        number = words[1] if len(words) == 2 else ""
        if len(words) > 2 or number and (not number.isdecimal() or len(number) > 4 or int(number) == 0):
            return "Informe a quantidade. Exemplo: *RADAR LISTAR 10*."
        return list_stories(actor, group, int(number) if number else (session.page_limit if action == "reenviar" else 10),
                            urgent=action == "urgentes")
    if plain == "mais":
        return list_stories(actor, group, more=True)
    if plain == "status":
        return radar_status()
    if action in {"fonte", "coletar"} or (plain == "fontes" and not quote_id):
        from .wa_actions import source_command
        return source_command(raw, actor)
    if action not in {"ver", "fontes", "foto", "buscar", "titulo", "resumo", "texto", "postar", "publicar", "confirmar", "aprovar", "gerar", "refazer",
                      "descartar", "restaurar", "apurar", "apurado", "agendar", "cancelar"}:
        return HELP

    card = quoted_card(quote_id, actor, group)
    target = re.fullmatch(r"(?:#?(?:pa-)?)?([0-9]{1,9})", words[1]) if len(words) > 1 else None
    editing = card and card.version is not None and action in {"titulo", "resumo", "texto"} and len(words) > 1
    if card and len(words) > 1 and action in {"postar", "publicar", "confirmar"}:
        return "Responda apenas *PUBLICAR* na prévia que você quer publicar."
    story_id = card.story_id if card else (int(target[1]) if target else None)
    if not story_id:
        return "Responda em uma pauta recebida ou informe o ID. Exemplo: *RADAR POSTAR 92*."
    story = lock_story(story_id)
    if not story or not story.articles:
        return "Pauta não encontrada."
    if action == "agendar":
        from .wa_actions import schedule_from_preview
        return schedule_from_preview(story, card, raw, actor)
    if action == "cancelar":
        if story.state != "scheduled" or not can_publish(actor):
            raise EditorialError("Esta pauta não pode ter o agendamento cancelado por este administrador.")
        story.state, story.scheduled, story.approved_draft_id = "review", None, None
        story.updated = now()
        audit("whatsapp_cancelou_agendamento", actor=actor, story=story.id)
        return f"Agendamento da pauta *#{story.id}* cancelado. Ela voltou para revisão."
    if editing:
        value = raw.split(maxsplit=1)[1]
        return edit_draft(story, card.version, action, value.strip(), actor, group)
    if action == "foto":
        return authorize_cover(story, card, raw, actor, group)
    if action == "buscar" and len(words) > 1 and words[1] in {"foto", "capa"}:
        current = latest_draft(story.id)
        if not card or not current or card.version != current.version or story.state != "review":
            raise EditorialError("Abra a prévia atual da pauta para buscar a capa.")
        from .wa_cover import queue_search
        parts = raw.split(maxsplit=2)
        return queue_search(actor, group, {"story": story.id, "version": current.version}, current.title, story.location,
                            parts[2] if len(parts) == 3 else "")
    if action == "apurado":
        latest = latest_draft(story.id)
        if not card or not latest or card.version != latest.version or story.state != "review":
            raise EditorialError("Responda *APURADO* na prévia da versão atual depois de conferir as fontes.")
        result = save_draft(story, {"titulo": latest.title, "resumo": latest.excerpt, "corpo": latest.body,
                                    "pendencias": []}, actor)
        audit("whatsapp_apuracao_concluida", actor=actor, story=story.id)
        return {"messages": draft_cards(story, result, actor, group)}
    if action == "fontes":
        return "\n".join(f"{a.source.name}\n{a.url}" for a in story.articles[:5])
    if action in {"ver", "resumo"}:
        if story.state in OPEN_STATES:
            return request_draft(story, actor, group)
        return {"messages": [story_card(story, actor, group)]}
    if action in {"postar", "publicar", "confirmar"}:
        if card and card.version is not None:
            return confirm_story(story, card.version, actor, group)
        if action == "confirmar":
            if target and len(words) == 3 and re.fullmatch(r"[0-9]{1,6}", words[2]):
                return confirm_story(story, int(words[2]), actor, group)
            return "Responda *PUBLICAR* na prévia."
        if story.post_id:
            return confirm_story(story, 0, actor, group)
        return request_draft(story, actor, group)
    if action in {"gerar", "refazer"}:
        return request_draft(story, actor, group, fresh=True)
    if action == "restaurar" and story.state == "discarded":
        story.state = story.previous_state or "new"
        story.previous_state = None
    elif story.state in OPEN_STATES and action in {"aprovar", "descartar", "apurar"}:
        story.previous_state = story.state
        story.state = {"aprovar": "production", "descartar": "discarded", "apurar": "research"}[action]
    else:
        raise EditorialError("A pauta não permite esta ação no estado atual.")
    story.updated = now()
    audit("whatsapp_" + action, actor=actor, story=story.id)
    return f"Ação registrada na pauta *#{story.id}*."

