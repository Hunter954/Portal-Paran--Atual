import base64
from datetime import timedelta
from email.utils import format_datetime
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from types import SimpleNamespace
import pytest
from app.models import db, Post
from app.radar.models import ManualSearch, SiteChat, Story, Source, Job, Draft, DraftMedia, MANUAL_REGION, now
from app.radar.fetch import FetchError
from app.radar.manual_search import search_news, request_search, selected_search, import_result, run_manual_search
from app.radar.wa_radar_menu import list_pr
from app.radar.editorial import EditorialError, run_job
from app.radar.worker import prefetch_job
from test_radar import portal, authenticated
from test_whatsapp_editorial import provision, seed, GROUP

def result(n=1):
    return {'title': f'Checagem {n}: é falsa a publicação sobre morte de político',
            'url': f'https://noticias.example.org/brasil/checagem-{n}',
            'source':'Redação nacional', 'summary':'A reportagem verifica uma mensagem que circula nas redes.',
            'published':now().isoformat()+'Z'}

def rss(rows):
    return ('<rss xmlns:News="https://bing.com/news"><channel>'+''.join(
        '<item><title>'+escape(row['title'])+'</title><link>'+escape(row['url'])+'</link><description>Reportagem real</description>'
        '<News:Source>Fonte nacional</News:Source><pubDate>'+row.get('date','')+'</pubDate>'
        '<News:Image>https://bing.com/irrelevante.jpg</News:Image></item>' for row in rows)+'</channel></rss>').encode()

def fetcher(body,status=200):
    return SimpleNamespace(get=lambda url:SimpleNamespace(body=body,status=status))

def test_rss_preserves_real_headlines_original_urls_dates_and_ignores_thumbnails():
    original='https://noticias.example.org/checagem?utm_source=busca'
    link='http://www.bing.com/news/apiclick.aspx?'+urlencode({'url':original})
    date=format_datetime(now().replace(tzinfo=__import__('datetime').timezone.utc))
    rows=search_news('Bolsonaro morre',fetcher=fetcher(rss([{'title':'É falsa a notícia de morte','url':link,'date':date},
        {'title':'Duplicada','url':original,'date':date},{'title':'Privada','url':'http://127.0.0.1/x','date':date}])))
    assert len(rows)==1 and rows[0]['title']=='É falsa a notícia de morte'
    assert rows[0]['url']=='https://noticias.example.org/checagem'
    assert rows[0]['source']=='Fonte nacional' and rows[0]['published'].endswith('Z')
    assert 'image' not in rows[0]

@pytest.mark.parametrize('body,status',[(b'not xml',200),(b'<html/>',200),(b'',503)])
def test_invalid_provider_responses_fail_without_fabricating_news(body,status):
    with pytest.raises(FetchError):search_news('noticias',fetcher=fetcher(body,status))

def test_period_filters_future_and_missing_dates():
    from datetime import timezone
    def date(days):return format_datetime((now()+timedelta(days=days)).replace(tzinfo=timezone.utc))
    data=rss([{'title':'Antiga','url':'https://fonte.example.org/antiga','date':date(-20)},
              {'title':'Futura','url':'https://fonte.example.org/futura','date':date(1)},
              {'title':'Sem data','url':'https://fonte.example.org/semdata'}])
    assert search_news('noticias',7,fetcher(data))==[]
    assert len(search_news('noticias',30,fetcher(data)))==1
    assert len(search_news('noticias',0,fetcher(data)))==2

def begin(send,query='Bolsonaro morre'):
    assert 'REDAÇÃO' in send('MENU').json['reply']
    assert 'Brasil e mundo' in send('9').json['reply']
    assert 'Qual assunto' in send('3').json['reply']
    assert 'Escolha o período' in send(query).json['reply']
    assert 'Consultando reportagens' in send('1').json['reply']
    return ManualSearch.query.order_by(ManualSearch.id.desc()).first()

