import io
import json
import os
import re
import subprocess
import sys
from bisect import bisect_right
from functools import lru_cache

import tiktoken
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from pypdf import PdfReader

from hub.fetch import InvalidSource, SourceUnavailable, fetch_html

MAX_TEXT = 2_000_000
MAX_CHUNKS = 2000
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
HTML_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def pdf_sections(content: bytes):
    if not content.startswith(b"%PDF-"):
        raise InvalidSource("invalid_pdf")
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise InvalidSource("encrypted_pdf")
        if len(reader.pages) > 500:
            raise InvalidSource("too_many_pages")
        for number, page in enumerate(reader.pages, 1):
            yield f"Page {number}", page.extract_text() or ""
    except InvalidSource:
        raise
    except Exception:
        raise InvalidSource("invalid_pdf") from None


def markdown_sections(text: str):
    headings = []
    section = []
    fence = None
    for line in text.splitlines():
        marker = FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif (
                marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end() :].strip()
            ):
                fence = None
            section.append(line)
            continue
        match = HEADING.match(line) if fence is None else None
        setext = SETEXT.match(line) if fence is None and section and section[-1].strip() else None
        if match or setext:
            title = match[2] if match else section.pop().strip()
            level = len(match[1]) if match else (1 if setext[1][0] == "=" else 2)
            yield " / ".join(title for _, title in headings) or "Introduction", "\n".join(section)
            headings = [(rank, title) for rank, title in headings if rank < level]
            headings.append((level, title))
            section = [line] if match else [title]
        else:
            section.append(line)
    yield " / ".join(title for _, title in headings) or "Introduction", "\n".join(section)


def html_sections(content: bytes):
    soup = BeautifulSoup(content, "html.parser")
    root = soup.find("main") or soup.find("article") or soup.body
    if root is None:
        if soup.head:
            soup.head.decompose()
        root = soup
    for node in root.find_all(
        ["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "template"]
    ):
        node.decompose()
    headings = []
    section = []
    for item in root.descendants:
        if isinstance(item, Tag) and item.name in HTML_HEADINGS:
            yield " / ".join(title for _, title in headings) or "Page content", " ".join(section)
            level = int(item.name[1])
            headings = [(rank, title) for rank, title in headings if rank < level]
            title = item.get_text(" ", strip=True)
            if title:
                headings.append((level, title))
            section = []
        elif type(item) is NavigableString and item.strip() and item.find_parent(HTML_HEADINGS) is None:
            section.append(item.strip())
    yield " / ".join(title for _, title in headings) or "Page content", " ".join(section)


@lru_cache
def encoding():
    return tiktoken.get_encoding("cl100k_base")


def chunk_sections(sections, *, lines=False):
    total = 0
    result = []
    codec = encoding()
    for location, text in sections:
        text = text.replace("\x00", "")
        total += len(text)
        if total > MAX_TEXT:
            raise InvalidSource("source_text_too_large")
        if not text.strip():
            continue
        ids = codec.encode_ordinary(text)
        decoded, offsets = codec.decode_with_offsets(ids)
        line_offsets = [m.start() for m in re.finditer("\n", decoded)] if lines else []
        for start in range(0, len(ids), 360):
            end = min(start + 420, len(ids))
            left = offsets[start]
            right = offsets[end] if end < len(ids) else len(decoded)
            body = decoded[left:right].strip()
            if body:
                label = location
                if lines:
                    label = f"Lines {bisect_right(line_offsets, left) + 1}–{bisect_right(line_offsets, right - 1) + 1}"
                result.append((label[:300], body))
                if len(result) > MAX_CHUNKS:
                    raise InvalidSource("too_many_passages")
            if end == len(ids):
                break
    if not result:
        raise InvalidSource("no_extractable_text")
    return result


def extract(kind: str, content: bytes, url: str | None, hosts: set[str]):
    if kind == "web":
        final_url, content = fetch_html(url, hosts)
        return final_url, chunk_sections(html_sections(content))
    if kind == "pdf":
        return None, chunk_sections(pdf_sections(content))
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise InvalidSource("expected_utf8") from None
    if "\x00" in text:
        raise InvalidSource("expected_utf8")
    if kind == "markdown":
        return None, chunk_sections(markdown_sections(text))
    return None, chunk_sections([("", text)], lines=True)


def isolated_extract(kind: str, content: bytes, url: str | None, hosts: set[str]):
    config = json.dumps([kind, url, sorted(hosts)]).encode("utf-8")
    env = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TIKTOKEN_CACHE_DIR", "SSL_CERT_FILE")
        if name in os.environ
    }
    try:
        process = subprocess.run(
            [sys.executable, "-m", "hub.extract"],
            input=config + b"\n" + content,
            capture_output=True,
            timeout=25,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if kind == "web":
            raise SourceUnavailable("website_timeout") from None
        raise InvalidSource("parser_limit") from None
    if process.returncode:
        raise InvalidSource("parser_limit")
    outcome = json.loads(process.stdout)
    if "error" in outcome:
        error_type = SourceUnavailable if outcome.get("transient") else InvalidSource
        raise error_type(outcome["error"])
    return outcome["url"], outcome["chunks"]


def main():
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (18, 20))
    kind, url, hosts = json.loads(sys.stdin.buffer.readline(8192))
    try:
        final_url, chunks = extract(kind, sys.stdin.buffer.read(), url, set(hosts))
        outcome = {"url": final_url, "chunks": chunks}
    except (InvalidSource, SourceUnavailable) as exc:
        outcome = {"error": str(exc), "transient": isinstance(exc, SourceUnavailable)}
    sys.stdout.buffer.write(json.dumps(outcome).encode("utf-8"))


if __name__ == "__main__":
    main()
