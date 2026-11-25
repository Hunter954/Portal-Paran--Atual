"""Menu e tarefas do portal dentro da conversa da redação."""

import re
from datetime import timedelta

from sqlalchemy import select

from ..models import db
from .editorial import EditorialError
from .geography import normalize
from .models import SiteChat, audit, now
from .wa_cms import (NEW_POST, POST_MENU, by_card, cms_command, find_card, new_post, post_list,
                     require_editor)
from .wa_cover import media_from_editor, queue_search, use_photo
from .wa_editorial import open_radar, quoted_card, radar_command
from .wa_site import (ADS_MENU, CATEGORY_MENU, MEDIA_MENU, SETTINGS_MENU, USERS_MENU, ads_action, category_action,
                      insights, library, overview, settings_overview, site_value, user_action, users_list)


GROUP_MENU = (
    "🟢 *PARANÁ ATUAL | REDAÇÃO*\n━━━━━━━━━━━━━━\n\n"
    "*1*  📊 Visão geral\n*2*  📰 Matérias\n*3*  👥 Usuários e acessos\n"
    "*4*  📈 Insights\n*5*  ⚙️ Configurações e SEO\n*6*  📣 Publicidade\n"
    "*7*  🖼️ Mídia e capas\n*8*  🏷️ Categorias e home\n*9*  📡 Radar de notícias\n\n"
    "Digite o número. *MENU*, *VOLTAR* e *SAIR* funcionam a qualquer momento."
)
MENUS = {
    "main": GROUP_MENU, "posts": POST_MENU, "users": USERS_MENU, "settings": SETTINGS_MENU,
    "ads": ADS_MENU, "media": MEDIA_MENU, "categories": CATEGORY_MENU,
}
MAIN_CHOICES = {"2": "posts", "3": "users", "5": "settings", "6": "ads", "7": "media", "8": "categories", "9": "radar"}
INPUTS = {
    "posts": {
        "1": ("new_title", NEW_POST),
        "2": ("find_post", "Digite parte do título. Abra a matéria respondendo *VER* e escolha *RETIRAR DO AR*."),
        "3": ("find_post", "Digite parte do título ou o número da matéria que quer editar."),
        "5": ("find_post", "🔎 Digite parte do título ou o número da matéria."),
    },
    "settings": {
        "2": ("titulo", "Envie o novo título do site."),
        "3": ("descricao", "Envie a nova descrição SEO."),
        "4": ("palavras", "Envie as palavras-chave separadas por vírgulas."),
        "6": ("social", "Envie *INSTAGRAM*, *FACEBOOK*, *YOUTUBE*, *X*, *CONTATO* ou *TELEFONE* seguido do valor."),
        "7": ("identity", "Envie *LOGO*, *CAPA* ou *FAVICON* seguido da URL pública."),
    },
    "categories": {
        "2": ("new", "Envie o nome da nova categoria."),
        "3": ("rename", "Envie *número | novo nome*."),
        "4": ("home", "Envie os números das categorias na ordem em que devem aparecer na home, ou *0* para limpar."),
        "5": ("delete", "Envie *número | EXCLUIR* para remover uma categoria vazia."),
    },
    "users": {
        "2": ("new", "Envie *e-mail | WhatsApp* com DDD. A criação da senha é feita pela recuperação no número cadastrado."),
        "3": ("active", "Envie *ID | SIM* para ativar ou *ID | NÃO* para pausar."),
        "4": ("phone", "Envie *ID | WhatsApp* com DDD."),
        "5": ("admin", "Envie *ID | SIM* para conceder acesso de administrador ou *ID | NÃO* para remover."),
    },
    "ads": {
        "2": ("add", "Envie *espaço | nome | URL da imagem | URL de destino*. Use *VOLTAR* e opção 1 para ver os espaços."),
        "3": ("toggle", "Envie *espaço | ATIVAR* ou *espaço | PAUSAR*."),
        "5": ("order", "Envie os três códigos da lateral na ordem desejada. Exemplo *sidebar_1 header_top sidebar_2*."),
        "6": ("remove", "Envie *espaço | número do banner* conforme a lista."),
    },
    "media": {
        "2": ("upload", "Envie uma foto JPG, PNG ou WebP de até 2 MB. O texto da legenda, se quiser, é o crédito."),
        "3": ("identity", "Envie *LOGO*, *CAPA* ou *FAVICON* seguido da URL pública."),
        "4": ("search_photo", "Digite o assunto ou lugar da foto."),
    },
}


