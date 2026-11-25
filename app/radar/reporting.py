import json
import re
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import current_app

from .fetch import FetchError, PublicFetcher, public_url


def clean(value):
    return " ".join(value.split())


def structured_data(page):
    nodes = []
    for tag in page.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        nodes.extend(data if isinstance(data, list) else [data])
    for node in nodes:
        if isinstance(node, dict):
            graph = node.get("@graph")
            if isinstance(graph, list):
                nodes.extend(graph)
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(name in ("NewsArticle", "Article", "ReportageNewsArticle") for name in kinds):
                return node
    return {}


def article_paragraphs(page, schema):
    for selector in (".mc-article-body .content-text__container", ".content-text__container",
                     '[itemprop="articleBody"] p', "article p", "main p"):
        paragraphs = []
        for node in page.select(selector):
            text = clean(node.get_text(" ", strip=True))
            if re.match(r"^(leia também|veja também|vídeos:|mais notícias)", text, re.I):
                break
            if len(text) < 50 or re.search(r"siga (o|a) (canal|página)|inscreva-se", text, re.I):
                continue
            if text not in paragraphs:
                paragraphs.append(text)
        if paragraphs:
            return paragraphs
    body = schema.get("articleBody")
    return [clean(part) for part in body.splitlines() if len(clean(part)) >= 50] if isinstance(body, str) else []


def photo_candidates(page, url, schema):
    missing_credit = "Crédito não informado na reportagem original"
    seen = set()
    def candidate(reference, credit="", description="", license_url=None):
        if not isinstance(reference, str) or not reference or reference.startswith("data:"):
            return None
        absolute = urljoin(url, reference)
        if absolute in seen or re.search(r"(?:^|[/_-])(logo|sprite|favicon|placeholder)(?:[/_.-]|$)", absolute, re.I):
            return None
        seen.add(absolute)
        compatible = isinstance(license_url, str) and re.fullmatch(
            r"https?://creativecommons\.org/(?:licenses/by|publicdomain/(?:zero|mark))/(?:3\.0|4\.0|1\.0)/?", license_url)
        return absolute, clean(credit) or missing_credit, clean(description), license_url if compatible else None
    for figure in page.select("article figure, main figure, .mc-article-body figure, figure.content-media-figure")[:12]:
        if any(parent.name in {"aside", "nav", "footer"} or re.search(
                r"related|recommend|sidebar|advert|publicidade|more-news|leia-tambem",
                " ".join(parent.get("class", [])), re.I) for parent in [figure, *figure.parents] if getattr(parent, "attrs", None) is not None):
            continue
        img, caption = figure.find(["img", "amp-img"]), figure.find("figcaption")
        if not img:
            continue
        caption = re.sub(r"^\d+\s+de\s+\d+\s*", "", clean(caption.get_text(" ", strip=True))) if caption else clean(img.get("alt") or "")
        description, _, credit = re.split(r"\s*[—–-]?\s*(Foto|Imagem|Crédito)\s*:\s*", caption, maxsplit=1,
                                            flags=re.I) if re.search(r"(Foto|Imagem|Crédito)\s*:", caption, re.I) else (caption, "", "")
        reference = img.get("data-src") or img.get("data-original") or img.get("src")
        if value := candidate(reference, credit, description):
            yield value
    image = schema.get("image")
    for entry in image if isinstance(image, list) else [image]:
        if isinstance(entry, str):
            value = candidate(entry)
        elif isinstance(entry, dict):
            creator = entry.get("creator")
            credit = entry.get("creditText") or (creator.get("name") if isinstance(creator, dict) else creator) or ""
            value = candidate(entry.get("url") or entry.get("contentUrl"), credit if isinstance(credit, str) else "",
                              entry.get("caption") if isinstance(entry.get("caption"), str) else "", entry.get("license"))
        else:
            value = None
        if value:
            yield value
    # A share image is considered only on an identified article page; never search results or a site's logo.
    article_type = page.select_one('meta[property="og:type"][content="article"]')
    if schema or (article_type and page.find("h1")):
        for node in page.select('meta[property="og:image"],meta[name="twitter:image"]'):
            if value := candidate(node.get("content")):
                yield value


def image_extension(headers, body):
    content_type = headers.get("content-type", "").partition(";")[0].lower()
    if content_type == "image/jpeg" and body.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content_type == "image/png" and body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content_type == "image/webp" and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "webp"
    return None


def save_photo(fetcher, candidates):
    for url, credit, description, *licenses in candidates:
        if not credit:
            continue
        try:
            response = fetcher.image(public_url(url))
        except FetchError:
            continue
        if response.status != 200:
            continue
        photo = store_photo(response.body, response.headers, credit, description, response.url, licenses[0] if licenses else None)
        if photo:
            return photo
    return None


def store_photo(body, headers, credit, description, url="", license_text=None, *, folder="radar"):
    extension = image_extension(headers, body)
    if not extension:
        return None
    filename = f"{sha256(body).hexdigest()}.{extension}"
    directory = Path(current_app.config["MEDIA_ROOT"]) / folder
    try:
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / filename
        if not dest.exists():
            with NamedTemporaryFile(dir=directory, delete=False) as temp:
                temp.write(body)
                temp_path = Path(temp.name)
            try:
                temp_path.chmod(0o644)
                temp_path.replace(dest)
            finally:
                temp_path.unlink(missing_ok=True)
    except OSError:
        return None
    return {"path": f"{current_app.config['MEDIA_URL_PREFIX'].rstrip('/')}/{folder}/{filename}",
            "credit": credit[:255], "description": description[:500], "url": url[:1000], "license": license_text}

def download_article_photo(app, candidates):
    # Only candidates extracted from this article are passed to the background download.
    with app.app_context():
        return save_photo(PublicFetcher(), candidates[:3])


def report_sources(sources, *, include_photo=True, photo_executor=None):
    fetcher = PublicFetcher()
    photo = None
    for index, source in enumerate(sources):
        try:
            response = fetcher.get(source["url"])
        except FetchError:
            continue
        if response.status != 200 or "html" not in response.headers.get("content-type", ""):
            continue
        page = BeautifulSoup(response.body, "html.parser")
        schema = structured_data(page)
        paragraphs = article_paragraphs(page, schema)
        if paragraphs:
            source["texto"] = "\n".join(paragraphs)[:(3300 if index == 0 else 1500)]
            date = page.find("meta", attrs={"property": "article:published_time"})
            source["publicado"] = schema.get("datePublished") or (date.get("content") if date else None) or source.get("publicado")
        if include_photo and index == 0:
            candidates = list(photo_candidates(page, response.url, schema))
            photo = (photo_executor.submit(download_article_photo, current_app._get_current_object(), candidates)
                     if photo_executor is not None else save_photo(fetcher, candidates))
    return sources, photo
