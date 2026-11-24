import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .fetch import FetchError, canonical_url


SOURCE_KINDS = {"site": "Página pública de notícias", "rss": "Feed RSS ou Atom",
                "sitemap": "Sitemap de notícias"}


@dataclass(frozen=True)
class Item:
    url: str
    title: str
    summary: str
    published: datetime | None


def plain(value, limit):
    if "<" not in (value or ""):
        return " ".join((value or "").split())[:limit]
    return " ".join(BeautifulSoup(value, "html.parser").stripped_strings)[:limit]


def date_from(raw):
    if not raw:
        return None
    for parser in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            value = parser(raw)
            if value and value.tzinfo:
                return value.astimezone(UTC).replace(tzinfo=None)
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def tag(element):
    return element.tag.rsplit("}", 1)[-1]


def child_text(element, *names):
    for name in names:
        for child in element:
            if tag(child) == name:
                return "".join(child.itertext()).strip()
    return ""


def entry_link(element):
    for child in element:
        if tag(child) == "link" and child.get("rel", "alternate") == "alternate":
            return child.get("href") or child.text or ""
    return ""


def read_xml(response):
    try:
        root = ElementTree.fromstring(response.body)
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        raise FetchError("A fonte não retornou XML válido de feed ou sitemap.") from exc

    root_name = tag(root).lower()
    if root_name not in {"rss", "feed", "rdf", "urlset"}:
        raise FetchError("Informe um feed RSS/Atom ou o sitemap específico de notícias.")
    entries = []
    for el in root.iter():
        kind = tag(el)
        if kind in {"item", "entry"}:
            title = child_text(el, "title")
            summary = child_text(el, "description", "summary", "encoded", "content")
            published = child_text(el, "pubDate", "published", "date")
            href = entry_link(el)
        elif kind == "url" and root_name == "urlset":
            news = next((node for node in el if tag(node) == "news"), None)
            if news is None:
                continue
            href = child_text(el, "loc")
            title = child_text(news, "title")
            summary = ""
            published = child_text(news, "publication_date")
        else:
            continue
        if not title or not href:
            continue
        try:
            url = canonical_url(urljoin(response.url, href.strip()))
        except FetchError:
            continue
        # Agregadores que ocultam o endereço original exigem um adaptador próprio.
        if urlsplit(url).hostname in {"news.google.com", "google.com", "feedproxy.google.com"}:
            continue
        entries.append(Item(url, plain(title, 500), plain(summary, 650), date_from(published)))
        if len(entries) >= 100:
            break
    return entries


def get_feed(fetcher, source):
    conditional = (source.cache_url, source.etag, source.modified)
    response = fetcher.get(source.cache_url or source.url, conditional=conditional)
    if response.status == 304:
        return response, []
    if response.status != 200:
        raise FetchError(f"A fonte retornou HTTP {response.status}.")
    if source.kind == "site" and re.search(r"<(?:!doctype html|html)(?:[\s>])", response.body[:1000].decode("ascii", "ignore"), re.I):
        document = BeautifulSoup(response.body, "html.parser")
        feed = document.find("link", rel="alternate", type=["application/rss+xml", "application/atom+xml"], href=True)
        if not feed:
            return response, read_listing(response, document)
        response = fetcher.get(urljoin(response.url, feed["href"]), conditional=conditional)
        if response.status == 304:
            return response, []
        if response.status != 200:
            raise FetchError(f"O feed anunciado pelo site retornou HTTP {response.status}.")
    items = read_xml(response)
    return response, items



def read_listing(response, document):
    entries = {}
    hostname = urlsplit(response.url).hostname
    for link in document.select("article h2 a[href], article h3 a[href], article a[rel~=bookmark][href]")[:100]:
        try:
            address = canonical_url(urljoin(response.url, link["href"]))
        except FetchError:
            continue
        if urlsplit(address).hostname != hostname or address == response.url or address in entries:
            continue
        title = " ".join(link.stripped_strings)[:500]
        if len(title) < 12:
            continue
        article = link.find_parent("article")
        paragraph = article.find("p")
        timestamp = article.find("time", datetime=True)
        entries[address] = Item(address, title, " ".join(paragraph.stripped_strings)[:650] if paragraph else "",
                                date_from(timestamp["datetime"]) if timestamp else None)
    if not entries:
        raise FetchError("O site não apresentou feed nem uma lista reconhecível. Informe o feed ou o sitemap de notícias.")
    return list(entries.values())
