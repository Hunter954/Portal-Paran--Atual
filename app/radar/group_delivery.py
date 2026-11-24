from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError

from ..models import Post, db
from .editorial import EditorialError
from .models import GroupDelivery, Story, now
from .whatsapp import enabled, gateway


PUBLIC_URL = "https://paranaatual.com.br"


def group_settings():
    return gateway("GET", "/radar/group/settings")


def post_url(post):
    return f"{PUBLIC_URL}/p/{quote(post.slug, safe='')}"


def portal_cover(post):
    image = post.featured_image or ""
    try:
        parts = urlsplit(image)
    except ValueError:
        return None
    if ((not parts.netloc and not parts.scheme) or
        (parts.scheme == "https" and parts.netloc in {"paranaatual.com.br", "www.paranaatual.com.br"})):
        if parts.path.startswith("/media/") and not parts.query and not parts.fragment:
            return PUBLIC_URL + parts.path
    return None


def eligible_since(value):
    return max(now() - timedelta(hours=12),
               datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None))


def next_subject(config, kind):
    if kind == "article":
        table, field, start = Post, Post.published_at, config["news_since"]
        query = Post.query.filter(Post.source.in_(("local", "radar")))
    else:
        table, field, start = Story, Story.created, config["alerts_since"]
        query = Story.query.filter(Story.score >= config["minimum"],
                                   Story.state.in_(("new", "research", "production", "review")))
    if not start:
        return None
    sent = db.session.query(GroupDelivery.id).filter_by(group_id=config["group_id"], kind=kind).filter(
        GroupDelivery.subject_id == table.id).exists()
    return query.filter(field >= eligible_since(start), field <= now(), ~sent).order_by(field, table.id).first()


def message_for(subject, kind):
    if kind == "alert":
        first = subject.articles[0]
        return (f"🚨 *Radar Paraná Atual · #{subject.id}*\n{subject.title}\n"
                f"{subject.location} · Relevância {subject.score}/100\n"
                f"{first.summary[:350]}\n{first.url}\n"
                f"Para consultar as fontes: RADAR FONTES {subject.id}")
    summary = BeautifulSoup(subject.excerpt or subject.content_html or "", "html.parser").get_text(" ", strip=True)
    return (f"📰 *Nova matéria no Paraná Atual*\n\n{subject.title}\n\n"
            f"{summary[:400]}\n{post_url(subject)}")


def send_pending_group(*, include_alerts=True):
    if not enabled():
        return
    try:
        config = group_settings()
    except EditorialError:
        return
    if not config.get("enabled") or not config.get("connected") or not config.get("available"):
        return
    gid = config["group_id"]
    recent = GroupDelivery.query.filter_by(group_id=gid).filter(
        GroupDelivery.created >= now() - timedelta(minutes=1)).count()
    if recent >= 5:
        return
    for flag, kind in (("news", "article"), ("alerts", "alert")):
        if not config.get(flag) or (kind == "alert" and not include_alerts):
            continue
        subject = next_subject(config, kind)
        if subject:
            dispatch(subject, config, kind)
            return


def dispatch(subject, config, kind):
    gid = config["group_id"]
    body = {"group_id": gid, "revision": config["revision"], "kind": kind, "text": message_for(subject, kind),
            "id": "3EB0" + sha256(f"parana-atual:{kind}:{gid}:{subject.id}".encode()).hexdigest()[:18].upper()}
    if kind == "article":
        body["cover"] = portal_cover(subject)
    delivery = GroupDelivery(group_id=gid, kind=kind, subject_id=subject.id, state="sending")
    db.session.add(delivery)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return
    try:
        result = gateway("POST", "/radar/group/send", json=body)
        if result.get("accepted") is not True:
            raise EditorialError("O gateway não confirmou o envio.")
        delivery.state, delivery.message = "sent", "Aceito pelo gateway"
    except EditorialError as exc:
        if getattr(exc, "status_code", None) == 409:
            db.session.delete(delivery)
            db.session.commit()
            return
        delivery.state, delivery.message = "error", str(exc)[:160]
    delivery.finished = now()
    db.session.commit()