def state_for(actor, group):
    scope = group or ""
    entry = db.session.execute(select(SiteChat).where(SiteChat.actor_id == actor, SiteChat.scope == scope)
                               .with_for_update()).scalar_one_or_none()
    if not entry:
        entry = SiteChat(actor_id=actor, scope=scope, step="idle", data={})
        db.session.add(entry)
    elif entry.updated < now() - timedelta(minutes=35):
        set_step(entry, "idle")
    return entry


def set_step(state, step, **data):
    state.step, state.data, state.updated = step, data, now()


def accepts_image(actor, group, quote_id):
    from .editorial import can_publish
    if not can_publish(actor):
        return False
    post = find_card(quote_id, actor, group)
    draft = quoted_card(quote_id, actor, group)
    if (post and post.kind == "post") or (draft and draft.version is not None):
        return True
    state = db.session.get(SiteChat, (actor, group or ""))
    if state and state.step == 'radar_photo' and state.updated >= now() - timedelta(minutes=35):
        card = quoted_card(state.data.get('quote_id'), actor, group)
        if card and card.version is not None:
            from .editorial import latest_draft
            draft = latest_draft(card.story_id)
            return bool(draft and draft.version == card.version)
    return bool(state and state.updated >= now() - timedelta(minutes=35) and state.step == "input"
                and state.data.get("action") == "upload")

def group_menu(text, actor, group, quote_id=None, image=None):
    text, state = text.strip(), state_for(actor, group)
    plain = normalize(text)
    if plain in {"menu", "/menu"}:
        set_step(state, "main")
        return GROUP_MENU
    if plain == "sair":
        set_step(state, "idle")
        return "Menu encerrado. Digite *MENU* para abrir a redação."
    from .wa_radar_menu import handle
    result = handle(state, text, actor, group, quote_id, image)
    if result is not None:
        return result
    if plain == "voltar":
        area = state.data.get("area") if state.step == "input" else "main"
        set_step(state, area or "main")
        return MENUS.get(state.step, GROUP_MENU)
    card = find_card(quote_id, actor, group)
    if card:
        return by_card(card, text, actor, group, image) if card.kind == "post" else use_photo(card, text, actor, group)
    radar_card = quoted_card(quote_id, actor, group)
    if radar_card and image:
        from .wa_editorial import save_cover_from_message
        return save_cover_from_message(radar_card, actor, group, text, image)
    if radar_card or plain == "mais" or plain == "radar" or plain.startswith("radar "):
        if state.step != "radar" and plain != "mais":
            set_step(state, "radar")
        return radar_command(text, actor, group, quote_id)
    if plain in {"nova materia", "nova noticia", "/postar", "cms nova"}:
        require_editor(actor)
        set_step(state, "input", area="posts", action="new_title")
        return NEW_POST
    if plain.startswith("cms "):
        return cms_command(text, actor, group)
    if plain == "cms" or plain == "materias":
        set_step(state, "posts")
        return POST_MENU
    if plain in {"publicidade", "seo", "usuarios", "categorias", "midia"}:
        area = {"publicidade": "ads", "seo": "settings", "usuarios": "users", "categorias": "categories", "midia": "media"}[plain]
        set_step(state, area)
        return MENUS[area]
    if plain == "insights":
        return insights()
    if plain.startswith("seo "):
        parts = text.split(maxsplit=2)
        return settings_overview() if normalize(parts[1]) == "ver" else site_value(
            normalize(parts[1]), parts[2] if len(parts) == 3 else "", actor)
    if plain.startswith("publicidade "):
        parts = text.split(maxsplit=2)
        action = normalize(parts[1])
        data = parts[2] if len(parts) == 3 else ""
        if action in {"ativar", "pausar"}:
            return ads_action("toggle", f"{data} | {action}", actor)
        return ads_action("list", "", actor) if action == "listar" else ADS_MENU
    if plain == "0" and state.step not in {"idle", "input", "radar"}:
        set_step(state, "main")
        return GROUP_MENU
    if state.step == "input":
        result = capture(state, text, image, actor, group)
        if result:
            return result
    if re.fullmatch(r"\d{1,4}", plain):
        if state.step == "radar":
            return radar_command(text, actor, group)
        return menu_number(state, plain, actor, group)
    if plain.split(maxsplit=1)[:1] and plain.split(maxsplit=1)[0] in {
        "postar", "ver", "fontes", "gerar", "confirmar", "apurar", "descartar", "apurado"
    }:
        return radar_command(text, actor, group)
    if state.data.get('radar_ui'):
        from .wa_radar_menu import current_options
        return 'Escolha uma das opções exibidas.\n\n' + current_options(state)
    return None


