import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import generate_csrf, validate_csrf
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from wtforms.validators import ValidationError

from ..models import SiteSetting, db
from ..timezone_utils import format_datetime_br
from .collector import queue_source
from .editorial import (EditorialError, can_publish, latest_draft, lock_story,
                        model_name, publish, queue_draft, save_draft, verify_approval)
from .feeds import SOURCE_KINDS
from .fetch import public_url
from .models import Alert, Article, Audit, Draft, GroupDelivery, InboundMessage, Job, Run, Source, Story, WhatsAppPreference, audit, now
from .group_delivery import group_settings
from .whatsapp import GROUP_ID, gateway, phone_number, status as whatsapp_status


bp = Blueprint("radar", __name__, url_prefix="/admin/radar")
TABS = {"new": "Para analisar", "urgent": "Urgentes", "research": "Em apuração",
        "production": "Em produção", "review": "Aguardando revisão", "scheduled": "Agendadas",
        "published": "Publicadas", "discarded": "Descartadas"}
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


@bp.before_request
def require_radar_admin():
    if not current_user.is_authenticated:
        return redirect(url_for("admin.login", next=request.path))
    if not current_user.is_active or not current_user.is_admin:
        abort(403)
    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token"))
        except ValidationError:
            abort(400, "Formulário expirado. Atualize a página e tente novamente.")


@bp.context_processor
def helpers():
    return {"radar_csrf": generate_csrf, "radar_datetime": format_datetime_br, "radar_tabs": TABS}


def screen(template, **context):
    return render_template(f"admin/radar/{template}.html", section="radar",
                           admin_media_root=current_app.config["MEDIA_ROOT"], **context)


@bp.get("")
@bp.get("/")
def index():
    from .models import MANUAL_REGION
    tab = request.args.get("tab", "new")
    term = request.args.get("q", "").strip()[:160]
    city = request.args.get("city", "").strip()[:150]
    region = request.args.get("region", "").strip()[:100]
    category = request.args.get("category", "").strip()[:100]
    source_id = request.args.get("source", type=int)
    query = Story.query.filter(Story.region != MANUAL_REGION).options(selectinload(Story.articles).joinedload(Article.source))
    if term:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.filter(or_(Story.title.ilike(pattern, escape="\\"),
                                 Story.articles.any(or_(Article.title.ilike(pattern, escape="\\"),
                                                        Article.summary.ilike(pattern, escape="\\")))))
        if "tab" not in request.args:
            tab = "all"
    if tab == "urgent":
        query = query.filter(Story.score >= 75, Story.state.in_(("new", "research", "production", "review")))
    elif tab in TABS:
        query = query.filter(Story.state == tab)
    elif tab != "all":
        abort(404)
    if city:
        query = query.filter_by(location=city)
    if region:
        query = query.filter_by(region=region)
    if category:
        query = query.filter_by(category=category)
    if source_id:
        query = query.filter(Story.articles.any(Article.source_id == source_id))
    stories = query.order_by(Story.score.desc(), Story.updated.desc(), Story.id.desc()).paginate(
        page=max(1, request.args.get("page", 1, type=int)), per_page=20, error_out=False)
    midnight = datetime.combine(datetime.now(LOCAL_TZ).date(), time(), LOCAL_TZ)
    today = midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return screen("index", nav="stories", tab=tab, stories=stories, term=term, city=city, region=region,
                  category=category, source_id=source_id,
                  cities=[row[0] for row in db.session.query(Story.location).filter(Story.region != MANUAL_REGION).distinct().order_by(Story.location)],
                  regions=[row[0] for row in db.session.query(Story.region).filter(Story.region != MANUAL_REGION).distinct().order_by(Story.region)],
                  categories=[row[0] for row in db.session.query(Story.category).filter(Story.region != MANUAL_REGION,Story.category != "")
                              .distinct().order_by(Story.category)],
                  sources=Source.query.filter_by(archived=False).order_by(Source.name).all(),
                  today=Article.query.join(Story).filter(Story.region != MANUAL_REGION,Article.discovered >= today).count(),
                  urgent=Story.query.filter(Story.region != MANUAL_REGION,Story.score >= 75, Story.state.in_(("new", "research", "review"))).count(),
                  pending=Story.query.filter(Story.region != MANUAL_REGION,Story.state=="review").count(),
                  failures=Source.query.filter(Source.active.is_(True), Source.failures > 0).count())


