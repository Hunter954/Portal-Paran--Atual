import pytest
from unittest.mock import Mock
from bs4 import BeautifulSoup
from app.models import Post,User,db
from app.radar.editorial import EditorialError,latest_draft,save_draft
from app.radar.models import Source,Story,Article,DraftMedia,CoverSearch,SiteCard
from app.radar.reporting import photo_candidates,structured_data
from app.radar.wa_cover import run_cover_search,use_photo
from test_radar import portal

GROUP='12345678@g.us'

def prepare(portal,monkeypatch):
    app,actor=portal
    db.session.get(User,actor).whatsapp='+5545990000011'
    source=Source(name='Fonte da reportagem',url='https://source.example.org/feed',kind='rss')
    story=Story(title='Chuvas no Paraná',location='Paraná',region='Paraná',category='Cidades',score=80)
    story.articles=[Article(source=source,title=story.title,url='https://source.example.org/reportagem',evidence='Paraná',digest='abc')]
    db.session.add(story);db.session.flush()
    draft=save_draft(story,{'titulo':story.title,'resumo':'Temporal','corpo':'Texto revisado sobre o temporal.'},actor)
    job=CoverSearch(actor_id=actor,scope=GROUP,term='Paraná',location='Paraná',target={'story':story.id,'version':draft.version})
    db.session.add(job);db.session.commit()
    config={'connected':True,'enabled':True,'group_id':GROUP,'revision':'test'}
    monkeypatch.setattr('app.radar.wa_cover.enabled',lambda:True)
    monkeypatch.setattr('app.radar.wa_cover.group_settings',lambda:config)
    monkeypatch.setattr('app.radar.wa_cover.search_images',Mock(side_effect=AssertionError('Topic search must never be used for previews')))
    delivered=[]
    def gateway(*args,**kwargs):delivered.extend(kwargs['json']['messages']);return {'accepted':True}
    monkeypatch.setattr('app.radar.wa_cover.gateway',gateway)
    return app,actor,story,draft,job,delivered

def photo():return {'path':'/media/radar/real.jpg','url':'https://cdn.example.org/original.jpg','credit':'Fotógrafo da fonte','description':'Rua atingida pela enchente','license':None}

def test_preview_uses_original_source_attaches_and_delivers_same_photo(portal,monkeypatch):
    app,actor,story,draft,job,delivered=prepare(portal,monkeypatch)
    fetch=Mock(return_value=([{}],photo()));monkeypatch.setattr('app.radar.reporting.report_sources',fetch)
    run_cover_search()
    fetch.assert_called_once_with([{'url':story.articles[0].url}])
    current=latest_draft(story.id);media=db.session.get(DraftMedia,current.id)
    assert current.version==draft.version+1 and current.body==draft.body
    assert media.url==photo()['url'] and media.credit==photo()['credit']
    assert delivered[0]['cover']=='https://paranaatual.com.br'+photo()['path']
    assert photo()['credit'] in delivered[0]['text'] and 'Prévia' in delivered[-1]['text']
    assert job.state=='sent'

def test_missing_original_reports_failure_without_generic_image(portal,monkeypatch):
    app,actor,story,draft,job,delivered=prepare(portal,monkeypatch)
    monkeypatch.setattr('app.radar.reporting.report_sources',lambda *a,**k:([{}],None))
    run_cover_search()
    assert 'reportagem original' in delivered[0]['text']
    assert not any(m.get('cover') for m in delivered)
    assert db.session.get(DraftMedia,draft.id) is None

def test_stale_preview_search_cannot_attach_or_send_photo(portal,monkeypatch):
    app,actor,story,draft,job,delivered=prepare(portal,monkeypatch)
    save_draft(story,{'titulo':story.title,'resumo':'Novo','corpo':'Texto editado pelo autor.'},actor);db.session.commit()
    fetch=Mock(side_effect=AssertionError('Stale jobs must not fetch'))
    monkeypatch.setattr('app.radar.reporting.report_sources',fetch)
    run_cover_search();assert job.state=='cancelled' and delivered==[]

def test_edit_during_download_does_not_receive_outdated_photo(portal,monkeypatch):
    app,actor,story,draft,job,delivered=prepare(portal,monkeypatch)
    def fetch(*a,**k):
        save_draft(story,{'titulo':story.title,'resumo':'Novo','corpo':'Nova versão.'},actor);db.session.commit()
        return [{}],photo()
    monkeypatch.setattr('app.radar.reporting.report_sources',fetch)
    run_cover_search();assert job.state=='cancelled' and not delivered
    assert db.session.get(DraftMedia,latest_draft(story.id).id) is None

def test_old_generic_search_cards_cannot_be_selected(portal,monkeypatch):
    app,actor,story,draft,job,delivered=prepare(portal,monkeypatch)
    card=SiteCard(message_id='old-topic-search',actor_id=actor,scope=GROUP,kind='photo',data={'target':job.target,'photo':photo()})
    with pytest.raises(EditorialError,match='reportagem original'):use_photo(card,'USAR FOTO',actor,GROUP)

def test_cms_search_without_original_link_does_not_fall_back_to_topics(portal,monkeypatch):
    from app.radar.wa_cms import preview_version
    app,actor,story,draft,job,delivered=prepare(portal,monkeypatch)
    post=Post(title='Matéria local',slug='local',content_html='<p>Texto.</p>',source='local')
    db.session.add(post);db.session.flush();job.target={'post':post.id,'version':preview_version(post)};db.session.commit()
    fetch=Mock(side_effect=AssertionError('No source must not trigger a search'));monkeypatch.setattr('app.radar.reporting.report_sources',fetch)
    run_cover_search();assert not any(m.get('cover') for m in delivered) and 'link' in delivered[0]['text']

def test_source_parser_keeps_caption_credit_and_ignores_related_figures():
    page=BeautifulSoup('''<article><aside><figure><img src="/unrelated.jpg"><figcaption>Outra notícia — Foto: Outro</figcaption></figure></aside>
        <figure class="content-media-figure"><amp-img src="https://cdn.example.org/flood.jpg"></amp-img>
        <figcaption>1 de 1 Rua atingida pela enchente — Foto: Bruno Fernandes</figcaption></figure></article>''','html.parser')
    rows=list(photo_candidates(page,'https://source.example.org/reportagem',{}))
    assert len(rows)==1 and rows[0][0]=='https://cdn.example.org/flood.jpg' and rows[0][1]=='Bruno Fernandes'

def test_missing_photo_author_is_not_replaced_by_article_author():
    page=BeautifulSoup('''<script type="application/ld+json">{"@type":["NewsArticle"],"image":"https://cdn.example.org/flood.jpg","author":{"name":"Autor do texto"}}</script>''','html.parser')
    rows=list(photo_candidates(page,'https://source.example.org/reportagem',structured_data(page)))
    assert rows[0][1]=='Crédito não informado na reportagem original' and 'Autor do texto' not in rows[0][1]
