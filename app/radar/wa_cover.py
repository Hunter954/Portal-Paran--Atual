import base64
import binascii
import os
from datetime import timedelta
from secrets import token_hex

import requests
from urllib.parse import urlsplit

from ..models import User, db
from .editorial import EditorialError, latest_draft, lock_story, save_draft
from .fetch import FetchError, PublicFetcher
from .group_delivery import PUBLIC_URL, group_settings
from .models import CoverSearch, audit, now
from .reporting import save_photo, store_photo
from .wa_cms import card_message, get_post, plain, preview, preview_version, require_editor
from .whatsapp import enabled, gateway, phone_number


def media_from_editor(value, image, *, library=False):
    credit = "Redação Paraná Atual"
    url, _, entered_credit = value.partition("|")
    if entered_credit.strip():
        credit = entered_credit.strip()
    folder = "posts" if library else "radar"
    if image:
        try:
            data = base64.b64decode(image["data"], validate=True)
        except (KeyError, ValueError, binascii.Error) as exc:
            raise EditorialError("Não consegui ler a foto enviada.") from exc
        if len(data) > 2_000_000:
            raise EditorialError("Envie uma foto JPG, PNG ou WebP de até 2 MB.")
        result = store_photo(data, {"content-type": image["mime"]}, credit, "Imagem da matéria",
                             license_text="Fornecida pela redação", folder=folder)
    else:
        if not url.strip() or not entered_credit.strip():
            raise EditorialError("Envie a foto ou responda *FOTO URL | crédito* com uma imagem autorizada.")
        result = save_photo(PublicFetcher(), [(url.strip(), credit, "Imagem da matéria", "Fornecida pela redação")])
    if not result:
        raise EditorialError("A imagem não abriu. Use JPG, PNG ou WebP com até 2 MB.")
    return result


def queue_search(actor, group, target, title, location="", query=""):
    require_editor(actor)
    term = (query or title).strip()[:220]
    if len(term) < 3:
        raise EditorialError("Informe o assunto ou o lugar da foto.")
    existing = CoverSearch.query.filter_by(actor_id=actor, scope=group or "", state="pending").count()
    if existing >= 2:
        return "Você já tem duas buscas na fila. Os resultados chegam nesta conversa."
    db.session.add(CoverSearch(actor_id=actor, scope=group or "", term=term, location=location[:100], target=target))
    return f"🔎 Buscando fotos de *{term}*. As opções chegam aqui."


def google_images(session, query):
    key = os.getenv("RADAR_GOOGLE_IMAGES_KEY")
    if not key:
        return []
    data = session.get("https://serpapi.com/search.json", params={"engine": "google_images", "q": query,
                       "gl": "br", "hl": "pt", "api_key": key, "safe": "active"},
                       timeout=(3, 9), allow_redirects=False)
    if not data.ok:
        return []
    result = data.json()
    rows = result.get("images_results") or result.get("image_results") or []
    return [(row["original"], (row.get("source") or "Resultado do Google Imagens"), plain(row.get("title"))[:180],
             None, row.get("link", "")) for row in rows[:8]
            if isinstance(row, dict) and isinstance(row.get("original"), str)]


def commons_images(session, query):
    response = session.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "formatversion": 2, "generator": "search", "gsrnamespace": 6,
        "gsrsearch": query + " filetype:bitmap", "gsrlimit": 8, "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": 1100,
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|ObjectName",
    }, timeout=(3, 8), allow_redirects=False)
    if not response.ok:
        return []
    rows = response.json().get("query", {}).get("pages", [])
    result = []
    for row in rows:
        image = next(iter(row.get("imageinfo") or []), {})
        values = image.get("extmetadata", {})
        name = values.get("LicenseShortName", {}).get("value", "")
        license_url = values.get("LicenseUrl", {}).get("value", "")
        credit = plain(values.get("Artist", {}).get("value", ""))
        if not credit or not name.startswith(("CC BY", "CC0", "Public domain")) or "-NC" in name or "-ND" in name:
            continue
        if name.startswith("CC BY") and urlsplit(license_url).hostname != "creativecommons.org":
            continue
        url = image.get("thumburl") or image.get("url")
        if not url or urlsplit(url).hostname != "upload.wikimedia.org":
            continue
        description = plain(values.get("ObjectName", {}).get("value", "") or row["title"].removeprefix("File:"))
        result.append((url, f"{credit} · {name}", f"Imagem ilustrativa · {description[:170]}",
                       license_url or name, image.get("descriptionurl", "")))
    return result


