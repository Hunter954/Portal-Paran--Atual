import json,re
from types import SimpleNamespace
from unittest.mock import Mock
from app.models import db,Post
from app.radar.editorial import generate,EditorialError,save_draft,published_text,latest_draft,source_notes
from app.radar.models import Story,Job,ChatCard,SiteChat
from app.radar.reporting import report_sources,article_paragraphs
from app.radar.wa_editorial import draft_cards,legacy_draft
from bs4 import BeautifulSoup
import pytest
from test_radar import portal
from test_original_cover import photo
from test_whatsapp_editorial import provision,seed,GROUP

PARAGRAPHS=[
 'A chuva que atingiu a região provocou transtornos em diferentes municípios e mobilizou equipes de atendimento. As ocorrências foram registradas pelas autoridades locais, que acompanham os danos e reúnem informações sobre as famílias atingidas. O levantamento continua em andamento, segundo o relato divulgado pela fonte responsável pelo balanço.',
 'Os moradores de áreas alagadas precisaram aguardar o trabalho das equipes para deixar os imóveis. O atendimento envolveu profissionais dos serviços públicos e voluntários que auxiliaram no deslocamento das pessoas. A reportagem informa que a prioridade naquele momento era prestar assistência a quem permanecia em situação de risco.',
 'Os dados apresentados pela administração municipal ainda não representam um balanço definitivo. As equipes verificam as informações recebidas e continuam percorrendo os bairros para identificar necessidades. A extensão dos prejuízos depende dessa avaliação, que deve considerar tanto os imóveis atingidos quanto as condições das vias de acesso.',
 'O temporal também afetou os serviços de energia elétrica na região. A concessionária mobilizou equipes para verificar a rede e executar os reparos necessários. O trabalho depende das condições encontradas em cada ponto, enquanto os moradores aguardam o restabelecimento do fornecimento nas localidades atingidas pela interrupção.',
 'As autoridades mantêm o acompanhamento dos registros e diferenciam as ocorrências confirmadas das informações ainda em apuração. Os levantamentos divulgados ao longo do atendimento podem apresentar alterações conforme novas verificações sejam concluídas. A fonte informa que a atualização dos dados segue sob responsabilidade das equipes que atuam na região.',
 'A situação das famílias atendidas continua sendo acompanhada pelos órgãos envolvidos na resposta ao temporal. O relato reúne as informações que estavam disponíveis na divulgação do balanço, mas não apresenta um levantamento final dos prejuízos. Novos dados dependem da conclusão das avaliações realizadas nos locais afetados.',
 'O documento detalha o atendimento prestado aos moradores e as ações que permanecem em execução. As equipes trabalham em diferentes frentes para identificar danos, garantir o acesso às áreas atingidas e verificar a necessidade de apoio. O encerramento dos trabalhos depende das condições observadas durante o acompanhamento das ocorrências.',
 'Os registros citados na reportagem mostram que a resposta ao temporal envolve diferentes órgãos. Cada instituição apresenta informações sobre sua área de atuação e atualiza os dados conforme o atendimento avança. A divulgação de um balanço consolidado depende da reunião dessas informações e da confirmação das ocorrências registradas.'
]

def source():
    return {'id':1,'titulo':'Temporal mobiliza equipes de atendimento','fonte':'Órgão local','publicado':'2026-11-24T19:00:00',
            'texto':('O informe municipal registra alagamentos em vias e atendimento a moradores na região. A chuva levou famílias a procurar apoio dos serviços públicos. A companhia de energia informou que profissionais foram mobilizados para verificar os danos à rede e executar os reparos. As autoridades acompanham as ocorrências e ainda não divulgaram um balanço definitivo dos prejuízos.\n'*9)+
            'DADO AO FINAL: a concessionária registrou 174,7 mil unidades sem energia.'}

def respond(monkeypatch,paragraphs=PARAGRAPHS,reason='stop'):
    monkeypatch.setenv('RADAR_OLLAMA_URL','http://local.test')
    payloads=[]
    def send(*a,**kw):
        payloads.append(kw)
        return SimpleNamespace(status_code=200,json=lambda:{'done_reason':reason,'message':{'content':json.dumps({'t':'Temporal mobiliza atendimento na região','s':'Equipes verificam danos e auxiliam moradores','p':paragraphs})}})
    monkeypatch.setattr('app.radar.editorial.requests.Session.post',send)
    return payloads

