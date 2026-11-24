"""Operações da redação disponíveis diretamente no WhatsApp."""

from datetime import timedelta
from hashlib import sha256
from html import escape
import hmac
import re

from bs4 import BeautifulSoup
from sqlalchemy import select

from ..admin import _ensure_unique_slug, _save_setting, _setting
from ..models import AdSlot, PageView, Post, User, db
from .editorial import EditorialError, can_publish
from .geography import normalize
from .models import audit, now
from .wa_editorial import chat_session, open_radar, radar_command


GROUP_MENU = (
    "*Paraná Atual · Menu da redação*\n\n"
    "1 — Visão geral\n2 — Matérias do portal\n3 — Usuários\n4 — Insights\n"
    "5 — Configurações e SEO\n6 — Publicidade\n9 — Radar de Notícias\n\n"
    "Digite *9* ou *RADAR* para escolher quantas pautas quer receber. Digite *MENU* para voltar."
)
CMS_HELP = (
    "*Matérias do portal*\n"
    "CMS LISTAR 10 · CMS VER ID\n"
    "CMS NOVA título | texto · CMS TÍTULO ID novo título · CMS RESUMO ID resumo · CMS TEXTO ID novo texto\n"
    "CMS PUBLICAR ID mostra a prévia e o código para confirmar."
)
SEO_HELP = "*SEO*\nSEO VER · SEO DESCRIÇÃO nova descrição · SEO PALAVRAS novas palavras-chave"
ADS_HELP = "*Publicidade*\nPUBLICIDADE LISTAR · PUBLICIDADE PAUSAR código · PUBLICIDADE ATIVAR código"


def group_menu(text, actor, group, quote_id=None):
    text = text.strip()
    plain = normalize(text)
    session = chat_session(actor, group)
    if plain in {"menu", "/menu"}:
        session.mode = "menu"
        return GROUP_MENU
    if plain.startswith("cms"):
        return cms_command(text, actor)
    if plain.startswith("seo"):
        return seo_command(text, actor)
    if plain.startswith("publicidade"):
        return ads_command(text, actor)
    if plain == "usuarios":
        return users_status()
    if plain == "insights":
        return stats()
    if quote_id or plain.startswith("radar") or plain == "mais":
        return radar_command(text, actor, group, quote_id)
    if plain.isdecimal():
        if session.mode == "radar":
            return radar_command(text, actor, group)
        if session.mode != "menu":
            return None
        choices = {"1": stats, "2": lambda: CMS_HELP, "3": users_status,
                   "4": stats, "5": lambda: SEO_HELP, "6": lambda: ads_command("PUBLICIDADE LISTAR", actor)}
        if plain == "9":
            return open_radar(actor, group)
        action = choices.get(plain)
        return action() if action else GROUP_MENU
    if plain.split() and plain.split()[0] in {"postar", "confirmar", "ver", "fontes", "gerar", "apurar", "descartar", "apurado"}:
        return radar_command(text, actor, group)
    return None


def stats():
    count = Post.query.filter(Post.published_at <= now()).count()
    views = PageView.query.filter(PageView.created_at >= now() - timedelta(hours=24)).count()
    return f"*Paraná Atual*\n{count} matérias publicadas · {views} visualizações nas últimas 24h.\nDigite *RADAR STATUS* para ver a fila editorial."


def users_status():
    active = User.query.filter_by(is_admin=True, is_active=True)
    return f"{active.count()} administradores ativos; {active.filter(User.whatsapp.isnot(None)).count()} com WhatsApp cadastrado."


def split_command(text):
    pieces = text.split(maxsplit=2)
    return normalize(pieces[1]) if len(pieces) >= 2 else "", pieces[2] if len(pieces) == 3 else ""


def require_editor(actor):
    if not can_publish(actor):
        raise EditorialError("Este administrador não pode editar ou publicar matérias.")


def render_text(text):
    return "".join(f"<p>{escape(line.strip())}</p>" for line in text.splitlines() if line.strip())


def post_version(post):
    parts = (str(post.id), post.title, post.excerpt or "", post.content_html or "")
    return sha256("\0".join(parts).encode()).hexdigest()[:10].upper()


def post_preview(post):
    summary = BeautifulSoup(post.content_html or "", "html.parser").get_text(" ", strip=True)
    text = f"*{post.title}*\n{post.excerpt or ''}\n\n{summary}"
    return text