@bp.get("/fontes")
def sources():
    records = Source.query.filter_by(archived=False).order_by(Source.active.desc(), Source.name).all()
    selected_id = request.args.get("run", type=int)
    selected = db.session.get(Run, selected_id) if selected_id else None
    latest_ids = db.session.query(func.max(Run.id)).group_by(Run.source_id)
    runs = {run.source_id: run for run in Run.query.filter(Run.id.in_(latest_ids)).all()}
    return screen("sources", nav="sources", records=records, runs=runs, selected=selected, kinds=SOURCE_KINDS)


@bp.route("/fontes/nova", methods=["GET", "POST"], defaults={"source_id": None})
@bp.route("/fontes/<int:source_id>/editar", methods=["GET", "POST"])
def source_form(source_id):
    if source_id is not None:
        source = db.session.get(Source, source_id)
        if not source or source.archived:
            abort(404)
    else:
        source = Source(kind="site", interval=15, priority=3, active=True)
    data = request.form if request.method == "POST" else None
    if data is not None:
        try:
            name = data.get("name", "").strip()
            if not name or len(name) > 160:
                raise ValueError("Informe um nome de até 160 caracteres.")
            address = public_url(data.get("url", "").strip())
            kind = data.get("kind")
            if kind not in SOURCE_KINDS:
                raise ValueError("Escolha o tipo de fonte.")
            try:
                interval, priority = int(data.get("interval", "")), int(data.get("priority", ""))
            except ValueError as exc:
                raise ValueError("Informe um intervalo e uma prioridade válidos.") from exc
            if not 2 <= interval <= 1440 or not 1 <= priority <= 5:
                raise ValueError("Use um intervalo de 2 a 1.440 minutos e prioridade entre 1 e 5.")
            region, category, use_policy = (data.get(key, "").strip() for key in ("region", "category", "use_policy"))
            if len(region) > 100 or len(category) > 100 or len(use_policy) > 300:
                raise ValueError("Confira o limite dos campos de região, categoria e política de uso.")
            existing = Source.query.filter_by(url=address).first()
            if existing and existing.id != source_id:
                if not source_id and existing.archived:
                    source = existing
                    source.archived = False
                else:
                    raise ValueError("Esta URL já está cadastrada.")
            if source.id:
                source = db.session.execute(select(Source).where(Source.id == source.id).with_for_update()).scalar_one()
            if source.url != address or source.kind != kind:
                source.cache_url = source.etag = source.modified = None
                source.failures = 0
                source.last_success = None
            source.name, source.url, source.kind = name, address, kind
            source.interval, source.priority = interval, priority
            source.region, source.category, source.use_policy = region, category, use_policy
            source.active = data.get("active") == "1"
            source.next_run = now()
            db.session.add(source)
            db.session.flush()
            audit("fonte_editada" if source_id else "fonte_adicionada", actor=current_user.id, source=source.id)
            db.session.commit()
            flash("Fonte salva.", "success")
            return redirect(url_for("radar.sources"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except IntegrityError:
            db.session.rollback()
            flash("Esta URL já está cadastrada.", "danger")
    return screen("source_form", nav="sources", source=source, data=data, kinds=SOURCE_KINDS)


@bp.post("/fontes/<int:source_id>/<action>")
def source_action(source_id, action):
    source = db.session.execute(select(Source).where(Source.id == source_id).with_for_update()).scalar_one_or_none()
    if not source or source.archived:
        abort(404)
    run = None
    if action == "toggle":
        source.active = not source.active
        source.next_run = now()
        flash("Fonte ativada." if source.active else "Fonte pausada.", "success")
    elif action == "archive":
        source.active, source.archived = False, True
        flash("Fonte removida do monitoramento. O histórico permanece disponível.", "success")
    elif action in {"test", "manual"}:
        if action == "manual" and not source.active:
            flash("Ative a fonte antes de iniciar uma coleta.", "warning")
            return redirect(url_for("radar.sources"))
        run = queue_source(source, action)
        if run.state == "pending":
            flash("Consulta enviada ao worker. O resultado aparecerá no histórico.", "info")
    else:
        abort(404)
    audit("fonte_" + action, actor=current_user.id, source=source.id)
    run_id = run.id if run else None
    db.session.commit()
    return redirect(url_for("radar.sources", run=run_id))


@bp.get("/fontes/<int:source_id>/historico")
def source_history(source_id):
    source = db.get_or_404(Source, source_id)
    runs = Run.query.filter_by(source_id=source_id).order_by(Run.id.desc()).limit(50).all()
    return screen("source_history", nav="sources", source=source, runs=runs)


@bp.get("/pautas/<int:story_id>")
def detail(story_id):
    story = db.get_or_404(Story, story_id)
    drafts = Draft.query.filter_by(story_id=story_id).order_by(Draft.version.desc()).all()
    version = request.args.get("version", type=int)
    draft = next((item for item in drafts if item.version == version), None) if version else (
        drafts[0] if drafts else None)
    if version and not draft:
        abort(404)
    return screen("detail", nav="stories", story=story, draft=draft, drafts=drafts,
                  editable=not story.post_id and (not version or (draft and draft.id == drafts[0].id)),
                  events=Audit.query.filter_by(story_id=story_id).order_by(Audit.id.desc()).limit(30),
                  publishing=os.getenv("RADAR_ENABLE_PUBLISHING") == "1" and can_publish(current_user.id),
                  ai_configured=bool(os.getenv("RADAR_OLLAMA_URL")))


@bp.post("/pautas/<int:story_id>/<action>")
def story_action(story_id, action):
    story = lock_story(story_id)
    if not story:
        abort(404)
    actor = current_user.id
    old = story.state
    try:
        if action in {"research", "approve", "discard"}:
            if story.post_id or old in {"published", "scheduled", "discarded"}:
                raise EditorialError("Esta pauta não permite a ação no estado atual.")
            story.state = {"research": "research", "approve": "production", "discard": "discarded"}[action]
            story.previous_state = old
        elif action == "undo":
            if old != "discarded" or not story.previous_state:
                raise EditorialError("Esta pauta não tem descarte para desfazer.")
            story.state, story.previous_state = story.previous_state, None
        elif action == "generate":
            queue_draft(story, actor)
        elif action == "save":
            if story.post_id or old in {"discarded", "published", "scheduled"}:
                raise EditorialError("A pauta precisa estar em análise ou revisão.")
            draft = latest_draft(story.id)
            version = request.form.get("version", type=int)
            if (draft.version if draft else 0) != version:
                raise EditorialError("Uma versão mais recente foi salva. Revise a página atual.")
            result = {name: request.form.get(field, "").strip() for name, field in (
                ("titulo", "title"), ("resumo", "excerpt"), ("corpo", "body"))}
            if (not result["titulo"] or len(result["titulo"]) > 500 or len(result["resumo"]) > 2000
                    or not result["corpo"] or len(result["corpo"]) > 10000):
                raise EditorialError("Preencha título e corpo dentro dos limites do formulário.")
            save_draft(story, result, actor)
        elif action == "approve_version":
            draft = latest_draft(story.id)
            if (story.state != "review" or not draft or draft.version != request.form.get("version", type=int)
                    or request.form.get("verified") != "1"):
                raise EditorialError("Confira as fontes e confirme a revisão da versão atual.")
            story.approved_draft_id = draft.id
        elif action == "publish":
            if request.form.get("confirm") != "1":
                raise EditorialError("Confirme a publicação da versão revisada.")
            publish(story, request.form.get("version", type=int), actor)
        elif action == "schedule":
            if os.getenv("RADAR_ENABLE_PUBLISHING") != "1" or not can_publish(actor):
                raise EditorialError("A publicação pelo Radar está desativada para este administrador.")
            verify_approval(story, request.form.get("version", type=int))
            if story.state != "review":
                raise EditorialError("A revisão precisa ser aprovada antes do agendamento.")
            try:
                scheduled = datetime.fromisoformat(request.form.get("scheduled", "")).replace(tzinfo=LOCAL_TZ)
            except ValueError as exc:
                raise EditorialError("Informe uma data e horário de Brasília válidos.") from exc
            when = scheduled.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            if when <= now() or when > now() + timedelta(days=90):
                raise EditorialError("Selecione um horário futuro nos próximos 90 dias.")
            story.state, story.scheduled = "scheduled", when
            audit("agendada", actor=actor, story=story_id, details=scheduled.isoformat())
        elif action == "cancel_schedule" and old == "scheduled":
            story.state, story.scheduled = "review", None
        else:
            abort(404)
        story.updated = now()
        audit(action, actor=actor, story=story_id, details=f"{old} → {story.state}")
        db.session.commit()
        flash("Decisão registrada.", "success")
    except EditorialError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except IntegrityError:
        db.session.rollback()
        flash("A operação conflita com uma alteração recente. Atualize a pauta.", "warning")
    return redirect(url_for("radar.detail", story_id=story_id))


@bp.get("/saude")
def health():
    record = SiteSetting.query.filter_by(key="radar_worker_heartbeat").first()
    paused = SiteSetting.query.filter_by(key="radar_worker_paused").first()
    heartbeat = datetime.fromisoformat(record.value) if record and record.value else None
    return screen("health", nav="health", heartbeat=heartbeat,
                  online=heartbeat and heartbeat > now() - timedelta(minutes=5),
                  paused=bool(paused and paused.value == "1"),
                  pending_runs=Run.query.filter(Run.state.in_(("pending", "running"))).count(),
                  pending_jobs=Job.query.filter(Job.state.in_(("pending", "running"))).count(),
                  jobs=Job.query.order_by(Job.id.desc()).limit(10).all(),
                  model=model_name(), ai_configured=bool(os.getenv("RADAR_OLLAMA_URL")),
                  runs=Run.query.order_by(Run.id.desc()).limit(15).all())


@bp.post("/saude/worker")
def worker_action():
    action = request.form.get("action")
    if action not in {"pause", "resume"}:
        abort(400)
    record = SiteSetting.query.filter_by(key="radar_worker_paused").first()
    if not record:
        record = SiteSetting(key="radar_worker_paused")
        db.session.add(record)
    record.value = "1" if action == "pause" else "0"
    audit("worker_" + action, actor=current_user.id)
    db.session.commit()
    return redirect(url_for("radar.health"))



@bp.post("/whatsapp/grupo")
def whatsapp_group_save():
    group_id = request.form.get("group_id", "")
    minimum = request.form.get("minimum", type=int)
    bot = request.form.get("enabled") == "1"
    if (group_id and not GROUP_ID.fullmatch(group_id)) or (bot and not group_id) or not minimum or not 75 <= minimum <= 100:
        flash("Confira o grupo e a pontuação mínima.", "warning")
        return redirect(url_for("radar.whatsapp"))
    try:
        gateway("POST", "/radar/group/settings", json={
            "group_id": group_id, "enabled": bot, "minimum": minimum,
            "news": request.form.get("news") == "1", "alerts": request.form.get("alerts") == "1",
            "revision": request.form.get("revision", ""),
        })
    except EditorialError as exc:
        flash(str(exc), "danger")
    else:
        audit("whatsapp_grupo_atualizado", actor=current_user.id,
              details="Bot ativado" if bot else "Bot pausado")
        db.session.commit()
        flash("Grupo salvo. Digite menu no grupo para abrir os atalhos." if bot else "Configuração salva.", "success")
    return redirect(url_for("radar.whatsapp"))


@bp.route("/whatsapp", methods=["GET", "POST"])
def whatsapp():
    preference = db.session.get(WhatsAppPreference, current_user.id)
    phone = phone_number(current_user.whatsapp)
    if request.method == "POST":
        minimum = request.form.get("minimum", type=int)
        opt_in = request.form.get("enabled") == "1"
        if not phone or not minimum or not 75 <= minimum <= 100:
            flash("Confira o número cadastrado e a pontuação mínima.", "warning")
        else:
            if not preference:
                preference = WhatsAppPreference(user_id=current_user.id, phone=phone)
                db.session.add(preference)
            if opt_in and (not preference.enabled or phone != preference.phone):
                preference.subscribed = now()
            preference.phone, preference.enabled, preference.minimum = phone, opt_in, minimum
            audit("whatsapp_preferencia", actor=current_user.id, details="Ativado" if opt_in else "Pausado")
            try:
                db.session.commit()
                flash("Preferências salvas.", "success")
            except IntegrityError:
                db.session.rollback()
                flash("Este número já está associado a outro administrador do Radar.", "warning")
        return redirect(url_for("radar.whatsapp"))
    group = None
    group_options, group_deliveries, group_error = [], [], ""
    try:
        group = group_settings()
        if group.get("connected"):
            group_options = gateway("GET", "/radar/group/list").get("groups", [])
        if group.get("group_id"):
            group_deliveries = GroupDelivery.query.filter_by(group_id=group["group_id"]).order_by(
                GroupDelivery.id.desc()).limit(20).all()
    except EditorialError as exc:
        group_error = str(exc)
    return screen("whatsapp", nav="whatsapp", preference=preference, number=phone, group=group,
                  group_options=group_options, group_error=group_error, group_deliveries=group_deliveries,
                  connection=whatsapp_status(), alerts=Alert.query.filter_by(user_id=current_user.id)
                  .order_by(Alert.id.desc()).limit(20),
                  last_inbound=InboundMessage.query.filter_by(user_id=current_user.id)
                  .order_by(InboundMessage.id.desc()).first())
