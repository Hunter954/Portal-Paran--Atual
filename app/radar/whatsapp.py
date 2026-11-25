import hashlib
import hmac
import os
import re
from datetime import timedelta

import requests
from flask import Blueprint, abort, jsonify, request
from sqlalchemy.exc import IntegrityError

from ..models import User, db
from .editorial import EditorialError
from .models import Alert, ChatCard, InboundMessage, Story, SiteChat, MANUAL_REGION, WhatsAppPreference, audit, now


bp = Blueprint("radar_gateway", __name__, url_prefix="/internal/radar/whatsapp")


def enabled():
    return bool(os.getenv("RADAR_WHATSAPP_URL") and os.getenv("RADAR_WHATSAPP_TOKEN"))


def phone_number(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) in {10, 11}:
        digits = "55" + digits
    return "+" + digits if re.fullmatch(r"[1-9][0-9]{7,14}", digits) else None


def gateway(method, path, *, timeout=(3, 7), **kwargs):
    if not enabled():
        raise EditorialError("Gateway do Radar ainda sem configuração.")
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, os.environ["RADAR_WHATSAPP_URL"].rstrip("/") + path,
                                       headers={"Authorization": "Bearer " + os.environ["RADAR_WHATSAPP_TOKEN"]},
                                       timeout=timeout, allow_redirects=False, **kwargs)
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
        story = Story.query.filter(Story.region != MANUAL_REGION,
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
                f"\n*1* Preparar prévia\n*2* Ver fontes\n*0* Menu do Radar")
        message_key = "3EB0" + hashlib.sha256(f"parana-atual:radar:{story.id}:{user.id}".encode()).hexdigest()[:18].upper()
        alert = Alert(story_id=story.id, user_id=user.id, state="sending")
        db.session.add(alert)
        if not db.session.get(ChatCard, message_key):
            db.session.add(ChatCard(message_id=message_key, story_id=story.id, actor_id=user.id, scope=""))
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

def authorized_request():
    secret = os.getenv("RADAR_WHATSAPP_TOKEN")
    received = request.headers.get("Authorization", "")
    return bool(secret and hmac.compare_digest(received.encode(), ("Bearer " + secret).encode()))


@bp.post("")
def receive():
    if not authorized_request():
        abort(401)
    if request.content_length and request.content_length > 2_740_000:
        abort(413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    phone, event_id, message = (data.get(key) for key in ("phone", "id", "text"))
    if (not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone)
            or not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,90}", event_id)
            or not isinstance(message, str) or len(message) > 11500):
        abort(400)
    group_id = data.get("group_id")
    quote_id = data.get("quote_id")
    image = data.get("image")
    if image is not None and (not isinstance(image, dict) or image.get("mime") not in {"image/jpeg", "image/png", "image/webp"}
                              or not isinstance(image.get("data"), str) or len(image["data"]) > 2_666_668):
        abort(400)
    if image is None and request.content_length and request.content_length > 50000:
        abort(413)
    if quote_id is not None and (not isinstance(quote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,90}", quote_id)):
        abort(400)
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
    if len(users) != 1:
        return jsonify(reply=None), 403
    user = users[0]
    if data.get("image_offer") or image:
        from .wa_console import accepts_image
        allowed = accepts_image(user.id, group_id, quote_id)
        if data.get("image_offer"):
            return jsonify(need_image=allowed)
        if not allowed:
            return jsonify(reply=None)
    db.session.add(InboundMessage(event_key=event_id, user_id=user.id))
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify(reply=None)
    try:
        from .wa_console import group_menu
        reply = group_menu(message, user.id, group_id, quote_id, image)
    except EditorialError as exc:
        reply = str(exc)
        state=db.session.get(SiteChat,(user.id,group_id or ''))
        if state and state.data.get('radar_ui'):
            from .wa_radar_menu import current_options
            reply+='\n\n'+current_options(state)
    db.session.commit()
    return jsonify(reply) if isinstance(reply, dict) else jsonify(reply=reply)