def cms_command(text, actor):
    action, argument = split_command(text)
    if action == "listar":
        if argument and (not argument.isdecimal() or len(argument) > 2 or int(argument) <= 0):
            return "Informe até 25 matérias. Exemplo: *CMS LISTAR 10*."
        posts = Post.query.order_by(Post.id.desc()).limit(min(25, int(argument) if argument else 10)).all()
        return "\n".join(f"#{p.id} · {'Publicada' if p.published_at else 'Rascunho'} · {p.title[:80]}" for p in posts) or "Nenhuma matéria."
    if action == "nova":
        require_editor(actor)
        title, sep, body = argument.partition("|")
        title, body = title.strip(), body.strip()
        if not sep or not title or not body or len(title) > 500 or len(body) > 10000:
            return "Envie *CMS NOVA título | texto* para criar um rascunho."
        post = Post(title=title, slug=_ensure_unique_slug(Post, title), content_html=render_text(body),
                    source="local", author_name="Redação Paraná Atual", updated_at=now())
        db.session.add(post)
        db.session.flush()
        audit("whatsapp_cms_rascunho", actor=actor, details=f"Matéria #{post.id}")
        return f"Rascunho *#{post.id}* criado. Digite *CMS PUBLICAR {post.id}* para revisar."
    if action not in {"ver", "titulo", "resumo", "texto", "publicar", "confirmar"}:
        return CMS_HELP
    field = argument.split(maxsplit=1)
    if not field or not re.fullmatch(r"[0-9]{1,9}", field[0]):
        return CMS_HELP
    post = db.session.execute(select(Post).where(Post.id == int(field[0])).with_for_update(of=Post)).unique().scalar_one_or_none()
    if not post:
        return "Matéria não encontrada."
    if action == "ver":
        preview = post_preview(post)
        text = preview[:11600]
        return {"reply": f"*Matéria #{post.id}*", "messages": [{"text": text[i:i+2900]} for i in range(0, len(text), 2900)]}
    require_editor(actor)
    if post.published_at or post.source == "radar":
        return "A edição por este comando é para rascunhos do portal. Para pautas do Radar, responda na prévia do Radar."
    if action == "publicar":
        if not post.content_html:
            return "O rascunho precisa de um texto para publicar."
        preview = post_preview(post)
        messages = [{"text": preview[i:i+2900]} for i in range(0, len(preview), 2900)]
        messages.append({"text": f"Após revisar, digite *CMS CONFIRMAR {post.id} {post_version(post)}* para publicar."})
        return {"messages": messages}
    if action == "confirmar":
        if len(field) != 2 or not hmac.compare_digest(field[1].upper(), post_version(post)):
            return "O código não corresponde à versão atual. Digite *CMS PUBLICAR* seguido do ID."
        post.published_at, post.updated_at = now(), now()
        audit("whatsapp_cms_publicada", actor=actor, details=f"Matéria #{post.id}")
        from .group_delivery import post_url
        return f"✅ *Matéria #{post.id} publicada*\n{post.title}\n{post_url(post)}"
    attr, limit = {"titulo": ("title", 500), "resumo": ("excerpt", 2000), "texto": ("content_html", 10000)}[action]
    value = field[1].strip() if len(field) == 2 else ""
    if not value or len(value) > limit:
        return f"Envie de 1 a {limit} caracteres."
    setattr(post, attr, render_text(value) if action == "texto" else value)
    post.updated_at = now()
    audit("whatsapp_cms_editada", actor=actor, details=f"Matéria #{post.id} · {action}")
    return f"Rascunho *#{post.id}* atualizado."


def seo_command(text, actor):
    action, value = split_command(text)
    if action == "ver":
        return f"Descrição atual\n{_setting('default_meta_description', '')}\n\nPalavras-chave\n{_setting('site_keywords', '')}"
    if action not in {"descricao", "palavras"}:
        return SEO_HELP
    if not value or len(value) > 350:
        return "Envie um texto de até 350 caracteres."
    key = "default_meta_description" if action == "descricao" else "site_keywords"
    _save_setting(key, value)
    audit("whatsapp_seo_atualizado", actor=actor, details=key)
    return "Configuração de SEO salva."


def ads_command(text, actor):
    action, key = split_command(text)
    if action == "listar":
        parts = [f"{slot.key} · {slot.name} · {'Ativo' if slot.is_active else 'Pausado'}"
                 for slot in AdSlot.query.order_by(AdSlot.id).all()]
        return "\n".join(parts) + "\n\n" + ADS_HELP if parts else "Nenhum espaço de publicidade cadastrado."
    if action not in {"pausar", "ativar"} or not key:
        return ADS_HELP
    slot = AdSlot.query.filter_by(key=key.strip().lower()).first()
    if not slot:
        return "Espaço não encontrado. Digite *PUBLICIDADE LISTAR*."
    slot.is_active = action == "ativar"
    audit("whatsapp_publicidade", actor=actor, details=f"{slot.key} · {action}")
    return f"{slot.name} {'ativado' if slot.is_active else 'pausado'}."

