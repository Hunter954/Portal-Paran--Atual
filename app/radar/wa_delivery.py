"""Entrega o resultado de geração editorial na conversa que solicitou a pauta."""

from datetime import timedelta
from secrets import token_hex

from ..models import User, db
from .editorial import EditorialError, latest_draft
from .group_delivery import group_settings
from .models import ChatDraftReply, Job, Story, now
from .wa_editorial import draft_cards
from .whatsapp import enabled, gateway, phone_number


def send_pending_draft_reply():
    if not enabled():
        return
    item = ChatDraftReply.query.join(Job, Job.id == ChatDraftReply.job_id).filter(
        ChatDraftReply.state == "pending", Job.state.in_(("done", "error"))
    ).order_by(ChatDraftReply.id).first()
    if not item:
        return
    user = db.session.get(User, item.actor_id)
    phone = phone_number(user.whatsapp) if user and user.is_active and user.is_admin else None
    if not phone or item.created < now() - timedelta(hours=12):
        item.state = "cancelled"
        db.session.commit()
        return
    config = None
    if item.scope:
        try:
            config = group_settings()
        except EditorialError:
            return
        if (not config.get("enabled") or not config.get("available") or
                config.get("group_id") != item.scope):
            item.state = "cancelled"
            db.session.commit()
            return
        if not config.get("connected"):
            return
    job = db.session.get(Job, item.job_id)
    story = db.session.get(Story, job.story_id)
    draft = latest_draft(story.id)
    if job.state == "done" and draft and story.state == "review":
        messages = draft_cards(story, draft, item.actor_id, item.scope)
    elif job.state == "error":
        from .wa_editorial import reply_card
        messages = [reply_card(story, f"Pauta *#{story.id}*\n{job.error or 'A geração falhou.'}\n"
                               "Responda *REFAZER* para tentar novamente.", item.actor_id, item.scope)]
    else:
        item.state = "cancelled"
        db.session.commit()
        return
    for message in messages:
        message.setdefault("id", "3EB0" + token_hex(9).upper())
    item.state = "sending"
    db.session.commit()
    try:
        if config:
            result = gateway("POST", "/radar/group/reply", timeout=(3, 35),
                             json={"group_id": item.scope, "revision": config["revision"], "messages": messages})
        else:
            result = gateway("POST", "/radar/reply", timeout=(3, 35), json={"phone": phone, "messages": messages})
        item.state = "sent" if result.get("accepted") is True else "failed"
    except EditorialError:
        item.state = "failed"
    db.session.commit()

