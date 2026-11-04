"""Pure text helpers: file -> sections -> size-capped, sentence-safe chunks. No I/O."""

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.config.errors import DomainValidationError

MAX_CHARS = 1200
OVERLAP_CHARS = 150
CHUNKING_PARAMS = {"strategy": "headings+sentences", "max_chars": MAX_CHARS, "overlap_chars": OVERLAP_CHARS}
SUPPORTED = (".md", ".txt", ".html", ".htm", ".jsonl", ".pdf")

_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENT = re.compile(r"(?<=[.!?…:;])\s+")


@dataclass
class Section:
    heading: str | None
    text: str
    extra: dict[str, Any] = field(default_factory=dict)
    atomic: bool = False  # True -> never split (diagnostic cases)


@dataclass
class Parsed:
    title: str | None
    sections: list[Section]


# ---------------------------------------------------------------- markdown / txt
def parse_markdown(text: str) -> Parsed:
    title: str | None = None
    stack: list[tuple[int, str]] = []
    sections: list[Section] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(" > ".join(h for _, h in stack) or None, body))
        buf.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        level, head = len(m.group(1)), m.group(2).strip()
        if title is None and level == 1:
            title = head
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, head))
    flush()
    return Parsed(title, sections)


def parse_text(text: str) -> Parsed:
    body = text.replace("\r\n", "\n").strip()
    return Parsed(None, [Section(None, body)] if body else [])


