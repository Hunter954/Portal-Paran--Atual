import hmac
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from secrets import token_hex
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import select

from ..admin import _ensure_unique_slug
from ..models import Category, Post, db
from .editorial import EditorialError, can_publish
from .geography import normalize
from .models import SiteCard, Story, audit, now


POST_MENU = (
    "📰 *Matérias do portal*\n\n"
    "1 · Nova matéria\n2 · Retirar do ar\n3 · Editar matéria\n4 · Últimas matérias\n"
    "5 · Buscar por título\n6 · Rascunhos\n\n*0* Menu principal"
)
NEW_POST = (
    "✍️ *Nova matéria*\n\nEnvie o título e o texto, separados por uma linha em branco. "
    "Se mandar só o título, eu peço o texto em seguida.\n\n*VOLTAR* retorna às matérias."
)


def require_editor(actor):
    if not can_publish(actor):
        raise EditorialError("Seu usuário não tem permissão editorial.")


def card_message(kind, data, text, actor, group, cover=None):
    key = "3EB0" + token_hex(9).upper()
    db.session.add(SiteCard(message_id=key, actor_id=actor, scope=group or "", kind=kind, data=data))
    return {"id": key, "text": text, **({"cover": cover} if cover else {})}


def find_card(message_id, actor, group):
    card = db.session.get(SiteCard, message_id) if message_id else None
    if card and card.scope == (group or "") and (group or card.actor_id == actor) and card.created >= now() - timedelta(days=7):
        return card
    return None


def text_html(text):
    return "".join(f"<p>{escape(line.strip())}</p>" for line in text.splitlines() if line.strip())


def plain(text):
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def preview_version(post):
    data = (post.id, post.title, post.excerpt, post.content_html, post.featured_image,
            post.image_credit, post.published_at, post.updated_at, sorted(c.id for c in post.categories))
    return sha256(repr(data).encode()).hexdigest()[:12].upper()


def old_version(post):
    return sha256("\0".join((str(post.id), post.title, post.excerpt or "", post.content_html or "")).encode()
                  ).hexdigest()[:10].upper()


def post_status(post):
    if not post.published_at:
        return "Rascunho"
    if post.published_at > now():
        return "Agendada"
    return "No ar"


def preview(post, actor, group):
    from .group_delivery import PUBLIC_URL, portal_cover

    version = preview_version(post)
    reference = {"post": post.id, "version": version}
    label = " · ".join(c.name for c in post.categories) or "Sem categoria"
    body = BeautifulSoup(post.content_html or "", "html.parser").get_text("\n\n", strip=True)[:9800]
    header = f"📰 *#{post.id} · {post_status(post)}*\n\n*{plain(post.title)}*\n{plain(post.excerpt)}\n\n"
    commands = ("Responda *PUBLICAR*, *BUSCAR FOTO* ou envie a foto nesta mensagem.\n"
                "*TÍTULO*, *TEXTO*, *RESUMO* e *CATEGORIA* aceitam a alteração na mesma resposta.\n"
                "Para programar, use *AGENDAR DD/MM/AAAA HH:MM*.")
    if post_status(post) == "No ar":
        commands = ("Responda *TÍTULO*, *TEXTO*, *RESUMO*, *CATEGORIA* ou *BUSCAR FOTO*. "
                    "Para retirar a publicação, responda *RETIRAR DO AR*.")
    cover = portal_cover(post)
    if post.featured_image and post.featured_image.startswith("/media/"):
        cover = PUBLIC_URL + post.featured_image
    messages = []
    if cover:
        messages.append({"text": post.image_credit or "Capa da matéria", "cover": cover})
    full = header + body
    if len(full) + len(commands) < 3100:
        commands = full + "\n\n" + commands
    else:
        messages.extend({"text": full[i:i+2800]} for i in range(0, len(full), 2800))
    messages.append(card_message("post", reference, f"{commands}\n\n_{label}_", actor, group))
    return {"messages": messages}


def get_post(identifier):
    return db.session.execute(select(Post).where(Post.id == identifier).with_for_update(of=Post)
                              ).unique().scalar_one_or_none()


