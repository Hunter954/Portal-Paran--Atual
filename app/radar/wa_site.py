import json
import re
from datetime import timedelta
from secrets import token_urlsafe

from sqlalchemy import func, select

from ..admin import (AD_SIDEBAR_KEYS, _ad_sidebar_order, _ensure_unique_slug, _media_root, _save_setting,
                     _selected_top_menu_category_ids, _setting, _slot_visual_payload)
from ..models import AdClick, AdSlot, AnalyticsSession, Category, PageView, Post, User, db
from .editorial import EditorialError
from .fetch import FetchError, public_url
from .geography import normalize
from .models import audit, now
from .whatsapp import phone_number


SETTINGS_MENU = (
    "⚙️ *Configurações do portal*\n\n1 · Ver configurações\n2 · Alterar título\n"
    "3 · Alterar descrição SEO\n4 · Alterar palavras-chave\n5 · Categorias e menu da home\n"
    "6 · Redes sociais e contato\n7 · Logo e imagem padrão\n\n*0* Menu principal"
)
ADS_MENU = (
    "📣 *Publicidade*\n\n1 · Ver espaços e banners\n2 · Cadastrar banner\n"
    "3 · Ativar ou pausar espaço\n4 · Relatório de cliques\n5 · Ordem na lateral\n"
    "6 · Remover banner\n\n*0* Menu principal"
)
USERS_MENU = (
    "👥 *Usuários e acessos*\n\n1 · Listar usuários\n2 · Cadastrar usuário\n"
    "3 · Ativar ou pausar usuário\n4 · Alterar WhatsApp\n5 · Permissão de administrador\n\n*0* Menu principal"
)
CATEGORY_MENU = (
    "🏷️ *Categorias e home*\n\n1 · Listar\n2 · Criar\n3 · Renomear\n"
    "4 · Escolher categorias do menu do site\n5 · Excluir categoria vazia\n\n*0* Menu principal"
)
MEDIA_MENU = (
    "🖼️ *Mídia e capas*\n\n1 · Imagens da biblioteca\n2 · Enviar imagem para a biblioteca\n"
    "3 · Logo e capa padrão\n4 · Buscar imagens por assunto\n\n"
    "Para a capa de uma matéria, abra a prévia e envie a foto ou responda *BUSCAR FOTO*.\n*0* Menu principal"
)


def audited_setting(key, value, actor):
    _save_setting(key, value)
    audit("whatsapp_site_configurado", actor=actor, details=key)
    return "✅ Configuração salva."


def overview():
    total = Post.query.count()
    public = Post.query.filter(Post.published_at <= now()).count()
    return (f"📊 *Visão geral*\n\n📰 {public} matérias no ar · {total} no acervo\n"
            f"✍️ {Post.query.filter_by(published_at=None).count()} rascunhos\n"
            f"🏷️ {Category.query.count()} categorias\n📣 {AdSlot.query.filter_by(is_active=True).count()} espaços ativos\n"
            f"👥 {User.query.filter_by(is_active=True).count()} usuários ativos\n\n*0* Menu principal")


def insights():
    since = now() - timedelta(days=30)
    views = PageView.query.filter(PageView.created_at >= since).count()
    sessions, visitors, average = db.session.query(
        func.count(AnalyticsSession.id), func.count(func.distinct(AnalyticsSession.visitor_id)),
        func.avg(AnalyticsSession.duration_seconds),
    ).filter(AnalyticsSession.created_at >= since).one()
    top = (db.session.query(Post.title, func.count(PageView.id)).join(PageView, PageView.post_id == Post.id)
           .filter(PageView.created_at >= since, Post.published_at <= now())
           .group_by(Post.id, Post.title).order_by(func.count(PageView.id).desc()).limit(5))
    lines = "\n".join(f"{title[:90]} · {count}" for title, count in top)
    return (f"📈 *Últimos 30 dias*\n\n{views} visualizações · {visitors} visitantes · {sessions} sessões\n"
            f"Duração média {round(float(average or 0))} s\n\n*Mais lidas*\n{lines or 'Sem dados no período.'}\n\n*0* Menu principal")