def menu_number(state, value, actor, group):
    step = state.step
    if step == "main":
        if value == "1":
            return overview()
        if value == "4":
            return insights()
        area = MAIN_CHOICES.get(value)
        if area:
            set_step(state, area)
            return open_radar(actor, group) if area == "radar" else MENUS[area]
        return GROUP_MENU
    entry = INPUTS.get(step, {}).get(value)
    if entry:
        action, instruction = entry
        if not (step == "posts" and action == "find_post"):
            require_editor(actor)
        set_step(state, "input", area=step, action=action)
        return instruction
    if step == "posts" and value in {"4", "6"}:
        return post_list(actor, group, drafts=value == "6")
    if step == "settings" and value == "5":
        set_step(state, "categories")
        return CATEGORY_MENU
    if step == "settings" and value == "1":
        return settings_overview()
    if step == "users" and value == "1":
        return users_list()
    if step == "categories" and value == "1":
        return category_action("list", "", actor)
    if step == "ads" and value in {"1", "4"}:
        return ads_action("list" if value == "1" else "report", "", actor)
    if step == "media" and value == "1":
        return library()
    return MENUS.get(step)


def capture(state, text, image, actor, group):
    area, action = state.data["area"], state.data["action"]
    if action == "upload":
        if not image:
            return "Envie uma foto do WhatsApp ou digite *VOLTAR*."
        result = media_from_editor(f"|{text}" if text else "", image, library=True)
        audit("whatsapp_imagem_enviada", actor=actor, details=result["path"])
        response = "✅ Imagem salva na biblioteca."
    elif not text:
        return None
    elif action == "new_title":
        separated = re.split(r"\n\s*\n", text, maxsplit=1)
        first, body = separated if len(separated) == 2 else (separated[0], "")
        if body.strip():
            response = new_post(first, body, actor, group)
        else:
            if len(first) > 500:
                raise EditorialError("O título pode ter até 500 caracteres.")
            set_step(state, "input", area="posts", action="new_body", title=first)
            return "Agora envie o texto completo. Eu devolvo a prévia para você publicar."
    elif action == "new_body":
        response = new_post(state.data["title"], text, actor, group)
    elif action == "find_post":
        response = cms_command("CMS VER " + text, actor, group) if re.fullmatch(r"\d{1,9}", text) else post_list(
            actor, group, query=text[:180])
    elif action == "search_photo":
        response = queue_search(actor, group, {}, text)
    elif area == "categories":
        if action == "delete":
            identifier, _, confirmation = text.partition("|")
            if normalize(confirmation) != "excluir":
                raise EditorialError("Envie *número | EXCLUIR* para confirmar.")
            text = identifier.strip()
        response = category_action(action, text, actor)
    elif area == "users":
        response = user_action(action, text, actor)
    elif area == "ads":
        response = ads_action(action, text, actor)
    else:
        field, value = action, text
        if action in {"identity", "social"}:
            field, _, value = text.partition(" ")
            field = normalize(field)
        response = site_value(field, value.strip(), actor)
    set_step(state, area)
    return response
