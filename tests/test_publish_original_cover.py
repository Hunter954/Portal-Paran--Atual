import base64,hashlib,re
from pathlib import Path
import pytest
from app.models import Post,db
from app.radar.models import DraftMedia
from app.radar.editorial import EditorialError,latest_draft
from app.radar.wa_cover import run_cover_search
from app.radar.wa_cms import publish_post
from app.radar.group_delivery import portal_cover
from test_radar import portal
from test_original_cover import prepare
from test_whatsapp_editorial import provision

def test_source_photo_survives_preview_publicar_public_html_and_share_image(portal,monkeypatch):
    send,group=provision(portal,monkeypatch)
    app,actor,story,initial,job,delivered=prepare(portal,monkeypatch)
    raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5xkAAAAASUVORK5CYII=')
    digest=hashlib.sha256(raw).hexdigest();path='/media/radar/'+digest+'.png'
    local=Path(app.config['MEDIA_ROOT'])/path.removeprefix('/media/');local.parent.mkdir(parents=True,exist_ok=True);local.write_bytes(raw)
    photo={'path':path,'url':'https://source.example.org/original.png','credit':'Bruno Fernandes','description':'Rua após enchente','license':None}
    monkeypatch.setattr('app.radar.reporting.report_sources',lambda *a,**k:([{}],photo))
    run_cover_search();assert delivered[0]['cover']=='https://paranaatual.com.br'+path
    card=delivered[-1]['id']
    result=send('PUBLICAR',card).json
    assert 'publicada' in result['reply'],result
    post=Post.query.one();draft=latest_draft(story.id)
    assert post.featured_image==path and post.image_credit=='Bruno Fernandes'
    assert db.session.get(DraftMedia,draft.id).license is None
    html=app.test_client().get('/p/'+post.slug,base_url='https://paranaatual.com.br').get_data(as_text=True)
    assert '<img src="'+path+'"' in html and 'Foto: Bruno Fernandes' in html
    assert '<meta property="og:image" content="https://paranaatual.com.br'+path+'"' in html
    public=app.test_client().get(path)
    assert public.status_code==200 and public.data==raw
    assert portal_cover(post)=='https://paranaatual.com.br'+path

@pytest.mark.parametrize('image',[None,'/static/img/logo-parana-atual.svg'])
def test_cms_does_not_publish_a_logo_when_the_photo_is_missing(portal,image):
    app,actor=portal
    post=Post(title='Matéria local',slug='local',content_html='<p>Texto da matéria.</p>',featured_image=image)
    db.session.add(post);db.session.flush()
    with pytest.raises(EditorialError,match='foto da matéria'):publish_post(post,actor,'12345678@g.us')
    assert post.published_at is None
