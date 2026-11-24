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
            if kind in ("NewsArticle", "Article", "ReportageNewsArticle"):
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
    for figure in page.select("article figure, main figure, .mc-article-body figure, figure.content-media-figure")[:12]:
        img, caption = figure.find(["img", "amp-img"]), figure.find("figcaption")
        if not img:
            continue
        caption = re.sub(r"^\d+\s+de\s+\d+\s*", "", clean(caption.get_text(" ", strip=True))) if caption else ""
        description, _, credit = re.split(r"\s*[—–-]?\s*(Foto|Imagem|Crédito)\s*:\s*", caption, maxsplit=1,
                                            flags=re.I) if re.search(r"(Foto|Imagem|Crédito)\s*:", caption, re.I) else (caption, "", "")
        reference = img.get("data-src") or img.get("data-original") or img.get("src")
        if reference and not reference.startswith("data:"):
            yield urljoin(url, reference), clean(credit), clean(description)
    image = schema.get("image")
    for entry in image if isinstance(image, list) else [image]:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and isinstance(entry.get("creditText"), str):
            caption = entry.get("caption")
            yield urljoin(url, entry["url"]), clean(entry["creditText"]), clean(caption) if isinstance(caption, str) else ""


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
    for url, credit, description in candidates:
        if not credit:
            continue
        try:
            response = fetcher.image(public_url(url))
        except FetchError:
            continue
        extension = image_extension(response.headers, response.body)
        if response.status != 200 or not extension:
            continue
        filename = f"{sha256(response.body).hexdigest()}.{extension}"
        directory = Path(current_app.config["MEDIA_ROOT"]) / "radar"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            dest = directory / filename
            if not dest.exists():
                with NamedTemporaryFile(dir=directory, delete=False) as temp:
                    temp.write(response.body)
                    temp_path = Path(temp.name)
                try:
                    temp_path.chmod(0o644)
                    temp_path.replace(dest)
                finally:
                    temp_path.unlink(missing_ok=True)
        except OSError:
            return None
        return {"path": f"{current_app.config['MEDIA_URL_PREFIX'].rstrip('/')}/radar/{filename}",
                "credit": credit[:255], "description": description[:500], "url": response.url[:1000]}
    return None


def report_sources(sources):
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
            source["publicado"] = schema.get("datePublished") or source.get("publicado")
        if photo is None and index == 0:
            photo = save_photo(fetcher, photo_candidates(page, response.url, schema))
    return sources, photo
