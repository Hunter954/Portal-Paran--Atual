"""Numbered navigation for the Radar, article previews and unrestricted searches."""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from ..models import db
from .models import SiteChat,Story,ManualSearch,MANUAL_REGION,now
from .editorial import EditorialError,latest_draft
from .geography import normalize

HOME=("📡 *RADAR DE NOTÍCIAS*\n\n*1* Pautas do Paraná\n*2* Urgentes do Paraná\n"
      "*3* Buscar notícia por assunto — Brasil e mundo\n*4* Minhas buscas\n\n*0* Menu principal")
PREVIEW=("*1* PUBLICAR MATÉRIA\n*2* REFAZER TEXTO\n*3* Buscar foto da reportagem\n"
         "*4* Ver fontes\n*5* Editar texto ou foto\n*6* Marcar apuração como conferida\n*8* Voltar à lista\n*0* Menu do Radar")
PERIOD="*1* Últimos 7 dias\n*2* Últimos 30 dias\n*3* Sem limite de data\n*0* Menu do Radar"
STEPS={'radar','radar_list','radar_story','radar_search','radar_period','radar_search_wait','radar_results',
       'radar_history','radar_result','radar_preview','radar_wait','radar_edit','radar_edit_text','radar_photo'}

def state_for(actor,group):
    from .wa_console import state_for as get
    return get(actor,group)

def step(state,name,**data):
    state.step=name;state.data={'radar_ui':True,**data};state.updated=now()

def enter(state,actor,group):
    from .wa_editorial import chat_session
    chat_session(actor,group).mode='menu'
    step(state,'radar');return HOME

def date_label(raw):
    if not raw:return 'Data não informada'
    value=datetime.fromisoformat(raw.replace('Z','+00:00'))
    if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y %H:%M')

def current_options(state):
    if state.step=='radar':return HOME
    if state.step=='radar_preview':return PREVIEW
    if state.step=='radar_period':return PERIOD
    if state.step=='radar_search_wait':return '*1* Verificar resultado\n*2* Fazer outra busca\n*0* Menu do Radar'
    if state.step in {'radar_search','radar_edit_text','radar_photo'}:return '*0* Voltar ao Radar'
    if state.step=='radar_wait':return '*1* Ver andamento\n*2* Ver reportagem original\n*8* Voltar à lista\n*0* Menu do Radar'
    if state.step=='radar_edit':return '*1* Título\n*2* Resumo\n*3* Texto completo\n*4* Enviar foto\n*5* Voltar à prévia\n*0* Menu do Radar'
    if state.step=='radar_story':return '*1* Preparar prévia\n*2* Ver fontes\n*8* Voltar à lista\n*0* Menu do Radar'
    if state.step=='radar_result':return '*1* Preparar matéria desta reportagem\n*2* Ver reportagem original\n*8* Voltar aos resultados\n*9* Fazer outra busca\n*0* Menu do Radar'
    if state.step=='radar_results':return '*6* Mais resultados\n*7* Página anterior\n*9* Fazer outra busca\n*0* Menu do Radar'
    return '*8* Atualizar lista\n*0* Menu do Radar'

def search_prompt(state):
    step(state,'radar_search')
    return ('🔎 *Busca livre — Brasil e mundo*\n\nQual assunto você quer pesquisar? Envie o nome de uma pessoa, '
            'um acontecimento ou uma cidade.\nA busca mostra reportagens das fontes; o texto pesquisado não é tratado como fato.\n\n*0* Menu do Radar')

def show_results(state,job,actor,group,offset=0):
    step(state,'radar_results',search_id=job.id,offset=offset)
    if job.state=='error':return 'Não consegui consultar as notícias agora.\n\n*1* Tentar novamente\n*9* Alterar assunto\n*0* Menu do Radar'
    if not job.results:return f'Nenhuma reportagem encontrada para “{job.term}” nesse período.\n\n*1* Alterar o período\n*9* Buscar outro assunto\n*0* Menu do Radar'
    rows=job.results[offset:offset+5]
    text=f'🔎 *Resultados para “{job.term}”*\nBrasil e mundo · horários de Brasília\n\n'
    text+='\n\n'.join(f"*{i}* {row['title'][:200]}\n{row['source'][:80]} · {date_label(row.get('published'))}" for i,row in enumerate(rows,1))
    text+='\n\nEscolha o número da reportagem.'
    if offset+5<len(job.results):text+='\n*6* Próxima página'
    if offset:text+='\n*7* Página anterior'
    return text+'\n*9* Fazer outra busca\n*0* Menu do Radar'