def category_action(action, value, actor):
    categories = Category.query.order_by(Category.name).limit(60).all()
    if action == "list":
        menu = set(_selected_top_menu_category_ids())
        lines = "\n".join(f"{c.id} · {c.name}" + (" · no menu" if c.id in menu else "") for c in categories)
        return "🏷️ *Categorias*\n\n" + (lines or "Nenhuma categoria.") + "\n\n*VOLTAR*"
    if action == "new":
        if not 1 <= len(value) <= 180:
            raise EditorialError("Envie o nome da categoria.")
        if Category.query.filter(func.lower(Category.name) == value.lower()).first():
            raise EditorialError("Já existe uma categoria com esse nome.")
        category = Category(name=value, slug=_ensure_unique_slug(Category, value))
        db.session.add(category)
        db.session.flush()
        audit("whatsapp_categoria_criada", actor=actor, details=f"#{category.id}")
        return f"✅ Categoria *{category.name}* criada · #{category.id}."
    if action == "home":
        values = value.replace(",", " ").split()
        ids = [] if values == ["0"] else list(dict.fromkeys(int(v) for v in values if re.fullmatch(r"\d{1,9}", v)))
        if values != ["0"] and (not ids or len(ids) > 10 or set(ids) - {c.id for c in categories}):
            raise EditorialError("Informe até dez números de categorias na ordem desejada, ou *0* para limpar.")
        return audited_setting("top_menu_category_ids", json.dumps(ids), actor)
    id_text, _, name = value.partition("|")
    identifier = id_text.strip()
    category = db.session.get(Category, int(identifier)) if re.fullmatch(r"\d{1,9}", identifier) else None
    if not category:
        raise EditorialError("Categoria não encontrada. Consulte a lista.")
    if action == "rename":
        name = name.strip()
        if not 1 <= len(name) <= 180:
            raise EditorialError("Envie *número | novo nome*.")
        category.name = name
    else:
        if Post.query.filter(Post.categories.any(Category.id == category.id)).first():
            raise EditorialError("A categoria ainda está vinculada a matérias.")
        if category.id in _selected_top_menu_category_ids():
            raise EditorialError("Retire a categoria do menu da home antes de excluir.")
        db.session.delete(category)
    audit("whatsapp_categoria_" + action, actor=actor, details=f"#{category.id}")
    return "✅ Categoria atualizada."


def users_list():
    users = User.query.order_by(User.is_admin.desc(), User.id).limit(30).all()
    if not users:
        return "Nenhum usuário."
    lines = "\n".join(f"{u.id} · {u.email} · {'ativo' if u.is_active else 'pausado'}"
                      + (" · administrador" if u.is_admin else "") for u in users)
    return f"👥 *Usuários*\n\n{lines}\n\n*VOLTAR*"


def user_action(action, value, actor):
    fields = [part.strip() for part in value.split("|")]
    if action == "new":
        email = fields[0].lower()
        phone = phone_number(fields[1]) if len(fields) == 2 else None
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 185 or not phone:
            raise EditorialError("Envie *e-mail | WhatsApp* com DDD.")
        if User.query.filter((User.email == email) | (User.whatsapp == phone)).first():
            raise EditorialError("Já existe um usuário com esse e-mail ou WhatsApp.")
        user = User(email=email, whatsapp=phone, is_admin=False, is_active=True)
        user.set_password(token_urlsafe(28))
        db.session.add(user)
        db.session.flush()
        audit("whatsapp_usuario_criado", actor=actor, details=f"#{user.id}")
        return "✅ Usuário cadastrado. Ele pode criar a senha pelo recurso de recuperação com o WhatsApp cadastrado."
    user = db.session.execute(select(User).where(User.id == int(fields[0])).with_for_update()
                              ).scalar_one_or_none() if re.fullmatch(r"\d{1,9}", fields[0]) else None
    if not user:
        raise EditorialError("Usuário não encontrado.")
    if user.id == actor and action in {"active", "admin"}:
        raise EditorialError("Seu próprio acesso de administrador precisa permanecer ativo.")
    if action == "phone":
        phone = phone_number(fields[1]) if len(fields) == 2 else None
        if not phone or User.query.filter(User.whatsapp == phone, User.id != user.id).first():
            raise EditorialError("Informe um WhatsApp válido e exclusivo. Use *ID | WhatsApp*.")
        user.whatsapp = phone
    else:
        if len(fields) != 2 or normalize(fields[1]) not in {"sim", "nao"}:
            raise EditorialError("Envie *ID | SIM* ou *ID | NÃO*.")
        setattr(user, "is_admin" if action == "admin" else "is_active", normalize(fields[1]) == "sim")
    audit("whatsapp_usuario_" + action, actor=actor, details=f"#{user.id}")
    return f"✅ Acesso do usuário *#{user.id}* atualizado."