def new_post(title, body, actor, group):
    require_editor(actor)
    title, body = title.strip(), body.strip()
    if not title or not body or len(title) > 500 or len(body) > 10000:
        raise EditorialError("Envie um título de até 500 caracteres e um texto de até 10 mil caracteres.")
    summary = re.split(r"(?<=[.!?])\s|\n", body, maxsplit=1)[0]
    summary = summary if len(summary) <= 240 else ""
    post = Post(source="local", title=escape(title), slug=_ensure_unique_slug(Post, title),
                excerpt=escape(summary), content_html=text_html(body), author_name="Redação Paraná Atual", updated_at=now())
    db.session.add(post)
    db.session.flush()
    audit("whatsapp_cms_rascunho", actor=actor, details=f"Matéria #{post.id}")
    return preview(post, actor, group)


def post_list(actor, group, *, query=None, drafts=False, limit=8):
    records = Post.query
    if query:
        records = records.filter(Post.title.ilike("%" + query.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
    if drafts:
        records = records.filter(Post.published_at.is_(None))
    posts = records.order_by(Post.id.desc()).limit(limit).all()
    if not posts:
        return "Nenhuma matéria encontrada. Digite *VOLTAR* para voltar às matérias."
    messages = [
        card_message("post", {"post": post.id, "version": preview_version(post)},
                     f"*#{post.id} · {post_status(post)}*\n{plain(post.title)}\nResponda *VER* para abrir.", actor, group)
        for post in posts
    ]
    return {"reply": "🔎 *Resultado da busca*" if query else "📰 *Matérias*", "messages": messages}


def publish_post(post, actor, group, scheduled=None):
    require_editor(actor)
    if not post.content_html:
        raise EditorialError("A matéria precisa de um texto.")
    if post.published_at and post.published_at <= now():
        return f"Essa matéria já está no ar.\n{public_url_for(post)}"
    from .wa_cover import generic_photo
    if (not post.featured_image or post.featured_image.startswith('/static/img/')
            or generic_photo({'url': post.featured_image, 'description': post.image_description or ''})):
        raise EditorialError("A foto da matéria ainda não está disponível. Busque ou envie a foto na prévia antes de publicar.")
    post.published_at, post.updated_at = scheduled or now(), now()
    audit("whatsapp_cms_agendada" if scheduled else "whatsapp_cms_publicada", actor=actor, details=f"Matéria #{post.id}")
    if scheduled:
        local = scheduled.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo"))
        return f"🗓️ *#{post.id} agendada* para {local:%d/%m/%Y às %H:%M}."
    return f"✅ *Matéria publicada*\n{plain(post.title)}\n{public_url_for(post)}"


def public_url_for(post):
    from .group_delivery import post_url
    return post_url(post)


def schedule_value(raw):
    try:
        local = datetime.strptime(raw, "%d/%m/%Y %H:%M").replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
    except ValueError as exc:
        raise EditorialError("Envie *AGENDAR DD/MM/AAAA HH:MM* no horário de Brasília.") from exc
    utc = local.astimezone(timezone.utc).replace(tzinfo=None)
    if not now() < utc < now() + timedelta(days=90):
        raise EditorialError("Escolha um horário futuro dentro dos próximos 90 dias.")
    return utc


def update_post(post, actor, group, action, value="", image=None):
    require_editor(actor)
    if action == "foto":
        from .wa_cover import media_from_editor
        data = media_from_editor(value, image)
        post.featured_image, post.image_credit, post.image_description = data["path"], data["credit"], data["description"]
    elif action == "categoria":
        if not value:
            return categories_text()
        categories = [] if value == "0" else Category.query.filter(
            Category.id.in_([int(v) for v in value.replace(",", " ").split() if v.isdecimal()])
        ).all()
        if not categories and value != "0":
            raise EditorialError("Responda *CATEGORIA* e os números. Use *CATEGORIA 0* para limpar.")
        post.categories = categories
    else:
        field, limit = {"titulo": ("title", 500), "resumo": ("excerpt", 2000), "texto": ("content_html", 10000)}[action]
        if not value or len(value) > limit:
            raise EditorialError(f"Envie o campo seguido de até {limit} caracteres.")
        setattr(post, field, text_html(value) if action == "texto" else escape(value))
    post.updated_at = now()
    audit("whatsapp_cms_editada", actor=actor, details=f"Matéria #{post.id} · {action}")
    return preview(post, actor, group)


def categories_text():
    categories = Category.query.order_by(Category.name).limit(60).all()
    lines = "\n".join(f"{c.id} · {c.name}" for c in categories)
    return f"🏷️ *Categorias*\n{lines}\n\nResponda na prévia com *CATEGORIA* e os números. Use *0* para limpar."


def by_card(card, text, actor, group, image=None):
    post = get_post(card.data["post"])
    if not post:
        return "A matéria saiu do acervo."
    command, _, value = text.strip().partition(" ")
    value = value.strip()
    action = normalize(command)
    plain_text = normalize(text)
    if action in {"ver", "editar"}:
        return preview(post, actor, group)
    if not image and action not in {"foto", "capa", "titulo", "texto", "resumo", "categoria", "publicar", "postar",
                                    "agendar", "retirar", "confirmar", "buscar"}:
        return None
    require_editor(actor)
    if card.data["version"] != preview_version(post):
        return {"reply": "A matéria mudou. Confira a prévia mais recente.", **preview(post, actor, group)}
    if plain_text in {"buscar foto", "buscar capa", "capa", "foto"} and not image:
        from .wa_cover import queue_search
        return queue_search(actor, group, {"post": post.id, "version": preview_version(post)}, plain(post.title))
    if action == "buscar" and normalize(value).startswith("foto "):
        from .wa_cover import queue_search
        return queue_search(actor, group, {"post": post.id, "version": preview_version(post)}, plain(post.title), "",
                            value.split(maxsplit=1)[1])
    if image or action in {"foto", "capa"}:
        return update_post(post, actor, group, "foto", f"|{text}" if image and text else value, image)
    if action in {"publicar", "postar", "agendar"}:
        return publish_post(post, actor, group, schedule_value(value) if action == "agendar" else None)
    if plain_text in {"retirar", "retirar do ar"}:
        data = {**card.data, "retire": True}
        return {"messages": [card_message("post", data, f"Retirar *{plain(post.title)}* do ar? Responda *CONFIRMAR*.",
                                          actor, group)]}
    if action == "confirmar" and card.data.get("retire"):
        post.published_at, post.updated_at = None, now()
        audit("whatsapp_cms_retirada", actor=actor, details=f"Matéria #{post.id}")
        return f"✅ A matéria *#{post.id}* voltou para rascunhos."
    if action in {"titulo", "texto", "resumo", "categoria"}:
        return update_post(post, actor, group, action, value)
    return None


def cms_command(text, actor, group):
    parts = text.split(maxsplit=2)
    action = normalize(parts[1]) if len(parts) > 1 else ""
    value = parts[2].strip() if len(parts) > 2 else ""
    if action == "nova" and value:
        title, sep, body = value.partition("|")
        return new_post(title, body, actor, group) if sep else NEW_POST
    if action == "listar":
        limit = int(value) if re.fullmatch(r"\d{1,2}", value) else 8
        return post_list(actor, group, limit=min(max(limit, 1), 10))
    if action == "buscar" and value:
        return post_list(actor, group, query=value[:180])
    identifier, _, body = value.partition(" ")
    if not re.fullmatch(r"\d{1,9}", identifier):
        return POST_MENU
    post = get_post(int(identifier))
    if not post:
        return "Matéria não encontrada."
    if action in {"ver", "editar", "publicar"}:
        return preview(post, actor, group)
    if action == "confirmar":
        if not all(c.isalnum() for c in body) or not hmac.compare_digest(body.upper(), old_version(post)):
            return "A versão mudou. Digite *CMS VER* e o ID para receber a prévia."
        return publish_post(post, actor, group)
    if action in {"titulo", "resumo", "texto", "foto", "categoria"}:
        return update_post(post, actor, group, action, body)
    return POST_MENU