def test_generation_uses_late_facts_and_returns_developed_article(monkeypatch):
    calls=respond(monkeypatch)
    result=generate([source()])
    assert len(result['corpo'].split())>=350 and len(result['corpo'].split('\n\n'))==8
    request=calls[0]['json'];assert '174,7 mil' in request['messages'][1]['content']
    assert request['options']['num_predict']>1000 and request['options']['num_ctx']>=4096
    assert calls[0]['timeout'][1]>=180
    assert result['fatos'][-1]['texto'].startswith('DADO AO FINAL')

@pytest.mark.parametrize('paragraphs',[PARAGRAPHS[:2],[PARAGRAPHS[0]]*8,[p.split('.')[0]+'.' for p in PARAGRAPHS]])
def test_short_or_repeated_note_is_never_delivered_as_a_full_article(monkeypatch,paragraphs):
    respond(monkeypatch,paragraphs)
    with pytest.raises(EditorialError,match='curta|repetiu'):
        generate([source()])

def test_token_limit_does_not_silently_return_a_cut_article(monkeypatch):
    respond(monkeypatch,reason='length')
    with pytest.raises(EditorialError,match='interrompida'):
        generate([source()])

def test_long_article_keeps_factual_numbers_checked(monkeypatch):
    respond(monkeypatch,[*PARAGRAPHS[:-1],PARAGRAPHS[-1]+' Foram confirmados 9999 casos.'])
    with pytest.raises(EditorialError,match='números'):
        generate([source()])

def test_source_extraction_does_not_stop_at_a_related_link_or_clip_after_intro(portal,monkeypatch):
    url='https://source.example.org/reportagem'
    paragraphs=['Texto de abertura com todas as informações necessárias sobre a ocorrência registrada na região.']
    paragraphs+=['Informações complementares sobre os atendimentos e os danos registrados nos bairros. '+str(i) for i in range(60)]
    paragraphs+=['DADO FINAL: a empresa informou que 174,7 mil unidades estavam sem energia elétrica naquele momento.']
    html='<article>'+''.join('<p>'+p+'</p>'+('<p>Leia também</p><p><a href="/outra">Uma chamada longa que apresenta outra notícia e não deve entrar no corpo da reportagem extraída</a></p>' if i==1 else '') for i,p in enumerate(paragraphs))+'</article>'
    client=Mock();client.get.return_value=SimpleNamespace(url=url,status=200,headers={'content-type':'text/html'},body=html.encode())
    monkeypatch.setattr('app.radar.reporting.PublicFetcher',lambda:client)
    sources,_=report_sources([{'url':url}],include_photo=False)
    assert len(sources[0]['texto'])>3300 and '174,7 mil' in sources[0]['texto']
    assert 'Uma chamada longa' not in sources[0]['texto']

def test_subheadings_render_safely_as_headings_in_public_article():
    html=published_text('Abertura da reportagem.\n\n## Situação nas cidades\n\nDetalhes confirmados.\n\n## <script>ataque</script>\n\nÚltima informação.',[])
    assert '<h2>Situação nas cidades</h2>' in html
    assert '<h2>&lt;script&gt;ataque&lt;/script&gt;</h2>' in html and '<script>' not in html

def test_long_whatsapp_preview_keeps_all_text_photo_credit_and_current_actions(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1);story=Story.query.one()
    body='\n\n'.join([*PARAGRAPHS,'## Serviços afetados',*PARAGRAPHS])
    draft=save_draft(story,{'titulo':'Reportagem completa','resumo':'Balanço da região','corpo':body},portal[1],photo=photo());db.session.commit()
    messages=draft_cards(story,draft,portal[1],GROUP)
    assert len(messages)>1 and messages[0].get('cover')
    assert 'Reportagem completa' in messages[0]['text'] and photo()['credit'] in messages[0]['text'] and PARAGRAPHS[0] in messages[0]['text']
    assert sum(bool(m.get('cover')) for m in messages)==1 and all(len(m['text'])<=3100 for m in messages)
    combined='\n'.join(m['text'] for m in messages)
    for p in PARAGRAPHS:assert combined.count(p)==2
    assert '*Serviços afetados*' in combined and '*1* PUBLICAR' in messages[-1]['text']
    for m in messages:
        card=db.session.get(ChatCard,m['id']);assert card.version==draft.version and card.story_id==story.id
    assert db.session.get(SiteChat,(portal[1],GROUP)).data['quote_id']==messages[-1]['id']

