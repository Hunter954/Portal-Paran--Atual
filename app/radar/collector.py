from datetime import timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from random import randint
from time import monotonic
from urllib.parse import urlsplit
import re

from sqlalchemy import select

from ..models import db
from .feeds import get_feed
from .fetch import FetchError, PublicFetcher
from .geography import classify, normalize
from .models import Article, Run, Source, Story, MANUAL_REGION, audit, now


URGENT = re.compile(
    r"\b(interdicao|interditad[ao]|alerta|deslizamento|desabamento|inundacao|"
    r"alagamento|desaparecid[ao]|acidente grave|mata|mort[oa]s|emergencia|"
    r"evacuacao|temporal|tornado|incendio)\b"
)
CRITICAL = re.compile(r"\b(alerta vermelho|estado de calamidade|ordem de evacuacao)\b")
COMMON = re.compile(r"\b(prefeitura|justica|governo|escola|saude|hospital|policia|servico)\b")
STOP = {"para", "pela", "pelo", "sobre", "apos", "contra", "uma", "com", "por", "dos", "das",
        "entre", "mais", "quando", "muito", "veja", "como", "esta", "nesta", "esse"}


def queue_source(source, kind):
    run = Run.query.filter(Run.source_id == source.id, Run.kind == kind,
                           Run.state.in_(("pending", "running"))).first()
    if not run:
        run = Run(source_id=source.id, kind=kind)
        db.session.add(run)
        db.session.flush()
    return run


def urgency(title, priority, published):
    value = normalize(title)
    score = 28 + priority * 6
    if CRITICAL.search(value):
        score += 49
    elif URGENT.search(value):
        score += 34
    elif COMMON.search(value):
        score += 13
    if published and now() - published <= timedelta(hours=12):
        score += 7
    return min(100, score)


def similar(left, right):
    lhs, rhs = normalize(left), normalize(right)
    if lhs == rhs:
        return True
    words = [set(re.findall(r"\b\w{3,}\b", value)) - STOP for value in (lhs, rhs)]
    numbers = [set(re.findall(r"\d+", value)) for value in (lhs, rhs)]
    if all(numbers) and numbers[0] != numbers[1]:
        return False
    union = words[0] | words[1]
    return (len(words[0] & words[1]) >= 4 and
            (len(words[0] & words[1]) / len(union) >= .65 or SequenceMatcher(None, lhs, rhs).ratio() >= .85))


def store_item(source, item, location):
    if Article.query.filter_by(url=item.url).first():
        return False
    fingerprint = sha256(normalize(item.title + "\n" + item.summary).encode()).hexdigest()
    recent = Story.query.filter(Story.region != MANUAL_REGION,Story.location == location.name, Story.created > now() - timedelta(days=3))
    same = recent.join(Article).filter(Article.digest == fingerprint).first()
    if same is None:
        matches = recent.order_by(Story.id.desc()).limit(100)
        same = next((story for story in matches if similar(story.title, item.title)), None)
    score = urgency(item.title, source.priority, item.published)
    if same is None:
        same = Story(title=item.title, location=location.name, region=location.region,
                     category=source.category, score=score)
        db.session.add(same)
        db.session.flush()
        audit("descoberta", story=same.id, source=source.id, details=location.evidence[:300])
    else:
        same.score = max(score, same.score)
        same.updated = now()
        audit("fonte_relacionada", story=same.id, source=source.id)
    db.session.add(Article(story_id=same.id, source_id=source.id, url=item.url,
                           title=item.title, summary=item.summary, evidence=location.evidence,
                           published_at=item.published, digest=fingerprint))
    db.session.flush()
    return True


def next_run():
    pending = Run.query.filter_by(state="pending").order_by(Run.started, Run.id).first()
    if pending:
        return pending
    source = Source.query.filter_by(active=True, archived=False).filter(Source.next_run <= now()).order_by(
        Source.next_run, Source.priority.desc(), Source.id).first()
    if source:
        return queue_source(source, "auto")
    return None


def run_collection(run):
    source = db.session.get(Source, run.source_id)
    if not source or source.archived or (not source.active and run.kind != "test"):
        run.state, run.finished, run.message = "error", now(), "Fonte suspensa ou removida."
        db.session.commit()
        return
    run_id, source_id, kind, url = run.id, source.id, run.kind, source.url
    snapshot = Source(url=url, kind=source.kind)
    if kind != "test":
        snapshot.cache_url, snapshot.etag, snapshot.modified = source.cache_url, source.etag, source.modified
    run.started, run.state = now(), "running"
    db.session.commit()
    started = monotonic()
    try:
        response, items = get_feed(PublicFetcher(), snapshot)
        error = None
    except FetchError as exc:
        error = str(exc)

    source = db.session.execute(select(Source).where(Source.id == source_id).with_for_update()).scalar_one()
    run = db.session.get(Run, run_id)
    if source.archived or source.url != url or source.kind != snapshot.kind or (not source.active and kind != "test"):
        run.state, run.finished, run.message = "error", now(), "Cadastro alterado durante a consulta."
        db.session.commit()
        return

    run.finished = now()
    run.duration_ms = round((monotonic() - started) * 1000)
    source.last_run = run.started
    if error:
        run.state, run.message = "error", error[:400]
        source.failures += 1
        delay = min(360, source.interval * 2 ** min(5, source.failures))
    else:
        run.state = "done"
        source.last_success = run.finished
        source.failures = 0
        source.response_ms = round((source.response_ms * 3 + run.duration_ms) / 4) if source.response_ms else run.duration_ms
        delay = source.interval
        if response.status == 304:
            run.message = "A fonte informou que o conteúdo segue na mesma versão."
        elif items:
            run.received = len(items)
            for item in items:
                location = classify(item.title, item.summary)
                if not location or (item.published and
                    (item.published < now() - timedelta(days=7) or item.published > now() + timedelta(minutes=20))):
                    run.excluded += 1
                    continue
                if kind == "test" or store_item(source, item, location):
                    run.accepted += 1
            run.message = (f"{run.received} lidas · {run.accepted} "
                           f"{'elegíveis' if kind == 'test' else 'novas'} · {run.excluded} fora do recorte ou período.")
        else:
            run.message = "A consulta retornou um feed válido, sem itens com título e link original."
        if kind != "test" and response.status == 200:
            source.cache_url = response.url
            source.etag = response.headers.get("etag", "")[:500] or None
            source.modified = response.headers.get("last-modified", "")[:100] or None
    if kind != "test":
        source.next_run = now() + timedelta(minutes=delay, seconds=randint(0, 30))
    db.session.commit()


def source_domain(run):
    source = db.session.get(Source, run.source_id)
    hostname = urlsplit(source.cache_url or source.url).hostname if source else ""
    return (hostname or "").removeprefix("www.")

