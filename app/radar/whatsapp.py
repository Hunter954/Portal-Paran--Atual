import hashlib
import hmac
import os
import re
from datetime import timedelta

import requests
from flask import Blueprint, abort, jsonify, request
from sqlalchemy.exc import IntegrityError

from ..models import Post, User, db
from .editorial import EditorialError, lock_story, queue_draft
from .geography import normalize
from .models import Alert, InboundMessage, Story, WhatsAppPreference, audit, now


bp = Blueprint("radar_gateway", __name__, url_prefix="/internal/radar/whatsapp")


def enabled():
    return bool(os.getenv("RADAR_WHATSAPP_URL") and os.getenv("RADAR_WHATSAPP_TOKEN"))


def phone_number(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) in {10, 11}:
        digits = "55" + digits
    return "+" + digits if re.fullmatch(r"[1-9][0-9]{7,14}", digits) else None


def gateway(method, path, **kwargs):
    if not enabled():
        raise EditorialError("Gateway do Radar ainda sem configuração.")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, os.environ["RADAR_WHATSAPP_URL"].rstrip("/") + path,
                                       headers={"Authorization": "Bearer " + os.environ["RADAR_WHATSAPP_TOKEN"]},
                                       timeout=(3, 7), allow_redirects=False, **kwargs)
            if response.status_code != 200:
                if response.status_code in {400, 409}:
                    message = response.json().get("message")
                    if isinstance(message, str):
                        error = EditorialError(message[:160])
                        error.status_code = response.status_code
                        raise error
                raise EditorialError(f"Gateway retornou HTTP {response.status_code}.")
            return response.json()
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, EditorialError):
            raise
        raise EditorialError("O gateway não confirmou a operação.") from exc


def status():
    if not enabled():
        return "Aguardando configuração da integração"
    try:
        return "Conectado" if gateway("GET", "/radar/status").get("connected") else "Desconectado"
    except EditorialError as exc:
        return str(exc)


def send_pending_alert():
    if not enabled():
        return
    admin_preferences = db.session.query(WhatsAppPreference, User).join(
        User, User.id == WhatsAppPreference.user_id
    ).filter(WhatsAppPreference.enabled.is_(True), User.is_admin.is_(True), User.is_active.is_(True))
    for preference, user in admin_preferences.order_by(WhatsAppPreference.user_id):
        phone = phone_number(user.whatsapp)
        if not phone or phone != preference.phone:
            continue
        if Alert.query.filter(Alert.user_id == user.id, Alert.created > now() - timedelta(hours=1)).count() >= 5:
            continue
        story = Story.query.filter(
            Story.created >= max(preference.subscribed, now() - timedelta(hours=12)),
            Story.score >= preference.minimum,
            Story.state.in_(("new", "research", "production", "review")),
            ~db.session.query(Alert.id).filter(Alert.story_id == Story.id, Alert.user_id == user.id).exists(),
        ).order_by(Story.score.desc(), Story.id).first()
        if not story:
            continue
        first = story.articles[0]
        text = (f"RADAR PARANÁ ATUAL · #{story.id}\n"
                f"{story.title}\n{story.location} · Relevância {story.score}/100\n"
                f"{first.summary[:350]}\n{first.url}\n"
                f"Responda RADAR FONTES {story.id} ou RADAR AJUDA.")
        message_key = "3EB0" + hashlib.sha256(f"parana-atual:radar:{story.id}:{user.id}".encode()).hexdigest()[:18].upper()
        alert = Alert(story_id=story.id, user_id=user.id, state="sending")
        db.session.add(alert)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return
        alert_id = alert.id
        try:
            result = gateway("POST", "/radar/send", json={"phone": phone, "text": text, "id": message_key})
            if result.get("accepted") is not True:
                raise EditorialError("O gateway não aceitou o alerta.")
            state, message = "sent", "Aceito pelo gateway"
        except EditorialError as exc:
            state, message = "error", str(exc)
        alert = db.session.get(Alert, alert_id)
        alert.state, alert.message, alert.finished = state, message[:160], now()
        db.session.commit()
        return


GROUP_ID = re.compile(r"\d{5,30}@g\.us")

COMMANDS = ("RADAR URGENTES · RADAR PENDENTES · RADAR STATUS · RADAR FONTES ID · "
            "RADAR RESUMO ID · RADAR APROVAR ID · RADAR GERAR ID · RADAR DESCARTAR ID · RADAR POSTAR ID")


