import logging
import os
from datetime import timedelta
from threading import Event, Thread
from time import monotonic, sleep

import click
from flask import Flask, current_app
from sqlalchemy import text

from ..config import Config
from ..models import SiteSetting, db
from .collector import next_run, run_collection, source_domain
from .editorial import EditorialError, lock_story, publish_due, queue_draft, run_job
from .migration import radar_cli
from . import seeds  # Registra o comando radar seed.
from .models import Alert, ChatDraftReply, CoverSearch, GroupDelivery, Job, ManualSearch, Run, Story, MANUAL_REGION, now
from .wa_cover import run_cover_search
from .wa_delivery import send_pending_draft_reply
from .group_delivery import send_pending_group
from .whatsapp import send_pending_alert


LOGGER = logging.getLogger(__name__)
LOCK_ID = 6410401


def heartbeat():
    record = SiteSetting.query.filter_by(key="radar_worker_heartbeat").first()
    if not record:
        record = SiteSetting(key="radar_worker_heartbeat")
        db.session.add(record)
    record.value = now().isoformat()
    db.session.commit()


def paused():
    record = SiteSetting.query.filter_by(key="radar_worker_paused").first()
    return bool(record and record.value == "1")


def tick(domains):
    heartbeat()
    worker_paused = paused()
    send_pending_group(include_alerts=not worker_paused)
    from .manual_search import run_manual_search
    run_manual_search()
    if worker_paused:
        return
    publish_due()
    send_pending_alert()
    run_cover_search()
    run = next_run()
    if run:
        domain = source_domain(run)
        if domain:
            delay = 10 - (monotonic() - domains.get(domain, 0))
            if delay > 0:
                sleep(delay)
        run_collection(run)
        domains[domain] = monotonic()
    db.session.commit()


def recover_interrupted(*, editorial=True):
    if editorial:
        ChatDraftReply.query.filter_by(state="sending").update({"state": "failed"})
        Job.query.filter_by(state="running").update({"state": "pending"})
    CoverSearch.query.filter_by(state="sending").update({"state": "failed"})
    ManualSearch.query.filter_by(state='running').update({'state':'pending'})
    GroupDelivery.query.filter_by(state="sending").update(
        {"state": "error", "message": "Entrega sem confirmação.", "finished": now()})
    Alert.query.filter_by(state="sending").update({"state": "error", "message": "Entrega sem confirmação.", "finished": now()})
    Run.query.filter_by(state="running").update({"state": "pending"})
    db.session.commit()


def prefetch_job():
    latest = [story.id for story in Story.query.filter(Story.region != MANUAL_REGION,
        Story.state.in_(("new", "research", "production", "review")),
        Story.created >= now() - timedelta(days=7), Story.articles.any(),
    ).order_by(Story.id.desc()).limit(4)]
    if not latest:
        return None
    candidate = Story.query.filter(Story.id.in_(latest), Story.state.in_(("new", "research")),
                                   ~db.session.query(Job.id).filter(Job.story_id == Story.id).exists()
                                   ).order_by(Story.id.desc()).first()
    if candidate:
        story = lock_story(candidate.id)
        if story.state in {"new", "research"}:
            try:
                return queue_draft(story, None)
            except EditorialError:
                return None
    return None


def editorial_step(*, prefetch=True):
    send_pending_draft_reply()
    if paused():
        return False
    job = Job.query.filter_by(state="pending").order_by(Job.actor_id.is_(None), Job.id).first()
    if job is None and prefetch:
        job = prefetch_job()
        if job:
            db.session.flush()
    if job is None:
        return False
    run_job(job)
    send_pending_draft_reply()
    return True


def editorial_loop(app, stop):
    while not stop.is_set():
        with app.app_context():
            try:
                if editorial_step(prefetch=os.getenv("RADAR_PREFETCH", "0") == "1"):
                    continue
            except Exception as exc:
                db.session.rollback()
                LOGGER.error("Falha no ciclo editorial do Radar (%s).", type(exc).__name__)
                Job.query.filter_by(state="running").update({"state": "pending"})
                ChatDraftReply.query.filter_by(state="sending").update({"state": "failed"})
                db.session.commit()
        stop.wait(0.8)


@radar_cli.command("worker")
@click.option("--once", is_flag=True, help="Executa um ciclo e encerra.")
def worker(once):
    """Executa a coleta e as tarefas editoriais fora do servidor web."""
    with db.engine.connect() as conn:
        postgres = conn.dialect.name == "postgresql"
        if postgres:
            if not conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}).scalar():
                raise click.ClickException("Já existe um worker Radar neste banco.")
            conn.commit()
        try:
            recover_interrupted()
            stop = Event()
            editor = Thread(target=editorial_loop, args=(current_app._get_current_object(), stop), daemon=True)
            if not once:
                editor.start()
            domains = {}
            while True:
                try:
                    if once:
                        editorial_step(prefetch=False)
                    tick(domains)
                except Exception as exc:
                    db.session.rollback()
                    LOGGER.error("Falha em ciclo do Radar (%s).", type(exc).__name__)
                    if once:
                        raise
                    recover_interrupted(editorial=False)
                finally:
                    db.session.remove()
                if once:
                    break
                if len(domains) > 500:
                    domains = {key: value for key, value in domains.items() if monotonic() - value < 60}
                sleep(5)
        finally:
            if "stop" in locals():
                stop.set()
            if postgres:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})


def create_worker_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    app.cli.add_command(radar_cli)
    return app