def test_old_automatic_note_is_regenerated_but_complete_human_edits_are_preserved(portal,monkeypatch):
    send,_=provision(portal,monkeypatch);seed(1);story=Story.query.one()
    draft=save_draft(story,{'titulo':'Nota antiga','resumo':'','corpo':'Texto curto sobre o evento.'},portal[1],model='modelo:radar-pt-br-v4',photo=photo());db.session.commit()
    assert legacy_draft(draft)
    send('MENU');send('9');send('1');send('1')
    assert 'lendo a reportagem' in send('1').json['reply'] and Job.query.count()==1
    complete=save_draft(story,{'titulo':'Revisão completa','resumo':'','corpo':'\n\n'.join(PARAGRAPHS)},portal[1]);db.session.commit()
    assert not legacy_draft(complete)

def test_originality_check_distinguishes_a_shared_factual_phrase_from_copied_passages():
    from app.radar.editorial import copied_passage
    fact=' '.join('nome'+str(i) for i in range(22))
    original=' '.join('fonte'+str(i) for i in range(150))
    authored=' '.join('redacao'+str(i) for i in range(400))
    assert not copied_passage(fact+' '+authored,fact+' '+original)
    assert copied_passage(original+' '+authored,original)
    assert copied_passage(' '.join(original.split()[:45])+' '+authored,original)

def test_generation_revises_copied_paragraphs_once_and_keeps_the_complete_article(monkeypatch):
    monkeypatch.setenv('RADAR_OLLAMA_URL','http://local.test')
    base=('O informe municipal registra alagamentos em vias e atendimento a moradores na região. A chuva levou famílias a procurar apoio dos serviços públicos. '
          'A companhia de energia informou que profissionais foram mobilizados para verificar os danos à rede e executar os reparos. '
          'As autoridades acompanham as ocorrências e ainda não divulgaram um balanço definitivo dos prejuízos.')
    originals=[base+' O registro consta no documento sob a referência '+chr(65+i)+'.' for i in range(8)]
    responses=[{'t':'Temporal mobiliza atendimento','s':'Situação acompanhada pelas equipes','p':originals},{'p':PARAGRAPHS}]
    calls=[]
    def send(*a,**kw):
        calls.append(kw);value=responses.pop(0)
        return SimpleNamespace(status_code=200,json=lambda:{'done_reason':'stop','message':{'content':json.dumps(value)}})
    monkeypatch.setattr('app.radar.editorial.requests.Session.post',send)
    original={**source(),'texto':'\n\n'.join(originals)}
    result=generate([original])
    assert len(calls)==2 and len(result['corpo'].split())>=300
    from app.radar.editorial import copied_passage
    assert not copied_passage(result['corpo'],original['texto'])

def test_originality_revision_has_one_attempt_instead_of_an_endless_retry(monkeypatch):
    calls=[];monkeypatch.setenv('RADAR_OLLAMA_URL','http://local.test')
    def send(*a,**kw):
        calls.append(kw);data={'t':'Temporal mobiliza atendimento','s':'Balanço local','p':PARAGRAPHS} if len(calls)==1 else {'p':PARAGRAPHS}
        return SimpleNamespace(status_code=200,json=lambda:{'done_reason':'stop','message':{'content':json.dumps(data)}})
    monkeypatch.setattr('app.radar.editorial.requests.Session.post',send)
    with pytest.raises(EditorialError,match='semelhante'):
        generate([{**source(),'texto':'\n\n'.join(PARAGRAPHS)}])
    assert len(calls)==2

def test_duplicate_headline_from_model_is_removed_without_cutting_complete_paragraphs(monkeypatch):
    title='Temporal mobiliza atendimento na região'
    respond(monkeypatch,[title,*PARAGRAPHS[:6]])
    result=generate([source()])
    assert not result['corpo'].startswith(title)
    assert result['corpo']=='\n\n'.join(PARAGRAPHS[:6]) and len(result['corpo'].split())>=260