@bp.post("")
def receive():
    secret = os.getenv("RADAR_WHATSAPP_TOKEN")
    received = request.headers.get("Authorization", "")
    if not secret or not hmac.compare_digest(received.encode(), ("Bearer " + secret).encode()):
        abort(401)
    if request.content_length and request.content_length > 2048:
        abort(413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    phone, event_id, message = (data.get(key) for key in ("phone", "id", "text"))
    if (not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone)
            or not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,90}", event_id)
            or not isinstance(message, str) or len(message) > 160):
        abort(400)
    group_id = data.get("group_id")
    if group_id is not None:
        if not isinstance(group_id, str) or not GROUP_ID.fullmatch(group_id):
            abort(400)
        try:
            config = gateway("GET", "/radar/group/settings")
        except EditorialError:
            return jsonify(reply=None), 503
        if not config.get("enabled") or not config.get("available") or config.get("group_id") != group_id:
            return jsonify(reply=None), 403
        users = [user for user in User.query.filter_by(is_admin=True, is_active=True)
                 .filter(User.whatsapp.isnot(None)) if phone_number(user.whatsapp) == phone]
    else:
        user = db.session.query(User).join(WhatsAppPreference, User.id == WhatsAppPreference.user_id).filter(
            User.is_admin.is_(True), User.is_active.is_(True), WhatsAppPreference.phone == phone,
            WhatsAppPreference.enabled.is_(True)).first()
        users = [user] if user and phone_number(user.whatsapp) == phone else []
    if len(users) != 1:
        return jsonify(reply=None), 403
    user = users[0]
    db.session.add(InboundMessage(event_key=event_id, user_id=user.id))
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify(reply=None)
    try:
        text = normalize(message).strip()
        reply = group_menu(text, user.id) if group_id is not None else command(text, user.id)
    except EditorialError as exc:
        reply = str(exc)
    db.session.commit()
    return jsonify(reply=reply)


GROUP_MENU = (
    "*Paraná Atual · Menu da redação*\n\n"
    "1 — Visão geral\n2 — Matérias\n3 — Usuários\n4 — Insights\n"
    "5 — Configurações e SEO\n6 — Publicidade e prints\n7 — Arte para redes sociais\n"
    "8 — Vídeo vertical\n9 — Radar de Notícias\n\n"
    "Digite uma opção ou RADAR AJUDA."
)


def group_menu(text, actor):
    portal = "https://paranaatual.com.br"
    if text in {"menu", "/menu"}:
        return GROUP_MENU
    if text == "1":
        posts = Post.query.filter(Post.published_at <= now()).count()
        new = Story.query.filter_by(state="new").count()
        return f"{posts} matérias publicadas · {new} pautas para analisar\nPainel: {portal}/admin/"
    if text == "2":
        from .group_delivery import post_url

        posts = Post.query.filter(Post.published_at <= now()).order_by(Post.published_at.desc()).limit(4)
        latest = "\n\n".join(f"{post.title}\n{post_url(post)}" for post in posts) or "Nenhuma matéria publicada."
        return latest + f"\n\nCriar matéria: {portal}/admin/posts/new"
    if text == "9":
        pending = command("radar pendentes", actor)
        return f"{pending}\n\n{COMMANDS}\n\nPainel: {portal}/admin/radar/"
    links = {
        "3": f"Usuários: {portal}/admin/users",
        "4": f"Insights: {portal}/admin/insights",
        "5": f"Configurações e SEO: {portal}/admin/settings",
        "6": f"Publicidade: {portal}/admin/ads\nPrint automático: {portal}/admin/prints",
        "7": f"Estúdio de mídia: {portal}/admin/estudio?formato=feed",
        "8": f"Estúdio de vídeo: {portal}/admin/estudio?formato=video",
    }
    if text in links:
        return links[text]
    return command(text, actor)


def command(text, actor):
    match = re.fullmatch(r"\s*radar\s+([a-z]+)(?:\s+(?:#?pa-)?([0-9]{1,9}))?\s*", text)
    if not match:
        return COMMANDS
    action, number = match.groups()
    if action == "ajuda":
        return COMMANDS
    if action == "status":
        return "Radar ativo. " + ("Publicação editorial disponível no painel." if os.getenv(
            "RADAR_ENABLE_PUBLISHING") == "1" else "Publicação pelo Radar desativada.")
    if action in {"pendentes", "urgentes"}:
        entries = Story.query.filter(Story.state.in_(("new", "research", "production", "review")))
        if action == "urgentes":
            entries = entries.filter(Story.score >= 75)
        return "\n".join(f"#{s.id} · {s.title}" for s in entries.order_by(
            Story.score.desc(), Story.id.desc()).limit(5)) or "Nenhuma pauta nesta fila."
    if not number or action not in {"resumo", "fontes", "aprovar", "gerar", "descartar", "postar"}:
        return COMMANDS
    story = lock_story(int(number))
    if not story:
        return "Pauta não encontrada."
    if action == "fontes":
        return "\n".join(f"{a.source.name}: {a.url}" for a in story.articles[:5]) or "Sem fontes."
    if action == "resumo":
        return f"{story.title}\n{story.location}\n{story.articles[0].summary}"
    if action == "postar":
        return ("Abra a pauta no painel para revisar e confirmar a publicação da versão atual.\n"
                f"https://paranaatual.com.br/admin/radar/pautas/{story.id}")
    if story.state in {"published", "discarded", "scheduled"}:
        raise EditorialError("Esta pauta não permite a ação solicitada.")
    if action == "gerar":
        queue_draft(story, actor)
    else:
        story.previous_state = story.state
        story.state = "production" if action == "aprovar" else "discarded"
    story.updated = now()
    audit("whatsapp_" + action, actor=actor, story=story.id)
    return f"Ação registrada na pauta #{story.id}."

