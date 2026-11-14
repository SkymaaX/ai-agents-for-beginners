"""Lecture de pages web, en invité poli.

- respecte robots.txt (y compris Crawl-delay) ; robots.txt illisible = on s'abstient ;
- un intervalle minimal entre deux requêtes vers le même domaine ;
- cache disque : une page n'est téléchargée qu'une fois pendant la durée de vie du cache ;
- User-Agent explicite, HTTP(S) uniquement, pages HTML/texte seulement, taille bornée.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urlsplit

import httpx

from ..config import WebCfg
from ..logs import logger
from .cache import DiskCache, RateLimiter

MAX_BYTES = 2_000_000
_SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe", "template"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "pre"}


@dataclass
class Page:
    url: str
    title: str
    text: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(titre, texte lisible) à partir d'une page HTML."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # HTML très abîmé : on garde ce qui a été lu
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return parser.title.strip(), text


class Fetcher:
    def __init__(self, cfg: WebCfg, cache: DiskCache, client: httpx.Client | None = None, limiter: RateLimiter | None = None):
        self.cfg = cfg
        self.cache = cache
        self.limiter = limiter or RateLimiter(cfg.rate_limit_seconds)
        self.client = client or httpx.Client(
            timeout=cfg.timeout, follow_redirects=True, headers={"User-Agent": cfg.user_agent},
        )
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    # ----------------------------------------------------------------------- robots.txt
    def _robots_for(self, origin: str) -> robotparser.RobotFileParser | None:
        """Règles robots.txt du site. None = site à éviter (robots.txt inaccessible)."""
        if origin in self._robots:
            return self._robots[origin]
        parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
        cached = self.cache.get("robots", origin)
        if cached is None:
            try:
                self.limiter.wait(urlsplit(origin).netloc)
                response = self.client.get(origin + "/robots.txt")
                if response.status_code >= 500:
                    cached = {"status": "error", "body": ""}
                elif response.status_code >= 400:
                    cached = {"status": "none", "body": ""}  # pas de robots.txt : tout est permis
                else:
                    cached = {"status": "ok", "body": response.text[:500_000]}
            except httpx.HTTPError as exc:
                logger.info("robots.txt illisible pour %s (%s) : site ignoré", origin, exc)
                cached = {"status": "error", "body": ""}
            self.cache.set("robots", origin, cached)
        if cached["status"] == "error":
            parser = None
        else:
            parser.parse(cached["body"].splitlines())
        self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        if not self.cfg.respect_robots:
            return True
        parser = self._robots_for(f"{parts.scheme}://{parts.netloc}")
        return parser is not None and parser.can_fetch(self.cfg.user_agent, url)

    def _crawl_delay(self, url: str) -> float:
        parts = urlsplit(url)
        parser = self._robots.get(f"{parts.scheme}://{parts.netloc}")
        if parser is None:
            return 0.0
        delay = parser.crawl_delay(self.cfg.user_agent)
        return float(delay) if delay else 0.0

    # ----------------------------------------------------------------------------- page
    def fetch(self, url: str) -> Page | None:
        """Texte de la page, ou None (interdite par robots.txt, pas du HTML, erreur)."""
        cached = self.cache.get("pages", url)
        if cached is not None:
            return Page(**cached) if cached.get("text") else None
        if not self.allowed(url):
            logger.info("page non lue (robots.txt ou schéma) : %s", url)
            return None
        self.limiter.wait(urlsplit(url).netloc, self._crawl_delay(url))
        page: Page | None = None
        try:
            with self.client.stream("GET", url) as response:
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code == 200 and ("html" in content_type or "text/plain" in content_type):
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_BYTES:
                            break
                    body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    title, text = html_to_text(body) if "html" in content_type else ("", body)
                    if text:
                        page = Page(url=url, title=title or url, text=text[: self.cfg.max_page_chars])
        except httpx.HTTPError as exc:
            logger.info("page illisible %s (%s)", url, exc)
        self.cache.set("pages", url, asdict(page) if page else {"url": url, "title": "", "text": ""})
        return page
