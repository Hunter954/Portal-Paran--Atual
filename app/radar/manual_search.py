"""User-requested news searches, independent of the Paraná monitoring queue."""
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlencode,urlsplit,parse_qs
from secrets import token_hex
import re
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from sqlalchemy import select
from ..models import User,db
from .fetch import PublicFetcher,FetchError,canonical_url
from .feeds import child_text,date_from,plain
from .models import ManualSearch,SiteChat,Source,Story,Article,MANUAL_REGION,now,audit
from .editorial import EditorialError,can_publish
from .whatsapp import enabled,gateway,phone_number

def search_news(query,days=7,fetcher=None):
    query=" ".join(query.split())
    if not 3<=len(query)<=220 or days not in (0,7,30):raise EditorialError("Informe um assunto entre 3 e 220 caracteres.")
    url='https://www.bing.com/news/search?'+urlencode({'q':query,'format':'rss','mkt':'pt-br'})
    response=(fetcher or PublicFetcher()).get(url)
    if response.status!=200:raise FetchError('O serviço de busca está indisponível.')
    try:root=ElementTree.fromstring(response.body)
    except (ElementTree.ParseError,DefusedXmlException) as exc:raise FetchError('A busca retornou uma resposta inválida.') from exc
    if root.tag.rsplit('}',1)[-1]!='rss':raise FetchError('A busca não retornou notícias.')
    results=[];seen=set()
    for row in root.iter('item'):
        link=child_text(row,'link');parts=urlsplit(link)
        if (parts.hostname or '').removeprefix('www.')=='bing.com':
            link=parse_qs(parts.query).get('url',[''])[0]
        try:link=canonical_url(link)
        except (FetchError,ValueError):continue
        if (urlsplit(link).hostname or '').removeprefix('www.') in {'bing.com','news.google.com','google.com'}:continue
        title=plain(child_text(row,'title'),500);published=date_from(child_text(row,'pubDate'))
        if not title or link in seen:continue
        if published and published>now()+timedelta(minutes=20):continue
        if days and (not published or published<now()-timedelta(days=days)):continue
        seen.add(link)
        results.append({'title':title,'url':link,'summary':plain(child_text(row,'description'),650),
                        'source':plain(child_text(row,'Source','source'),120) or urlsplit(link).hostname,
                        'published':published.isoformat()+'Z' if published else None})
        if len(results)==10:break
    return results

def request_search(actor,scope,query,days):
    if not can_publish(actor):raise EditorialError('Seu usuário não tem acesso editorial.')
    query=' '.join(query.split())
    if not 3<=len(query)<=220:raise EditorialError('Informe um assunto entre 3 e 220 caracteres.')
    ManualSearch.query.filter_by(actor_id=actor,scope=scope or '',state='pending').update({'state':'cancelled'})
    job=ManualSearch(actor_id=actor,scope=scope or '',term=query,days=days)
    db.session.add(job);db.session.flush()
    audit('busca_manual_solicitada',actor=actor,details=query)
    return job

def selected_search(ident,actor,scope):
    job=db.session.get(ManualSearch,ident)
    if not job or job.actor_id!=actor or job.scope!=(scope or ''):raise EditorialError('Essa busca não pertence à sua conversa.')
    return job

def import_result(job,index,actor):
    if not can_publish(actor) or job.actor_id!=actor or job.state!='ready':raise EditorialError('A busca não está disponível para esse usuário.')
    try:result=job.results[index]
    except (IndexError,TypeError):raise EditorialError('Escolha uma notícia da lista.') from None
    url=canonical_url(result['url'])
    existing=Article.query.filter_by(url=url).first()
    if existing:return existing.story
    source=Source.query.filter_by(url=url).first()
    if not source:
        source=Source(name=result['source'][:160],url=url,kind='site',active=False,archived=True)
        db.session.add(source);db.session.flush()
    story=Story(title=result['title'],location='Busca livre',region=MANUAL_REGION,category='Notícias',score=0,state='research')
    db.session.add(story);db.session.flush()
    db.session.add(Article(story_id=story.id,source_id=source.id,url=url,title=result['title'],summary=result['summary'],
                           published_at=date_from(result['published']) if result['published'] else None,
                           evidence='Reportagem escolhida pela redação em busca manual; fora do monitoramento automático.',
                           digest=sha256(url.encode()).hexdigest()))
    db.session.flush();audit('busca_manual_selecionada',actor=actor,story=story.id,details=url[:300])
    return story

def run_manual_search():
    if not enabled():return
    job=db.session.execute(select(ManualSearch).where(ManualSearch.state=='pending').order_by(ManualSearch.id)
                           .limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
    if not job:return
    state=db.session.get(SiteChat,(job.actor_id,job.scope));user=db.session.get(User,job.actor_id)
    if (not user or not user.is_admin or not user.is_active or not state or
            state.step!='radar_search_wait' or state.data.get('search_id')!=job.id or now()-job.created>timedelta(minutes=15)):
        job.state='cancelled';db.session.commit();return
    config=None
    if job.scope:
        from .group_delivery import group_settings
        try:config=group_settings()
        except EditorialError:return
        if not config.get('enabled') or config.get('group_id')!=job.scope:
            job.state='cancelled';db.session.commit();return
        if not config.get('connected'):return
    phone=phone_number(user.whatsapp)
    if not phone:job.state='cancelled';db.session.commit();return
    ident,actor,scope,query,days=job.id,job.actor_id,job.scope,job.term,job.days
    job.state='running';db.session.commit()
    try:results=search_news(query,days);error=None
    except (FetchError,ValueError) as exc:results=[];error=str(exc)[:300]
    job=db.session.get(ManualSearch,ident,populate_existing=True)
    state=db.session.execute(select(SiteChat).where(SiteChat.actor_id==actor,SiteChat.scope==scope)
                             .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    job.results=results;job.error=error;job.state='error' if error else 'ready';job.finished=now()
    if not state or state.step!='radar_search_wait' or state.data.get('search_id')!=ident:
        db.session.commit();return
    user=db.session.get(User,actor,populate_existing=True)
    if not user or not user.is_active or not user.is_admin:job.state='cancelled';db.session.commit();return
    from .wa_radar_menu import show_results
    text=show_results(state,job,actor,scope,0)
    message={'id':'3EB0'+token_hex(9).upper(),'text':text}
    db.session.commit()
    try:
        if config:
            gateway('POST','/radar/group/reply',json={'group_id':scope,'revision':config['revision'],'messages':[message]})
        else:gateway('POST','/radar/reply',json={'phone':phone,'messages':[message]})
    except EditorialError:pass
