from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from lxml import etree, html
import requests
from loguru import logger

from .protocol import Paper
from .url_utils import safe_http_url


REQUEST_TIMEOUT = (3.05, 8)
MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 3
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>?#]+", re.IGNORECASE)
PII_PATTERN = re.compile(r"[A-Za-z0-9.-]{6,64}")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SESSION = requests.Session()
_SESSION.trust_env = False


def _doi(value: str | None) -> str | None:
    if not value:
        return None
    match = DOI_PATTERN.search(value)
    return match.group(0).rstrip(".,;:").casefold() if match else None


def _paper_doi(paper: Paper) -> str | None:
    return _doi(paper.doi) or _doi(paper.url) or _doi(paper.pdf_url)


def _is_public_http_url(value: str) -> bool:
    url = safe_http_url(value)
    if url is None:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").casefold()
        if not host or host == "localhost" or "." not in host or host.endswith((".localhost", ".local", ".internal", ".test")):
            return False
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(result[4][0].split("%", 1)[0])
                for result in socket.getaddrinfo(
                    host, parsed.port or (443 if parsed.scheme.casefold() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            ]
        return bool(addresses) and all(address.is_global for address in addresses)
    except (OSError, ValueError):
        return False


def _get_public(url: str, accept: str) -> tuple[bytes, str] | None:
    url = safe_http_url(url) or ""
    for redirect_count in range(MAX_REDIRECTS + 1):
        if not _is_public_http_url(url):
            raise ValueError("refusing a non-public or invalid HTTP(S) destination")
        with _SESSION.get(
            url,
            headers={"Accept": accept, "User-Agent": "zotero-arxiv-daily (https://github.com/TideDra/zotero-arxiv-daily)"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location or redirect_count == MAX_REDIRECTS:
                    raise ValueError("refusing an incomplete or excessive redirect chain")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_RESPONSE_BYTES:
                raise ValueError("abstract lookup response exceeds the size limit")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=16_384):
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("abstract lookup response exceeds the size limit")
            return bytes(content), response.headers.get("Content-Type", "").casefold()
    return None


def _clean_text(value: str) -> str:
    value = " ".join(value.split())
    value = re.sub(r"(?i)^abstract\s*[:\-–—]\s*", "", value).strip()
    return "" if value.casefold() == "abstract" else value[:30_000]


def _visible_text(node, clean: bool = True) -> str:
    text_nodes = node.xpath(
        ".//text()[not(ancestor::script|ancestor::style|ancestor::noscript|ancestor::svg) "
        "and not(ancestor::*[contains(translate(concat(@id,' ',@class), $upper, $lower), 'graphical') "
        "or contains(translate(concat(@id,' ',@class), $upper, $lower), 'highlight') "
        "or contains(translate(concat(@id,' ',@class), $upper, $lower), 'visual')])]",
        upper=UPPER,
        lower=LOWER,
    )
    text = " ".join(" ".join(str(item) for item in text_nodes).split())
    return _clean_text(text) if clean else text


def _page_data(content: bytes) -> tuple[str, str | None]:
    root = html.fromstring(content)
    metas = root.xpath("//meta[@name or @property]")
    abstract = ""
    doi = None
    for meta in metas:
        name = (meta.get("name") or meta.get("property") or "").casefold()
        content_value = meta.get("content") or ""
        if name in {"citation_doi", "prism.doi", "dc.identifier", "dc.identifier.doi"}:
            doi = doi or _doi(content_value)
        if name == "citation_abstract":
            abstract = abstract or _clean_text(content_value)
    if abstract:
        return abstract, doi

    regions = root.xpath(
        "//*[translate(@id, $upper, $lower)='abstract' or translate(@id, $upper, $lower)='abstracts' "
        "or contains(concat(' ', normalize-space(translate(@class, $upper, $lower)), ' '), ' abstract ') "
        "or contains(concat(' ', normalize-space(translate(@class, $upper, $lower)), ' '), ' abstracts ')]",
        upper=UPPER,
        lower=LOWER,
    )
    candidates = []
    for region in regions:
        labels = f"{region.get('id', '')} {region.get('class', '')}".casefold()
        if any(term in labels for term in ("graphical", "highlight", "visual")):
            continue
        classes = set((region.get("class") or "").casefold().split())
        score = 3 if {"abstract", "author"}.issubset(classes) else 2 if "abstract" in classes else 1
        text = _visible_text(region)
        if text:
            candidates.append((score, text))

    headings = root.xpath(
        "//h1|//h2|//h3|//h4|//h5|//h6|//*[contains(concat(' ', normalize-space(translate(@class, $upper, $lower)), ' '), ' section-title ')]",
        upper=UPPER,
        lower=LOWER,
    )
    for heading in headings:
        if _visible_text(heading, clean=False).strip(" :").casefold() != "abstract":
            continue
        sibling = heading.getnext()
        parts = []
        while sibling is not None and sibling not in headings:
            parts.append(_visible_text(sibling))
            sibling = sibling.getnext()
        text = _clean_text(" ".join(parts))
        if text:
            candidates.append((2, text))
    if candidates:
        abstract = max(candidates, key=lambda item: (item[0], len(item[1])))[1]
    return abstract, doi


def _page_abstract(paper: Paper) -> tuple[str, str | None]:
    if not paper.url or urlsplit(paper.url).path.casefold().endswith(".pdf"):
        return "", None
    result = _get_public(paper.url, "text/html,application/xhtml+xml")
    if not result:
        return "", None
    content, content_type = result
    if "html" not in content_type and "xhtml" not in content_type:
        return "", None
    return _page_data(content)


def _paper_pii(paper: Paper) -> str | None:
    for value in (paper.url, paper.pdf_url):
        if not value:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        match = re.search(r"(?:^|/)pii/([^/?#]+)", parsed.path, flags=re.IGNORECASE)
        candidate = match.group(1) if match else parse_qs(parsed.query).get("pii", [""])[0]
        if PII_PATTERN.fullmatch(candidate):
            return candidate
    return None


def _elsevier_data(pii: str) -> tuple[str, str | None]:
    url = f"https://api.elsevier.com/content/article/pii/{quote(pii, safe='')}?httpAccept=text/xml"
    headers = "application/xml,text/xml"
    result = _get_public(url, headers)
    if not result:
        return "", None
    body, _ = result
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(body, parser=parser)
    doi = _doi(root.xpath("string((//*[local-name()='coredata']/*[local-name()='doi'])[1])"))
    abstracts = root.xpath("//*[local-name()='abstract']")
    values = []
    for node in abstracts:
        labels = " ".join(
            [node.get("class", ""), node.get("id", "")]
            + node.xpath(".//*[local-name()='section-title']/text()")
        ).casefold()
        if any(term in labels for term in ("graphical", "highlight", "visual")):
            continue
        values.append(_clean_text(" ".join(node.itertext())))
    if not any(values):
        descriptions = root.xpath("//*[local-name()='coredata']/*[local-name()='description']/text()")
        values.extend(
            _clean_text(value) for value in descriptions
            if not re.search(r"(?i)\b(graphical abstract|highlights)\b", value)
        )
    return max((value for value in values if value), key=len, default=""), doi


def _crossref_abstract(doi: str) -> str:
    url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
    result = _get_public(url, "application/json")
    if not result:
        return ""
    content, _ = result
    abstract = json.loads(content).get("message", {}).get("abstract") or ""
    fragments = html.fragments_fromstring(str(abstract))
    return _clean_text(" ".join(item if isinstance(item, str) else item.text_content() for item in fragments))


def _attempt(paper: Paper, source: str, lookup) -> str:
    try:
        return lookup() or ""
    except Exception as exc:
        logger.warning(f"Failed to retrieve {source} abstract for {paper.title}: {exc}")
        return ""


def _retrieve_abstract(paper: Paper) -> str:
    page_doi = None
    try:
        abstract, page_doi = _page_abstract(paper)
    except Exception as exc:
        logger.warning(f"Failed to retrieve article-page abstract for {paper.title}: {exc}")
        abstract = ""
    if page_doi and not paper.doi:
        paper.doi = page_doi
    if abstract:
        return abstract

    host_names = []
    for value in (paper.url, paper.pdf_url):
        if value:
            try:
                host_names.append((urlsplit(value).hostname or "").casefold())
            except ValueError:
                pass
    is_elsevier = any(
        host in {"sciencedirect.com", "elsevier.com"}
        or host.endswith((".sciencedirect.com", ".elsevier.com"))
        for host in host_names
    )
    if is_elsevier and (pii := _paper_pii(paper)):
        try:
            abstract, elsevier_doi = _elsevier_data(pii)
        except Exception as exc:
            logger.warning(f"Failed to retrieve Elsevier Article API abstract for {paper.title}: {exc}")
            abstract, elsevier_doi = "", None
        if elsevier_doi and not paper.doi:
            paper.doi = elsevier_doi
        if abstract:
            return abstract

    doi = _paper_doi(paper)
    return _attempt(paper, "Crossref", lambda: _crossref_abstract(doi)) if doi else ""


def enrich_missing_abstracts(papers: list[Paper]) -> None:
    """Fill missing non-arXiv candidate abstracts before relevance ranking."""
    for paper in papers:
        if (paper.source or "").casefold() == "arxiv" or (paper.abstract or "").strip():
            continue
        try:
            abstract = _retrieve_abstract(paper)
        except Exception as exc:
            logger.warning(f"Failed to enrich abstract for {paper.title}: {exc}")
            continue
        if abstract.strip():
            paper.abstract = abstract.strip()