def list_pr(state,actor,group,urgent=False,page=0,cursors=None):
    cursors=cursors or [None]
    query=Story.query.filter(Story.region!=MANUAL_REGION,Story.state.in_(('new','research','production','review')),Story.articles.any())
    if urgent:query=query.filter(Story.score>=75)
    if cursors[page] is not None:query=query.filter(Story.id<cursors[page])
    rows=query.order_by(Story.id.desc()).limit(6).all();items=rows[:5]
    step(state,'radar_list',ids=[s.id for s in items],urgent=urgent,page=page,cursors=cursors,more=len(rows)>5)
    if not items:return 'Nenhuma pauta nessa fila agora.\n\n*8* Atualizar lista\n*0* Menu do Radar'
    title='Urgentes do Paraná' if urgent else 'Pautas do Paraná'
    text='📡 *'+title+'*\n\n'+'\n\n'.join(f'*{i}* #{s.id} · {s.title[:210]}\n{s.location} · {s.articles[0].source.name[:70]}' for i,s in enumerate(items,1))
    text+='\n\nEscolha o número da pauta.'
    if len(rows)>5:text+='\n*6* Próxima página'
    if page:text+='\n*7* Página anterior'
    return text+'\n*8* Atualizar lista\n*0* Menu do Radar'

def context(state):
    return {'step':state.step,'data':dict(state.data)}

def back_to_list(state,actor,group):
    previous=state.data.get('return_to') or {}
    data=previous.get('data',{})
    if previous.get('step')=='radar_results':
        from .manual_search import selected_search
        return show_results(state,selected_search(data['search_id'],actor,group),actor,group,data.get('offset',0))
    if previous.get('step')=='radar_list':
        return list_pr(state,actor,group,data.get('urgent',False),data.get('page',0),data.get('cursors'))
    return enter(state,actor,group)

def detail(state,story,actor,group,previous=None):
    from .wa_editorial import reply_card
    first=story.articles[0]
    step(state,'radar_story',story_id=story.id,return_to=previous or state.data.get('return_to'))
    text=f'*Pauta #{story.id}*\n\n*{story.title}*\n{first.summary[:650]}\n\nFonte: {first.source.name}\n{first.url}\n\n'+current_options(state)
    card=reply_card(story,text,actor,group);state.data={**state.data,'quote_id':card['id']}
    return {'messages':[card]}

def remember_preview(story,draft,actor,group,card_id):
    if not actor:return
    state=state_for(actor,group)
    selected=state.data.get('story_id')
    if state.data.get('radar_ui') and selected is not None and selected!=story.id:return
    if state.data.get('radar_ui') and state.step not in {'radar_story','radar_wait','radar_preview','radar_edit','radar_edit_text','radar_photo'}:return
    if state.step in {'radar_edit_text','radar_photo'}:
        step(state,state.step,**{k:v for k,v in state.data.items() if k not in {'radar_ui','version','quote_id'}},version=draft.version,quote_id=card_id)
        return
    step(state,'radar_preview',story_id=story.id,version=draft.version,quote_id=card_id,return_to=state.data.get('return_to'))

def history(state,actor,group):
    rows=ManualSearch.query.filter_by(actor_id=actor,scope=group or '').order_by(ManualSearch.id.desc()).limit(5).all()
    step(state,'radar_history',ids=[r.id for r in rows])
    if not rows:return 'Você ainda não fez buscas nesta conversa.\n\n*9* Fazer uma busca\n*0* Menu do Radar'
    return '🕘 *Minhas buscas*\n\n'+'\n'.join(f'*{i}* {r.term[:100]} · {len(r.results)} resultados' for i,r in enumerate(rows,1))+'\n\n*9* Nova busca\n*0* Menu do Radar'

def prepare_complete(story,actor,group):
    from .models import DraftMedia
    from .wa_editorial import request_draft,legacy_draft
    from .wa_cover import queue_draft_cover,generic_photo
    draft=latest_draft(story.id)
    if draft and draft.body.strip() and story.state=='review' and not legacy_draft(draft):
        photo=db.session.get(DraftMedia,draft.id)
        if not photo or generic_photo(photo):
            queue_draft_cover(story,draft,actor,group)
            return 'O texto já está pronto. Estou localizando a foto original para enviar a prévia completa aqui.'
    return request_draft(story,actor,group)