def search_images(query, location=""):
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.headers["User-Agent"] = "ParanaAtualRadar/1.0 (https://paranaatual.com.br/)"
            rows = google_images(session, query) if os.getenv("RADAR_GOOGLE_IMAGES_KEY") else []
            if rows:
                return "Google Imagens", rows
            rows = commons_images(session, query)
            if not rows and location:
                rows = commons_images(session, location + " Paraná")
            return "Wikimedia Commons", rows
    except (requests.RequestException, ValueError, KeyError):
        return "Busca de imagens", []


def run_cover_search():
    if not enabled():
        return
    job = CoverSearch.query.filter_by(state="pending").order_by(CoverSearch.id).first()
    if not job:
        return
    if now() - job.created > timedelta(hours=2):
        job.state = "cancelled"
        db.session.commit()
        return
    user = db.session.get(User, job.actor_id)
    phone = phone_number(user.whatsapp) if user and user.is_admin and user.is_active else None
    if not phone:
        job.state = "cancelled"
        db.session.commit()
        return
    config = None
    if job.scope:
        try:
            config = group_settings()
        except EditorialError:
            return
        if not config["enabled"] or config["group_id"] != job.scope:
            job.state = "cancelled"
            db.session.commit()
            return
        if not config["connected"]:
            return
    actor, scope, target = job.actor_id, job.scope, job.target
    source, rows = search_images(job.term, job.location)
    fetcher, messages = PublicFetcher(), []
    for url, credit, description, license_text, origin in rows[:5]:
        try:
            photo = save_photo(fetcher, [(url, credit, description, license_text)])
        except (FetchError, ValueError):
            continue
        if not photo:
            continue
        link = origin if isinstance(origin, str) and origin.startswith("https://") else ""
        text = f"🖼️ *{source}*\n{photo['description']}\n{photo['credit']}"
        if link:
            text += f"\n{link[:300]}"
        if target:
            text += "\n\nResponda *USAR FOTO*." if photo["license"] else "\n\nCom permissão, responda *USAR FOTO* seguida de quem autorizou."
        messages.append(card_message("photo", {"photo": photo, "target": target}, text, actor, scope, PUBLIC_URL + photo["path"]))
        if len(messages) == 3:
            break
    if not messages:
        messages = [{"id": "3EB0" + token_hex(9).upper(),
                     "text": "Não encontrei uma foto para esse assunto. Tente *BUSCAR FOTO* com o nome do local ou envie sua foto na prévia."}]
    job.state = "sending"
    db.session.commit()
    try:
        if config:
            result = gateway("POST", "/radar/group/reply", timeout=(3, 35),
                             json={"group_id": scope, "revision": config["revision"], "messages": messages})
        else:
            result = gateway("POST", "/radar/reply", timeout=(3, 35), json={"phone": phone, "messages": messages})
        job.state = "sent" if result.get("accepted") else "failed"
    except EditorialError:
        job.state = "failed"
    db.session.commit()


def use_photo(card, text, actor, group):
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2 or plain(text).lower().split()[:2] != ["usar", "foto"]:
        return None
    require_editor(actor)
    photo, target = dict(card.data["photo"]), card.data["target"]
    if not target:
        return "Abra a matéria e busque a foto a partir da prévia para vincular à publicação."
    if not photo["license"]:
        if len(parts) != 3 or not 3 <= len(parts[2]) <= 250:
            raise EditorialError("Responda *USAR FOTO* seguida de quem autorizou o uso público.")
        photo["license"] = parts[2].strip()
    if "post" in target:
        post = get_post(target["post"])
        if not post or preview_version(post) != target["version"]:
            raise EditorialError("A matéria mudou. Busque a foto a partir da prévia atual.")
        post.featured_image, post.image_description, post.image_credit = photo["path"], photo["description"], photo["credit"]
        post.updated_at = now()
        audit("whatsapp_cms_foto_escolhida", actor=actor, details=f"Matéria #{post.id} · {photo['license']}")
        return preview(post, actor, group)
    story = lock_story(target["story"])
    draft = latest_draft(story.id) if story else None
    if not draft or draft.version != target["version"] or story.state != "review":
        raise EditorialError("A pauta mudou. Busque a foto a partir da prévia atual.")
    latest = save_draft(story, {"titulo": draft.title, "resumo": draft.excerpt, "corpo": draft.body}, actor, photo=photo)
    audit("whatsapp_radar_foto_escolhida", actor=actor, story=story.id, details=photo["license"])
    from .wa_editorial import draft_cards
    return {"messages": draft_cards(story, latest, actor, group)}
