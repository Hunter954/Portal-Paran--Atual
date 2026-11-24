from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..models import db
from .collector import queue_source
from .editorial import EditorialError, can_publish, latest_draft
from .feeds import SOURCE_KINDS
from .fetch import FetchError, public_url
from .geography import normalize
from .models import Source, audit, now


LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
SOURCE_HELP = ("*Fontes do Radar*\nRADAR FONTES · RADAR FONTE ADICIONAR nome | URL | rss\n"
               "Tipos: rss, site, sitemap\nRADAR FONTE PAUSAR ID · RADAR FONTE ATIVAR ID · RADAR COLETAR ID")


def source_command(raw, actor):
    command = raw.split(maxsplit=2)
    action = normalize(command[1]) if len(command) > 1 else ""
    arg = command[2] if len(command) > 2 else ""
    if normalize(command[0]) == "coletar":
        action, arg = "coletar", command[1] if len(command) == 2 else ""
    if not action or normalize(command[0]) == "fontes":
        items = Source.query.filter_by(archived=False).order_by(Source.id).limit(25).all()
        rows = [f"#{item.id} · {item.name} · {'ativa' if item.active else 'pausada'}"
                + (f" · {item.failures} falhas" if item.failures else "") for item in items]
        return ("\n".join(rows) or "Nenhuma fonte cadastrada.") + "\n\n" + SOURCE_HELP
    if action == "adicionar":
        parts = [part.strip() for part in arg.split("|")]
        if len(parts) not in (2, 3):
            return SOURCE_HELP
        name, address, *type_field = parts
        kind = type_field[0].lower() if type_field else "rss"
        if not name or len(name) > 150 or kind not in SOURCE_KINDS:
            raise EditorialError("Informe um nome de até 150 caracteres e um tipo válido.")
        try:
            address = public_url(address)
        except FetchError as exc:
            raise EditorialError(str(exc)) from exc
        if Source.query.filter_by(url=address).first():
            raise EditorialError("Esse endereço já está cadastrado.")
        item = Source(name=name, url=address, kind=kind, active=True, next_run=now())
        db.session.add(item)
        db.session.flush()
        audit("whatsapp_fonte_criada", actor=actor, source=item.id)
        return f"Fonte *#{item.id} · {item.name}* cadastrada. A primeira coleta foi agendada."
    if action not in {"pausar", "ativar", "coletar"} or not arg.isdecimal() or len(arg) > 9:
        return SOURCE_HELP
    item = db.session.execute(select(Source).where(Source.id == int(arg), Source.archived.is_(False)).with_for_update()).scalar_one_or_none()
    if not item:
        raise EditorialError("Fonte não encontrada.")
    if action == "coletar":
        if not item.active:
            raise EditorialError("Ative a fonte antes de solicitar a coleta.")
        run = queue_source(item, "manual")
        audit("whatsapp_fonte_coleta", actor=actor, source=item.id)
        return f"Coleta *#{run.id}* na fila para {item.name}. Use *RADAR LISTAR 10* para conferir."
    item.active = action == "ativar"
    if item.active:
        item.next_run = now()
    audit("whatsapp_fonte_atualizada", actor=actor, source=item.id, details=action)
    return f"Fonte *#{item.id} · {item.name}* {'ativada' if item.active else 'pausada'}."


def schedule_from_preview(story, card, raw, actor):
    draft = latest_draft(story.id)
    if not card or not draft or card.version != draft.version or story.state != "review":
        raise EditorialError("Responda na prévia da versão atual com *RADAR AGENDAR DD/MM/AAAA HH:MM*.")
    from .wa_editorial import legacy_draft, request_draft
    if legacy_draft(draft):
        return request_draft(story, actor, card.scope, fresh=True)
    if not can_publish(actor):
        raise EditorialError("Este administrador não pode agendar publicações.")
    try:
        text = raw.split(maxsplit=1)[1].strip()
        scheduled = datetime.strptime(text, "%d/%m/%Y %H:%M").replace(tzinfo=LOCAL_TZ)
    except (ValueError, IndexError) as exc:
        raise EditorialError("Informe data e horário, como *RADAR AGENDAR DD/MM/AAAA HH:MM* (Brasília).") from exc
    utc = scheduled.astimezone(timezone.utc).replace(tzinfo=None)
    current = now()
    if not current < utc < current + timedelta(days=90):
        raise EditorialError("Escolha um horário futuro dentro dos próximos 90 dias.")
    story.state, story.scheduled, story.approved_draft_id = "scheduled", utc, draft.id
    story.updated = current
    audit("agendada", actor=actor, story=story.id, details=f"WhatsApp · versão {draft.version}")
    return f"*Pauta #{story.id}* agendada para {scheduled.strftime('%d/%m/%Y às %H:%M')} (Brasília)."