# ---------------------------------------------------------------- html
_SKIP = {
    "nav", "script", "style", "header", "footer", "aside", "noscript", "svg", "form", "template",
    "button", "select", "dialog", "iframe", "bread-crumbs",
}  # fmt: skip
_BLOCK = {"p", "div", "li", "br", "tr", "pre", "section", "article", "ul", "ol", "table", "blockquote"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# class/id fragments that mark page chrome (menus, banners, feedback widgets, ...)
_NOISE = re.compile(
    r"(?:^|[-_ ])(?:breadcrumbs?|cookies?|consent|sidebar|toc|menu|navbar|feedback|modal|signin|sign-in|"
    r"skip|share|social|pagination|rating|banner|popover|content-header|article-header|action-panel|"
    r"related)(?:$|[-_ ])",
    re.I,
)
_MAIN = re.compile(r"<(?:main|article)[\s>]", re.I)
_ANCHOR = re.compile(r"[\u00b6]+")  # pilcrow permalinks


class _Html(HTMLParser):
    def __init__(self, restrict_main: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, is_noise, is_main)
        self._noise = 0
        self._main = 0
        self._restrict = restrict_main
        self._in_title = False
        self._head_level = 0

    def _emitting(self) -> bool:
        return not self._noise and (self._main > 0 or not self._restrict)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if tag not in _VOID:
            a = dict(attrs)
            is_main = tag in ("main", "article") or a.get("role") == "main"
            noisy = (
                not is_main
                and tag not in ("html", "body")
                and (
                    tag in _SKIP
                    or bool(_NOISE.search(" ".join(filter(None, [a.get("class"), a.get("id")]))))
                    or a.get("aria-hidden") == "true"
                )
            )
            self._stack.append((tag, noisy, is_main))
            self._noise += noisy
            self._main += is_main
        if not self._emitting():
            return
        if tag in ("h1", "h2", "h3"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
            self._head_level = int(tag[1])
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if tag not in _VOID:
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i][0] == tag:
                    for _, noisy, is_main in self._stack[i:]:
                        self._noise -= noisy
                        self._main -= is_main
                    del self._stack[i:]
                    break
        if not self._emitting():
            return
        if tag in ("h1", "h2", "h3"):
            self.out.append("\n\n")
            self._head_level = 0
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._emitting():
            data = _ANCHOR.sub("", data)
            self.out.append(re.sub(r"\s+", " ", data) if self._head_level else data)


def parse_html(raw: str) -> Parsed:
    p = _Html(restrict_main=bool(_MAIN.search(raw)))
    p.feed(raw)
    p.close()
    text = re.sub(r"[ \t]+", " ", "".join(p.out))
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    parsed = parse_markdown(text)
    parsed.title = parsed.title or p.title.strip() or None
    return parsed


# ---------------------------------------------------------------- RFC / PDF plain text
_RFC_HEAD = re.compile(r"^(?:RFC \d+\s.*\s\w+\s\d{4}|RFC \d+|\s*[A-Z][a-z]+ \d{4})\s*$")
_PAGE_FOOT = re.compile(r"^.*\[Page \d+\]\s*$")
_NUM_HEAD = re.compile(r"^(\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)\.?\s{1,4}([A-Z][^\n]{2,100}?)\s*$")
_DROP_SECTIONS = re.compile(
    r"^(table of contents|contents|status of this memo|copyright notice|authors?'? addresses|"
    r"full copyright statement|intellectual property|acknowledg(e)?ments?|author'?s? address(es)?)\b",
    re.I,
)


def is_rfc_text(text: str) -> bool:
    return bool(re.search(r"^Request for Comments:\s*\d+|^Internet Engineering Task Force", text[:3000], re.M))


def _unwrap(lines: list[str]) -> str:
    """Join hard-wrapped prose; keep indented / diagram blocks verbatim."""
    out: list[str] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        pre = any(len(ln) - len(ln.lstrip()) >= 5 or re.search(r"[|+]-{2,}|-{3,}\+", ln) for ln in block)
        if pre or len(block) == 1:
            out.append("\n".join(x.rstrip() for x in block))
        else:
            out.append(re.sub(r"\s+", " ", " ".join(block)))
        block.clear()

    for ln in lines:
        if ln.strip():
            block.append(ln.rstrip())
        else:
            flush()
    flush()
    return "\n\n".join(out)


_RFC_ART = re.compile(r"^\s*(?:\+[-+=]{6,}\+?|(?:\d\s+){6,}\d?|[-=_.]{8,})\s*$")  # diagram rulers / rules
_PLAIN_HEAD = re.compile(r"^[A-Z][A-Za-z0-9 ,/()'-]{3,70}$")


def _numbered_sections(lines: list[str], plain_titles: bool = False) -> Parsed:
    """Split cleaned lines on column-0 numbered headings ('3.1.  Title'); heading path keeps the parents."""
    sections: list[Section] = []
    titles: dict[str, str] = {}
    cur_head: str | None = None
    cur_num: str | None = None
    buf: list[str] = []
    numbered = sum(1 for ln in lines if _NUM_HEAD.match(ln))
    plain_titles = plain_titles and numbered < 3  # old RFCs (e.g. 792) have unnumbered section titles

    def flush() -> None:
        text = _unwrap(buf).strip()
        buf.clear()
        if not text or (cur_head and _DROP_SECTIONS.match(cur_head.split(" > ")[-1].split(". ", 1)[-1])):
            return
        sections.append(Section(cur_head, text))

    for i, ln in enumerate(lines):
        m = _NUM_HEAD.match(ln) if ln[:1].isalnum() else None
        if plain_titles and _PLAIN_HEAD.match(ln) and (i == 0 or not lines[i - 1].strip()):
            flush()
            cur_head = ln.strip()
            continue
        if m and not re.search(r"\.{3,}|\s\d+$|\.$|[\[\]]|^\d+\.?\s+See\s", ln):
            flush()
            cur_num, label = m.group(1), f"{m.group(1)}. {m.group(2)}"
            titles[cur_num] = label
            parts = cur_num.split(".")
            chain = [titles[".".join(parts[:i])] for i in range(1, len(parts)) if ".".join(parts[:i]) in titles]
            cur_head = " > ".join([*chain, label])
        elif ln[:1].isalpha() and ln.rstrip() in ("Abstract", "Introduction"):
            flush()
            cur_head = ln.strip()
        elif ln[:1].isalpha() and ln.strip() in ("Status of This Memo", "Copyright Notice", "Table of Contents"):
            flush()
            cur_head = ln.strip()
        else:
            buf.append(ln)
    flush()
    if any(sec.heading for sec in sections):  # front matter (authors, acknowledgements) is not knowledge
        sections = [sec for sec in sections if sec.heading]
    return Parsed(None, sections)


def parse_rfc(text: str) -> Parsed:
    lines = []
    for ln in text.replace("\r\n", "\n").replace("\f", "\n").split("\n"):
        if _PAGE_FOOT.match(ln) or _RFC_HEAD.match(ln) or _RFC_ART.match(ln):
            continue
        lines.append(ln)
    return _numbered_sections(lines, plain_titles=True)


def parse_pdf(data: bytes) -> Parsed:
    from io import BytesIO

    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(BytesIO(data))
        pages = [(pg.extract_text() or "").split("\n") for pg in reader.pages]
        meta_title = (reader.metadata.title if reader.metadata else None) or None
    except (PyPdfError, ValueError, OSError) as e:
        raise DomainValidationError(f"unreadable PDF ({e})") from e
    # running headers/footers: lines repeated on >= 40% of pages (page numbers normalised)
    norm = lambda ln: re.sub(r"\d+", "#", ln.strip())  # noqa: E731
    freq: dict[str, int] = {}
    for pg in pages:
        for k in {norm(ln) for ln in pg if ln.strip()}:
            freq[k] = freq.get(k, 0) + 1
    limit = max(3, int(len(pages) * 0.4))
    lines: list[str] = []
    for pg in pages:
        for ln in pg:
            if ln.strip() and freq.get(norm(ln), 0) >= limit and len(ln.strip()) < 90:
                continue
            lines.append(ln.strip())
        lines.append("")
    # undo end-of-line hyphenation
    joined = re.sub(r"(\w)-\n(\w)", r"\1\2", "\n".join(lines))
    parsed = _numbered_sections(joined.split("\n"))
    if not parsed.sections:  # no numbered headings: treat as plain text
        parsed = Parsed(None, [Section(None, _unwrap(joined.split("\n")))])
    parsed.title = meta_title
    return parsed


# ---------------------------------------------------------------- jsonl cases
_CASE_LABELS = [
    ("symptoms", "Síntomas"),
    ("initial_questions", "Preguntas iniciales"),
    ("evidence_to_collect", "Evidencias a recolectar"),
    ("possible_causes", "Causas posibles"),
    ("safe_checks", "Comprobaciones seguras"),
    ("resolution", "Resolución"),
    ("escalation_conditions", "Condiciones de escalamiento"),
    ("source_refs", "Fuentes"),
    ("expected_diagnosis", "Diagnóstico esperado"),
]


def parse_jsonl_cases(raw: str) -> Parsed:
    sections: list[Section] = []
    for n, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
            if not isinstance(case, dict):
                raise ValueError("not an object")
        except ValueError as e:
            raise DomainValidationError(f"jsonl line {n}: invalid JSON case ({e})") from e
        cid, ctitle = str(case.get("case_id", f"line-{n}")), str(case.get("title", ""))
        parts = [f"Caso {cid}: {ctitle}".strip()]
        for key, label in _CASE_LABELS:
            v = case.get(key)
            if not v:
                continue
            if isinstance(v, list):
                parts.append(f"{label}:\n" + "\n".join(f"- {x}" for x in v))
            else:
                parts.append(f"{label}: {v}")
        extra = {"case_id": cid, "case_type": case.get("case_type")}
        sections.append(Section(f"{cid} {ctitle}".strip(), "\n\n".join(parts), extra, atomic=True))
    return Parsed(None, sections)


def parse_file(filename: str, data: bytes) -> Parsed:
    name = filename.lower()
    if not name.endswith(SUPPORTED):
        raise DomainValidationError(f"unsupported file type (supported: {', '.join(SUPPORTED)})")
    if name.endswith(".pdf"):
        return parse_pdf(data)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DomainValidationError("file must be UTF-8 text") from e
    if name.endswith(".md"):
        return parse_markdown(text)
    if name.endswith(".jsonl"):
        return parse_jsonl_cases(text)
    if name.endswith((".html", ".htm")):
        return parse_html(text)
    return parse_rfc(text) if is_rfc_text(text) else parse_text(text)


# ---------------------------------------------------------------- size cap
def _hard_split(s: str, limit: int) -> list[str]:
    out: list[str] = []
    while len(s) > limit:
        cut = s.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    return out + ([s] if s else [])


def _units(text: str, limit: int) -> list[tuple[str, str]]:
    """(sentence, separator-before). Sentences never exceed `limit`."""
    units: list[tuple[str, str]] = []
    for para in re.split(r"\n\s*\n", text):
        first = True
        for line_block in para.split("\n"):
            for sent in _SENT.split(line_block.strip()):
                if not sent:
                    continue
                for piece in _hard_split(sent, limit):
                    sep = "" if not units else ("\n\n" if first else "\n")
                    units.append((piece, sep))
                    first = False
    return units


def split_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    units = _units(text, max_chars)
    chunks: list[str] = []
    cur: list[tuple[str, str]] = []
    size = 0
    for u in units:
        add = len(u[0]) + len(u[1] if cur else "")
        if cur and size + add > max_chars:
            chunks.append(_join(cur))
            keep: list[tuple[str, str]] = []
            tot = 0
            for prev in reversed(cur):  # whole trailing sentences only
                if tot + len(prev[0]) > overlap:
                    break
                keep.insert(0, prev)
                tot += len(prev[0]) + len(prev[1])
            cur = keep if keep and tot + len(u[0]) <= max_chars else []
            size = len(_join(cur)) if cur else 0
            add = len(u[0]) + (len(u[1]) if cur else 0)
        cur.append(u)
        size += add
    if cur:
        chunks.append(_join(cur))
    return chunks


def _join(units: list[tuple[str, str]]) -> str:
    return "".join((sep if i else "") + s for i, (s, sep) in enumerate(units))


def build_chunks(parsed: Parsed, base_meta: dict[str, Any]):
    """Sections -> ChunkDraft list (chunk_index continuous across the document)."""
    from .models import ChunkDraft

    drafts: list[ChunkDraft] = []
    for sec in parsed.sections:
        pieces = [sec.text] if sec.atomic else split_text(sec.text)
        leaf = (sec.heading or "").split(" > ")[-1]
        for piece in pieces:
            if leaf and not sec.atomic and not piece.startswith(leaf):
                piece = f"{leaf}\n\n{piece}"  # keep the topic in the text the embedder / LLM sees
            drafts.append(
                ChunkDraft(
                    chunk_index=len(drafts),
                    content=piece,
                    section=sec.heading,
                    metadata={**base_meta, **sec.extra, "section": sec.heading},
                )
            )
    return drafts


# ---------------------------------------------------------------- full text (document reader)
def _heading_line(section: str) -> str:
    parts = section.split(" > ")
    return "#" * (min(len(parts), 3) + 1) + " " + parts[-1]


def full_text_from_parsed(parsed: Parsed) -> str:
    """Readable markdown-ish text of the whole document: section headings + original section text."""
    out: list[str] = []
    for sec in parsed.sections:
        if sec.heading:
            out.append(_heading_line(sec.heading))
        out.append(sec.text.strip())
    return "\n\n".join(p for p in out if p)


def _overlap(a: str, b: str, limit: int = 200) -> int:
    for n in range(min(limit, len(a), len(b)), 0, -1):
        if a.endswith(b[:n]):
            return n
    return 0


def rebuild_full_text(chunks: list[dict[str, Any]]) -> str:
    """Rebuild the text from ordered chunks: drop the overlap repeated between consecutive chunks and keep section
    headings (the chunker already prepends the leaf heading to the first piece of each section)."""
    out = ""
    prev_sec: str | None = None
    for c in chunks:
        sec, body = c.get("section"), (c.get("content") or "").strip()
        leaf = (sec or "").split(" > ")[-1]
        has_head = bool(leaf) and (body == leaf or body.startswith(leaf + "\n\n"))
        if sec != prev_sec or not out:
            if has_head:
                body = body[len(leaf) :].lstrip()
            head = _heading_line(sec) if sec else ""
            body = head + ("\n\n" if head and body else "") + body
            out += ("\n\n" if out else "") + body
        else:
            if has_head:
                body = body[len(leaf) :].lstrip()
            n = _overlap(out, body)
            body = body[n:]
            out += (("\n\n" if n == 0 else "") + body) if body else ""
        prev_sec = sec
    return out


def locate_chunks(full_text: str, chunks: list[dict[str, Any]]) -> list[tuple[int | None, int | None]]:
    """Best-effort (start, end) of each chunk inside full_text; (None, None) when it cannot be found."""
    res: list[tuple[int | None, int | None]] = []
    cursor = 0
    for c in chunks:
        sec, body = c.get("section"), (c.get("content") or "").strip()
        leaf = (sec or "").split(" > ")[-1]
        if leaf and body.startswith(leaf + "\n\n"):
            body = body[len(leaf) :].lstrip()
        if not body:
            res.append((None, None))
            continue
        i = full_text.find(body, max(0, cursor - 400))
        if i >= 0:
            res.append((i, i + len(body)))
            cursor = i
            continue
        head = full_text.find(body[:60], max(0, cursor - 400))
        if head < 0:
            res.append((None, None))
            continue
        tail = full_text.find(body[-60:], head)
        res.append((head, tail + 60 if tail >= 0 else None))
        cursor = head
    return res


def excerpt(text: str, n: int = 450) -> str:
    """Chunk text trimmed to ~n chars at a sentence boundary (falls back to a word boundary)."""
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    head = t[:n]
    cut = max(head.rfind(". "), head.rfind("? "), head.rfind("! "), head.rfind("; "))
    if cut >= n * 0.5:
        return head[: cut + 1]
    return head.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
