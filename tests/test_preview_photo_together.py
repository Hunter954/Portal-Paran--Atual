from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from app.models import db,Post
from app.radar.models import Story,Article,Job,DraftMedia,ChatDraftReply,now
from app.radar.editorial import run_job,latest_draft,save_draft
from app.radar.reporting import report_sources
from app.radar.wa_delivery import send_pending_draft_reply
from app.radar.wa_editorial import draft_cards
from test_radar import portal
from test_whatsapp_editorial import provision,seed,GROUP
from test_original_cover import photo

def output(article):
    return {'titulo':'Notícia confirmada','resumo':'Informações da reportagem','corpo':'A reportagem confirmou os fatos nesta terça-feira.',
            'fatos':[{'fonte':article.id,'texto':'Informação confirmada na fonte'}],'pendencias':[]}

def test_initial_generation_attaches_photo_and_sends_one_image_with_text_and_credit(portal,monkeypatch):
    send,group=provision(portal,monkeypatch);seed(1)
    card=send('RADAR LISTAR 1').json['messages'][0];send('1',card['id'])
    article=Article.query.one();downloaded=Future();downloaded.set_result(photo())
    def read(sources,**options):
        assert options['photo_executor'] is not None
        return sources,downloaded
    monkeypatch.setattr('app.radar.editorial.report_sources',read)
    monkeypatch.setattr('app.radar.editorial.generate',lambda sources:output(article))
    run_job(Job.query.one());draft=latest_draft(article.story_id)
    assert db.session.get(DraftMedia,draft.id).url==photo()['url']
    monkeypatch.setattr('app.radar.wa_delivery.enabled',lambda:True)
    monkeypatch.setattr('app.radar.wa_delivery.group_settings',lambda:group)
    sent=[]
    monkeypatch.setattr('app.radar.wa_delivery.gateway',lambda *a,**kw:sent.extend(kw['json']['messages']) or {'accepted':True})
    send_pending_draft_reply()
    assert len(sent)==1 and sent[0]['cover'].endswith(photo()['path'])
    for value in ['Prévia','Notícia confirmada','A reportagem confirmou',photo()['credit'],'*1* PUBLICAR']:
        assert value in sent[0]['text']
    assert ChatDraftReply.query.one().state=='sent' and Post.query.count()==0