def handle(state,text,actor,group,quote_id=None,image=None):
    from .wa_editorial import quoted_card,radar_command,request_draft,edit_draft,save_cover_from_message
    from .manual_search import selected_search,request_search,import_result
    value=normalize(text)
    active=state.step in STEPS and state.data.get('radar_ui')
    quoted=quoted_card(quote_id,actor,group) if quote_id else None
    if value=='radar':return enter(state,actor,group)
    if value=='voltar' and active:return enter(state,actor,group)
    if value=='0' and active:
        if state.step=='radar':
            from .wa_console import GROUP_MENU,set_step
            set_step(state,'main');return GROUP_MENU
        return enter(state,actor,group)
    if quoted and value.isdecimal():
        previous=state.data.get('return_to')
        step(state,'radar_preview' if quoted.version is not None else 'radar_story',story_id=quoted.story_id,
             quote_id=quoted.message_id,version=quoted.version,return_to=previous)
        active=True
        if value=='0':return enter(state,actor,group)
    if not active:return None
    if state.step=='radar_search' and text:
        query=' '.join(text.split())
        if not 3<=len(query)<=220:return 'Informe um assunto entre 3 e 220 caracteres.\n\n*0* Menu do Radar'
        step(state,'radar_period',query=query)
        return f'🔎 Buscar reportagens sobre “{query}”\n\nEscolha o período:\n'+PERIOD
    if state.step=='radar_edit_text' and text:
        data=dict(state.data);story=db.session.get(Story,data['story_id'])
        step(state,'radar_preview',**{k:v for k,v in data.items() if k!='radar_ui'})
        return edit_draft(story,data['version'],data['field'],text,actor,group)
    if state.step=='radar_photo' and image:
        card=quoted_card(state.data['quote_id'],actor,group)
        step(state,'radar_preview',**{k:v for k,v in state.data.items() if k!='radar_ui'})
        return save_cover_from_message(card,actor,group,text,image)
    if not value.isdecimal():return None
    data=dict(state.data)
    if state.step=='radar':
        if value in {'1','2'}:return list_pr(state,actor,group,value=='2')
        if value=='3':return search_prompt(state)
        if value=='4':return history(state,actor,group)
    elif state.step=='radar_period' and value in {'1','2','3'}:
        job=request_search(actor,group,data['query'],{'1':7,'2':30,'3':0}[value])
        step(state,'radar_search_wait',search_id=job.id)
        return f'🔎 Consultando reportagens para “{job.term}”. Os resultados chegam aqui.\n\n'+current_options(state)
    elif state.step=='radar_search_wait':
        if value=='2':return search_prompt(state)
        if value=='1':
            job=selected_search(data['search_id'],actor,group)
            return show_results(state,job,actor,group) if job.state in {'ready','error'} else 'A busca ainda está em andamento.\n\n'+current_options(state)
    elif state.step=='radar_history':
        if value=='9':return search_prompt(state)
        if 1<=int(value)<=len(data['ids']):
            job=selected_search(data['ids'][int(value)-1],actor,group)
            if job.state in {'pending','running'}:
                step(state,'radar_search_wait',search_id=job.id);return 'Busca em andamento.\n\n'+current_options(state)
            return show_results(state,job,actor,group)
    elif state.step=='radar_results':
        job=selected_search(data['search_id'],actor,group);offset=data.get('offset',0)
        if value=='9':return search_prompt(state)
        if value=='1' and (not job.results or job.state=='error'):
            step(state,'radar_period',query=job.term);return 'Escolha o período da busca:\n'+PERIOD
        if value=='6' and offset+5<len(job.results):return show_results(state,job,actor,group,offset+5)
        if value=='7' and offset:return show_results(state,job,actor,group,max(0,offset-5))
        if 1<=int(value)<=min(5,len(job.results)-offset):
            index=offset+int(value)-1;row=job.results[index];previous=context(state)
            story=import_result(job,index,actor)
            step(state,'radar_wait',story_id=story.id,return_to=previous)
            result=prepare_complete(story,actor,group)
            return result if isinstance(result,dict) else f"*{row['title']}*\nFonte: {row['source']}\n\n"+result+'\n\n'+current_options(state)
    elif state.step=='radar_result':
        if value=='9':return search_prompt(state)
        if value=='8':return back_to_list(state,actor,group)
        job=selected_search(data['search_id'],actor,group)
        if value=='2':return job.results[data['index']]['url']+'\n\n'+current_options(state)
        if value=='1':
            story=import_result(job,data['index'],actor)
            step(state,'radar_wait',story_id=story.id,return_to=data.get('return_to'))
            result=prepare_complete(story,actor,group)
            return result if isinstance(result,dict) else result+'\n\n'+current_options(state)
    elif state.step=='radar_list':
        if value=='8':return list_pr(state,actor,group,data['urgent'])
        if value=='6' and data['more']:
            cursors=data['cursors'][:data['page']+1]+[data['ids'][-1]]
            return list_pr(state,actor,group,data['urgent'],data['page']+1,cursors)
        if value=='7' and data['page']:return list_pr(state,actor,group,data['urgent'],data['page']-1,data['cursors'])
        if 1<=int(value)<=len(data['ids']):
            story=db.session.get(Story,data['ids'][int(value)-1])
            return detail(state,story,actor,group,context(state))
    elif state.step in {'radar_story','radar_preview'}:
        if value=='8':return back_to_list(state,actor,group)
        if state.step=='radar_story':
            if value=='2':return radar_command('FONTES',actor,group,data['quote_id'])+'\n\n'+current_options(state)
            if value=='1':
                step(state,'radar_wait',**{k:v for k,v in data.items() if k!='radar_ui'})
                result=prepare_complete(db.session.get(Story,data['story_id']),actor,group)
                return result if isinstance(result,dict) else result+'\n\n'+current_options(state)
        else:
            commands={'1':'PUBLICAR','2':'REFAZER','3':'BUSCAR FOTO','4':'FONTES','6':'APURADO'}
            if value in commands:
                result=radar_command(commands[value],actor,group,data['quote_id'])
                if value=='1':return result+'\n\n*8* Voltar à lista\n*0* Menu do Radar'
                return result if isinstance(result,dict) else result+'\n\n'+current_options(state)
            if value=='5':
                step(state,'radar_edit',**{k:v for k,v in data.items() if k!='radar_ui'})
                return '✏️ *Editar prévia*\n\n'+current_options(state)
    elif state.step=='radar_edit':
        if value=='5':
            step(state,'radar_preview',**{k:v for k,v in data.items() if k!='radar_ui'});return PREVIEW
        if value in {'1','2','3'}:
            field={'1':'titulo','2':'resumo','3':'texto'}[value]
            step(state,'radar_edit_text',**{k:v for k,v in data.items() if k not in {'radar_ui','field'}},field=field)
            return 'Envie o novo '+{'titulo':'título','resumo':'resumo','texto':'texto completo'}[field]+'.\n\n*0* Menu do Radar'
        if value=='4':
            step(state,'radar_photo',**{k:v for k,v in data.items() if k!='radar_ui'});return 'Envie a foto nesta conversa. A legenda pode informar o crédito.\n\n*0* Menu do Radar'
    elif state.step=='radar_wait':
        if value=='8':return back_to_list(state,actor,group)
        if value=='2':
            story=db.session.get(Story,data['story_id'])
            return story.articles[0].url+'\n\n'+current_options(state)
        if value=='1':
            story=db.session.get(Story,data['story_id']);draft=latest_draft(story.id) if story else None
            if story and story.state=='review' and draft:
                from .models import DraftMedia,CoverSearch
                photo=db.session.get(DraftMedia,draft.id)
                pending=CoverSearch.query.filter_by(scope=group or '',state='pending').all() if not photo else []
                if any(row.target=={'story':story.id,'version':draft.version} for row in pending):
                    return 'O texto está pronto. Aguardando a foto original para enviar tudo junto.\n\n'+current_options(state)
                from .wa_editorial import draft_cards
                return {'messages':draft_cards(story,draft,actor,group)}
            from .models import Job,Audit
            job=Job.query.filter_by(story_id=data['story_id']).order_by(Job.id.desc()).first()
            if job and job.state=='running':
                seconds=max(0,round((now()-job.started).total_seconds())) if job.started else 0
                read=Audit.query.filter(Audit.story_id==job.story_id,Audit.action=='reportagem_lida',Audit.created>=job.started).first() if job.started else None
                status='Preparando o texto e a foto original' if read else 'Lendo a reportagem e localizando a foto'
                return f'{status} · {seconds}s. A prévia chega automaticamente aqui.\n\n'+current_options(state)
            if job and job.state=='error':
                step(state,'radar_story',**{k:v for k,v in data.items() if k!='radar_ui'})
                if not state.data.get('quote_id'):
                    return detail(state,db.session.get(Story,data['story_id']),actor,group,data.get('return_to'))
                return 'Não consegui concluir a preparação: '+(job.error or 'falha na fonte')+'\n\n'+current_options(state)
            return 'Sua matéria está na fila prioritária. A prévia com texto e foto chega automaticamente aqui.\n\n'+current_options(state)
    return 'Escolha uma das opções exibidas.\n\n'+current_options(state)