def settings_overview():
    return (f"⚙️ *Configurações*\n\nNome · {_setting('site_name', 'Paraná Atual')}\n"
            f"Descrição · {_setting('default_meta_description') or 'Vazia'}\n"
            f"Palavras-chave · {_setting('site_keywords') or 'Vazias'}\n"
            f"Instagram · {_setting('instagram_url') or 'Vazio'}\n\n*VOLTAR*")


def site_value(action, value, actor):
    fields = {
        "titulo": ("site_name", 160), "descricao": ("default_meta_description", 350),
        "palavras": ("site_keywords", 350), "instagram": ("instagram_url", 500),
        "facebook": ("facebook_url", 500), "youtube": ("youtube_url", 500), "x": ("x_url", 500),
        "contato": ("contact_email", 190), "telefone": ("contact_phone", 80),
        "logo": ("logo_url", 500), "capa": ("default_share_image", 500), "favicon": ("favicon_url", 500),
    }
    if action not in fields:
        return SETTINGS_MENU
    key, limit = fields[action]
    if not value or len(value) > limit:
        raise EditorialError(f"Envie um valor de até {limit} caracteres.")
    if key.endswith("_url") or key == "default_share_image":
        value = external_url(value)
    return audited_setting(key, value, actor)


def external_url(value):
    try:
        return public_url(value)
    except FetchError as exc:
        raise EditorialError("Informe uma URL pública válida.") from exc


def ads_action(action, value, actor):
    slots = AdSlot.query.order_by(AdSlot.key).limit(30).all()
    if action in {"list", "report"}:
        if action == "report":
            counts = dict(db.session.query(AdClick.slot_key, func.count(AdClick.id))
                          .filter(AdClick.created_at >= now() - timedelta(days=30)).group_by(AdClick.slot_key))
            return "📣 *Cliques em 30 dias*\n\n" + "\n".join(f"{s.name} · {counts.get(s.key, 0)}" for s in slots)
        return "📣 *Espaços de publicidade*\n\n" + "\n".join(
            f"{s.key} · {s.name} · {'ativo' if s.is_active else 'pausado'}"
            + "".join(f"\n  {i} · {banner.get('title') or 'Banner'}"
                      for i, banner in enumerate(_slot_visual_payload(s)["banners"], 1))
            for s in slots
        )
    if action == "order":
        order = value.replace(",", " ").split()
        if len(order) != len(AD_SIDEBAR_KEYS) or set(order) != set(AD_SIDEBAR_KEYS):
            raise EditorialError("Informe os três espaços em ordem. Atual: " + " ".join(_ad_sidebar_order()))
        return audited_setting("ad_sidebar_order", json.dumps(order), actor)
    values = [part.strip() for part in value.split("|")]
    slot = AdSlot.query.filter_by(key=values[0].lower()).with_for_update().first()
    if not slot:
        raise EditorialError("Espaço de publicidade não encontrado.")
    payload = _slot_visual_payload(slot)
    if action == "add":
        if len(values) not in {3, 4} or not 1 <= len(values[1]) <= 180 or len(payload["banners"]) >= 12:
            raise EditorialError("Envie *espaço | nome | URL da imagem | URL de destino*.")
        payload["banners"].append({"title": values[1], "image": external_url(values[2]),
                                   "link": external_url(values[3]) if len(values) == 4 and values[3] else ""})
        slot.is_active = True
    elif action == "remove":
        if len(values) != 2 or not re.fullmatch(r"\d{1,2}", values[1]) or not 1 <= int(values[1]) <= len(payload["banners"]):
            raise EditorialError("Envie *espaço | número do banner* conforme a lista.")
        payload["banners"].pop(int(values[1]) - 1)
        if not payload["banners"]:
            slot.is_active = False
    else:
        if len(values) != 2 or normalize(values[1]) not in {"ativar", "pausar"}:
            raise EditorialError("Envie *espaço | ATIVAR* ou *espaço | PAUSAR*.")
        slot.is_active = normalize(values[1]) == "ativar"
    if action in {"add", "remove"}:
        slot.html = "__ADCFG__" + json.dumps(payload, ensure_ascii=False)
    audit("whatsapp_publicidade_" + action, actor=actor, details=slot.key)
    return f"✅ Publicidade atualizada em *{slot.name}*."


def library():
    root = _media_root()
    results = []
    for folder in ("posts", "general", "studio"):
        for path in (root / folder).glob("*"):
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                results.append((path.stat().st_mtime, path.relative_to(root).as_posix()))
    lines = "\n".join(f"• {path}" for _, path in sorted(results, reverse=True)[:12])
    return f"🖼️ *Imagens da biblioteca*\n\n{lines or 'Nenhuma imagem enviada.'}"