def test_original_page_is_fetched_once_and_photo_download_overlaps_text(portal,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app,_=portal;started=Event();finish=Event()
    url='https://source.example.org/noticia';html=b'<article><p>A reportagem confirmou os fatos na cidade e apresentou todos os detalhes da noticia.</p><figure><img src="https://source.example.org/original.jpg"><figcaption>Cena original - Foto: Fotografa</figcaption></figure></article>'
    page=SimpleNamespace(url=url,status=200,body=html,headers={'content-type':'text/html'})
    fetch=Mock();fetch.get.return_value=page
    monkeypatch.setattr('app.radar.reporting.PublicFetcher',lambda:fetch)
    def image(_fetch,candidates):
        assert candidates[0][0]=='https://source.example.org/original.jpg'
        started.set();assert finish.wait(2);return photo()
    monkeypatch.setattr('app.radar.reporting.save_photo',image)
    with ThreadPoolExecutor(max_workers=1) as executor:
        sources,pending=report_sources([{'url':url}],photo_executor=executor)
        assert started.wait(2) and not pending.done()
        assert 'reportagem confirmou' in sources[0]['texto']
        finish.set();assert pending.result(timeout=2)==photo()
    fetch.get.assert_called_once_with(url)

def test_generation_does_not_attach_photo_after_story_is_changed(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1);article=Article.query.one()
    card=send('RADAR LISTAR 1').json['messages'][0];send('1',card['id'])
    monkeypatch.setattr('app.radar.editorial.report_sources',lambda sources,**kw:(sources,photo()))
    def changed(sources):
        story=Story.query.one();save_draft(story,{'titulo':'Revisão humana','resumo':'','corpo':'Texto revisado.'},portal[1]);db.session.commit()
        return output(article)
    monkeypatch.setattr('app.radar.editorial.generate',changed)
    run_job(Job.query.one());assert Job.query.one().state=='error'
    assert DraftMedia.query.count()==0 and latest_draft(article.story_id).title=='Revisão humana'

def test_wait_menu_shows_phase_elapsed_and_automatic_delivery(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1)
    card=send('RADAR LISTAR 1').json['messages'][0];send('1',card['id'])
    job=Job.query.one();job.state='running';job.started=now();db.session.commit()
    reply=send('1').json['reply'];assert 'Lendo a reportagem' in reply and 'automaticamente' in reply and '*2* Ver reportagem' in reply

def test_photo_failure_keeps_preview_and_actionable_options_without_random_fallback(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1);article=Article.query.one()
    card=send('RADAR LISTAR 1').json['messages'][0];send('1',card['id'])
    pending=Future();pending.set_exception(ValueError('Foto indisponível'))
    monkeypatch.setattr('app.radar.editorial.report_sources',lambda sources,**kw:(sources,pending))
    monkeypatch.setattr('app.radar.editorial.generate',lambda sources:output(article))
    run_job(Job.query.one());preview=send('1').json['messages']
    assert len(preview)==1 and not preview[0].get('cover') and 'Foto original indisponível' in preview[0]['text']
    assert 'foto da matéria ainda não está disponível' in send('1').json['reply']

def test_generation_preserves_editorial_schema_uses_four_threads_and_stays_warm(monkeypatch):
    from app.radar.editorial import generate
    monkeypatch.setenv('RADAR_OLLAMA_URL','http://local.test')
    payloads=[]
    paragraphs=['A celebração religiosa foi anunciada pelo sacerdote por meio de suas redes sociais. O encontro acontecerá diante da unidade de saúde onde o ex-presidente recebe atendimento médico.',
                'Segundo a reportagem, o religioso divulgou os detalhes da programação e pediu a participação dos fiéis. As informações foram publicadas nesta terça-feira.',
                'A publicação informa o local onde os participantes deverão se reunir. O convite foi feito pelo sacerdote em uma mensagem dirigida aos apoiadores.',
                'O atendimento ao ex-presidente continua sob responsabilidade da equipe médica. A notícia apresenta as informações conhecidas sobre a programação e a internação.']
    def response(*a,**kw):
        payloads.append(kw['json']);return SimpleNamespace(status_code=200,json=lambda:{'message':{'content':__import__('json').dumps({'t':'Padre anuncia missa em frente a hospital','s':'Celebração religiosa foi anunciada nas redes.','p':paragraphs})}})
    monkeypatch.setattr('app.radar.editorial.requests.Session.post',response)
    source={'id':1,'titulo':'Padre anuncia missa em frente a hospital','texto':'O padre publicou um convite nas redes. '+('A fonte traz detalhes sobre a programação prevista e o local de realização. '*8)}
    data=generate([source])
    assert data['titulo']==source['titulo'] and data['resumo']=='Celebração religiosa foi anunciada nas redes.' and data['corpo']=='\n\n'.join(paragraphs)
    assert set(payloads[0]['format']['properties'])=={'t','s','p'} and payloads[0]['options']['num_thread']==4
    assert payloads[0]['keep_alive']=='24h'

def test_cached_text_waits_for_photo_instead_of_sending_a_partial_preview(portal,monkeypatch):
    from app.radar.models import CoverSearch
    send,_=provision(portal,monkeypatch);seed(1);story=Story.query.one()
    save_draft(story,{'titulo':'Texto pronto','resumo':'','corpo':'Nota já redigida.'},portal[1]);db.session.commit()
    send('MENU');send('9');send('1');send('1')
    reply=send('1').json
    assert not reply.get('messages') and 'texto já está pronto' in reply['reply']
    assert CoverSearch.query.count()==1 and Job.query.count()==0
    assert 'Aguardando a foto original' in send('1').json['reply']

def test_cached_preview_with_photo_is_returned_immediately_without_regeneration(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1);story=Story.query.one()
    save_draft(story,{'titulo':'Texto pronto','resumo':'','corpo':'Nota já redigida.'},portal[1],photo=photo());db.session.commit()
    send('MENU');send('9');send('1');send('1')
    messages=send('1').json['messages']
    assert len(messages)==1 and messages[0].get('cover') and 'Nota já redigida.' in messages[0]['text']
    assert Job.query.count()==0
