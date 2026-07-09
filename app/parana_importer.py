import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import current_app
from slugify import slugify

BASE_URL = "https://www.paranaatual.com.br"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PortalParanaAtualImporter/1.0; +https://portalparanaatual.com.br)"
}

@dataclass
class ImportCandidate:
    url: str
    title: str = ""


def _get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response


def soup_from_url(url: str) -> BeautifulSoup:
    response = _get(url)
    response.encoding = response.apparent_encoding or response.encoding
    return BeautifulSoup(response.text, "lxml")


def collect_links(start_url: str = BASE_URL, limit: int = 30) -> list[ImportCandidate]:
    soup = soup_from_url(start_url)
    found: dict[str, ImportCandidate] = {}
    for a in soup.select('a[href]'):
        href = urljoin(start_url, a.get('href', ''))
        parsed = urlparse(href)
        if parsed.netloc and 'paranaatual.com.br' not in parsed.netloc:
            continue
        if '/noticia/' not in parsed.path or not parsed.path.endswith('.html'):
            continue
        href = href.split('#', 1)[0]
        title = re.sub(r'\s+', ' ', a.get_text(' ', strip=True) or '').strip()
        if href not in found:
            found[href] = ImportCandidate(url=href, title=title)
        if len(found) >= limit:
            break
    return list(found.values())


def _clean_text(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').strip())


def _find_main_container(soup: BeautifulSoup):
    selectors = [
        'article', '.post-content', '.noticia', '.materia', '.content', 'main', '.container'
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(node.get_text(' ', strip=True)) > 300:
            return node
    return soup.body or soup


def parse_article(url: str) -> dict:
    soup = soup_from_url(url)
    container = _find_main_container(soup)

    title_node = soup.find('h1') or container.find('h1')
    title = _clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    if not title:
        title = _clean_text((soup.title.string if soup.title else '').split('|')[0])

    subtitle = ''
    for selector in ['h2', '.subtitulo', '.subtitle', '.resumo', '.lead']:
        node = container.select_one(selector) or soup.select_one(selector)
        if node:
            subtitle = _clean_text(node.get_text(' ', strip=True))
            if subtitle and subtitle != title:
                break

    page_text = soup.get_text('\n', strip=True)
    published_at = None
    date_match = re.search(r'(\d{2}/\d{2}/\d{4})(?:\s*[àaá]\s*(\d{1,2}:\d{2}))?', page_text)
    if date_match:
        raw = date_match.group(1) + (f" {date_match.group(2)}" if date_match.group(2) else '')
        for fmt in ['%d/%m/%Y %H:%M', '%d/%m/%Y']:
            try:
                published_at = datetime.strptime(raw, fmt)
                break
            except ValueError:
                pass

    author = ''
    author_match = re.search(r'(?:Por|Autor[:\s])\s+([^\n]{2,80})', page_text, re.I)
    if author_match:
        author = _clean_text(author_match.group(1))

    image_url = ''
    og = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
    if og and og.get('content'):
        image_url = urljoin(url, og.get('content'))
    if not image_url:
        for img in container.select('img[src]') + soup.select('img[src]'):
            src = img.get('src') or ''
            if not src or src.startswith('data:'):
                continue
            candidate = urljoin(url, src)
            lowered = candidate.lower()
            if any(x in lowered for x in ['logo', 'sprite', 'whatsapp', 'facebook', 'instagram']):
                continue
            image_url = candidate
            break

    credit = ''
    if image_url:
        img_node = soup.find('img', src=lambda s: s and (s in image_url or urljoin(url, s) == image_url))
        if img_node:
            credit = _clean_text(img_node.get('alt') or img_node.get('title') or '')

    paragraphs = []
    for p in container.find_all(['p']):
        text = _clean_text(p.get_text(' ', strip=True))
        if len(text) < 35:
            continue
        if text.lower().startswith(('compartilhe', 'publicidade', 'leia também')):
            continue
        paragraphs.append(text)
    # remove duplicates preserving order
    seen = set()
    clean_paragraphs = []
    for text in paragraphs:
        key = text[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        clean_paragraphs.append(text)

    content_html = '\n'.join(f'<p>{p}</p>' for p in clean_paragraphs)
    return {
        'source_url': url,
        'title': title or 'Matéria importada',
        'slug': slugify(title or 'materia-importada'),
        'excerpt': subtitle or (clean_paragraphs[0] if clean_paragraphs else ''),
        'content_html': content_html,
        'published_at': published_at,
        'author_name': author or 'Portal Paraná Atual',
        'image_url': image_url,
        'image_credit': credit,
    }


def save_image_locally(image_url: str, slug: str) -> str:
    if not image_url:
        return ''
    response = _get(image_url)
    content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    ext = mimetypes.guess_extension(content_type) or Path(urlparse(image_url).path).suffix or '.jpg'
    if ext == '.jpe':
        ext = '.jpg'
    safe_slug = slugify(slug) or 'imagem'
    media_root = Path(current_app.config['MEDIA_ROOT']).resolve()
    target_dir = media_root / 'imported'
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f'{safe_slug}{ext}'
    target = target_dir / name
    i = 2
    while target.exists():
        target = target_dir / f'{safe_slug}-{i}{ext}'
        i += 1
    target.write_bytes(response.content)
    prefix = current_app.config.get('MEDIA_URL_PREFIX', '/media').rstrip('/')
    return f'{prefix}/imported/{target.name}'