def worker_mock(monkeypatch,group,rows):
    deliveries=[]
    monkeypatch.setattr('app.radar.manual_search.enabled',lambda:True)
    monkeypatch.setattr('app.radar.group_delivery.group_settings',lambda:group)
    monkeypatch.setattr('app.radar.manual_search.search_news',lambda *a:rows)
    monkeypatch.setattr('app.radar.manual_search.gateway',lambda *a,**kw:deliveries.extend(kw['json']['messages']) or {'accepted':True})
    return deliveries

def test_numbered_navigation_pagination_urgent_and_global_do_not_mix(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(13)
    job=ManualSearch(actor_id=portal[1],scope=GROUP,term='Brasil',state='ready',results=[result()]);db.session.add(job);db.session.flush()
    manual=import_result(job,0,portal[1]);db.session.commit()
    send('MENU');send('9')
    first=send('1').json['reply'];assert '*6* Próxima página' in first and 'Busca livre' not in first and manual.title not in first
    second=send('6').json['reply'];assert '*7* Página anterior' in second
    assert send('7').json['reply']==first
    card=send('1').json['messages'][0];assert '*1* Preparar prévia' in card['text']
    assert 'Fonte local' in send('2').json['reply']
    assert send('8').json['reply']==first
    send('0');urgent=send('2').json['reply'];assert 'Urgentes do Paraná' in urgent
    send('0');assert 'Menu principal' in send('99').json['reply']
    assert 'REDAÇÃO' in send('0').json['reply']

def test_free_search_delivers_real_results_and_full_numbered_publish_preserves_original_photo(portal,monkeypatch):
    send,group=provision(portal,monkeypatch);job=begin(send)
    delivered=worker_mock(monkeypatch,group,[result(i) for i in range(1,8)])
    run_manual_search()
    assert job.state=='ready' and len(delivered)==1
    assert '*6* Próxima página' in delivered[0]['text'] and 'Checagem' in delivered[0]['text']
    assert Post.query.count()==0 and Story.query.count()==0
    assert 'Checagem 6:' in send('6').json['reply'];send('7')
    assert 'Ver reportagem original' in send('1').json['reply']
    assert result()['url'] in send('2').json['reply']
    assert 'lendo a reportagem' in send('1').json['reply']
    story=Story.query.one();article=story.articles[0]
    assert story.title==result()['title'] and story.title!=job.term and story.region==MANUAL_REGION
    assert not article.source.active and article.source.archived
    assert prefetch_job() is None
    raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xkAAAAASUVORK5CYII=')
    path='/media/radar/'+__import__('hashlib').sha256(raw).hexdigest()+'.png';local=Path(portal[0].config['MEDIA_ROOT'])/path.removeprefix('/media/');local.parent.mkdir(parents=True,exist_ok=True);local.write_bytes(raw)
    photo={'path':path,'url':'https://noticias.example.org/original.png','credit':'Fotógrafa da reportagem','description':'Foto original','license':None}
    monkeypatch.setattr('app.radar.editorial.generate',lambda sources:{'titulo':result()['title'],'resumo':'Checagem da redação.',
        'corpo':'A reportagem desmente a alegação que circulou nas redes.','fatos':[{'fonte':article.id,'texto':'A alegação é falsa.'}],'pendencias':[]})
    run_job(Job.query.one());draft=Draft.query.one()
    db.session.add(DraftMedia(draft_id=draft.id,**photo));db.session.commit()
    preview=send('1').json['messages'];assert preview[0]['cover'].endswith(path)
    assert '*1* PUBLICAR MATÉRIA' in preview[-1]['text']
    assert 'publicada' in send('1').json['reply']
    post=Post.query.one();assert post.featured_image==path and post.image_credit==photo['credit']
    html=portal[0].test_client().get('/p/'+post.slug).get_data(as_text=True)
    assert path in html and photo['credit'] in html
    assert portal[0].test_client().get(path).data==raw

def test_manual_result_does_not_pollute_automatic_lists_or_dashboard(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);job=ManualSearch(actor_id=portal[1],scope=GROUP,term='Brasil',state='ready',results=[result()]);db.session.add(job);db.session.flush()
    story=import_result(job,0,portal[1]);db.session.commit()
    assert import_result(job,0,portal[1]).id==story.id and Story.query.count()==1
    assert prefetch_job() is None
    assert 'Nenhuma pauta' in send('RADAR LISTAR 10').json['reply']
    client=authenticated(*portal)
    html=client.get('/admin/radar?tab=all').get_data(as_text=True)
    assert story.title not in html and 'Busca livre</option>' not in html

def test_cancelled_navigation_and_new_search_never_receive_old_results(portal,monkeypatch):
    send,group=provision(portal,monkeypatch);first=begin(send)
    deliveries=worker_mock(monkeypatch,group,[result()])
    send('0');run_manual_search();assert first.state=='cancelled' and not deliveries
    second=begin(send,'chuvas Brasil')
    def during_fetch(*a):
        state=db.session.get(SiteChat,(portal[1],GROUP));state.step='radar';state.data={'radar_ui':True};db.session.commit()
        return [result()]
    monkeypatch.setattr('app.radar.manual_search.search_news',during_fetch)
    run_manual_search();assert second.state=='ready' and not deliveries
    assert db.session.get(SiteChat,(portal[1],GROUP)).step=='radar'
    assert 'chuvas Brasil' in send('4').json['reply']
    assert 'Checagem' in send('1').json['reply']

def test_provider_error_has_retry_options_and_ownership_guard(portal,monkeypatch):
    send,group=provision(portal,monkeypatch);job=begin(send)
    deliveries=worker_mock(monkeypatch,group,[])
    def fail(*a):raise FetchError('timeout')
    monkeypatch.setattr('app.radar.manual_search.search_news',fail)
    run_manual_search();assert job.state=='error' and '*1* Tentar novamente' in deliveries[0]['text']
    assert 'Escolha o período' in send('1').json['reply']
    with pytest.raises(EditorialError):selected_search(job.id,portal[1],'different@g.us')
    with pytest.raises(EditorialError):selected_search(job.id,portal[1]+1,GROUP)

def test_quoted_number_selects_exact_story_and_edit_menu_needs_no_command(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(2)
    cards=send('RADAR LISTAR 2').json['messages']
    assert 'lendo a reportagem' in send('1',cards[1]['id']).json['reply']
    assert Job.query.one().story_id==Story.query.order_by(Story.id).first().id
    monkeypatch.setattr('app.radar.editorial.generate',lambda sources:{'titulo':'Título original','resumo':'Resumo',
        'corpo':'Corpo da notícia.','fatos':[],'pendencias':[]})
    run_job(Job.query.one());preview=send('1').json['messages'][-1]
    assert 'Editar prévia' in send('5',preview['id']).json['reply']
    assert 'novo título' in send('1').json['reply']
    edited=send('Título revisado').json['messages'][-1]
    assert 'Título revisado' in edited['text'] and '*1* PUBLICAR' in edited['text']
    assert 'mudou' in send('1',preview['id']).json['reply']

def test_unquoted_image_is_accepted_only_after_photo_option(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1)
    card=send('RADAR LISTAR 1').json['messages'][0];send('1',card['id'])
    monkeypatch.setattr('app.radar.editorial.generate',lambda sources:{'titulo':'Notícia','resumo':'Resumo','corpo':'Texto.', 'fatos':[],'pendencias':[]})
    run_job(Job.query.one());send('1');send('5');send('4')
    assert send('',image_offer=True).json['need_image']
    image={'mime':'image/jpeg','data':base64.b64encode(b'\xff\xd8\xfflocal-original-photo').decode()}
    cards=send('Crédito da fotógrafa',image=image).json['messages']
    assert cards[0]['cover'] and 'Crédito da fotógrafa' in cards[0]['text']
    assert DraftMedia.query.one().credit=='Crédito da fotógrafa'
