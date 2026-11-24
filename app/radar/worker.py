import logging
from time import monotonic, sleep

import click
from flask import Flask
from sqlalchemy import text

from ..config import Config
from ..models import SiteSetting, db
from .collector import next_run, run_collection, source_domain
from .editorial import publish_due, run_job
from .migration import radar_cli
from . import seeds  # Registra o comando radar seed.
from .models import Alert, Job, Run, now
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
    if paused():
        return
    publish_due()
    send_pending_alert()
    job = Job.query.filter_by(state="pending").order_by(Job.id).first()
    if job:
        run_job(job)
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


def recover_interrupted():
    Alert.query.filter_by(state="sending").update({"state": "error", "message": "Entrega sem confirmação.", "finished": now()})
    Run.query.filter_by(state="running").update({"state": "pending"})
    Job.query.filter_by(state="running").update({"state": "pending"})
    db.session.commit()


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
            domains = {}
            while True:
                try:
                    tick(domains)
                except Exception as exc:
                    db.session.rollback()
                    LOGGER.error("Falha em ciclo do Radar (%s).", type(exc).__name__)
                    if once:
                        raise
                    recover_interrupted()
                finally:
                    db.session.remove()
                if once:
                    break
                if len(domains) > 500:
                    domains = {key: value for key, value in domains.items() if monotonic() - value < 60}
                sleep(5)
        finally:
            if postgres:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})


def create_worker_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    app.cli.add_command(radar_cli)
    return app

