"""Incremental sentence splitter for streamed LLM text (feeds TTS).

A sentence ends at . ! ? … or newline, but only once we SEE the char after the terminator (streaming!) and it is
whitespace. So `192.168.1.1` (dot followed by digit) never splits, and neither do abbreviations (Sr., Dra., p. ej.),
single-letter initials, or list markers like `1.` at the start of a line.
"""

import re

_ABBREV = {
    "sr", "sra", "srta", "dr", "dra", "ing", "lic", "prof", "ud", "uds", "vs", "etc", "ej", "fig", "tel", "av",
    "gral", "pag", "aprox", "max", "min", "cap", "num", "no", "e.g", "i.e", "p.ej", "ca", "dto", "depto",
}  # fmt: skip
_TERMINATORS = ".!?…"
_MAX_BUFFER = 240


def _is_abbrev(buf: str, i: int) -> bool:
    start = i
    while start > 0 and not buf[start - 1].isspace():
        start -= 1
    token = buf[start:i].lower().strip("(\"'¿¡")
    token_plain = token.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    if token_plain in _ABBREV:
        return True
    if len(token) == 1 and token.isalpha():  # initials, "p. ej."
        return True
    if token.isdigit() and (start == 0 or buf[start - 1] == "\n"):  # list marker "1. "
        return True
    return False


_MD = re.compile(r"[*_`#>]+|^\s*[-•]\s+", re.M)


def clean_for_speech(text: str) -> str:
    return re.sub(r"\s+", " ", _MD.sub("", text)).strip()


class SentenceSplitter:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        while (cut := self._find_cut()) is not None:
            sentence, self._buf = self._buf[:cut], self._buf[cut:]
            if clean_for_speech(sentence):
                out.append(sentence.strip())
        if len(self._buf) > _MAX_BUFFER:  # no punctuation for a long time: cut at the last comma/space
            cut = max(self._buf.rfind(", "), self._buf.rfind("; "))
            cut = cut + 1 if cut > 40 else self._buf.rfind(" ")
            if cut > 0:
                sentence, self._buf = self._buf[:cut], self._buf[cut:]
                out.append(sentence.strip())
        return out

    def flush(self) -> str | None:
        rest, self._buf = self._buf, ""
        return rest.strip() if clean_for_speech(rest) else None

    def _find_cut(self) -> int | None:
        buf = self._buf
        for i, ch in enumerate(buf):
            if ch == "\n":
                return i + 1
            if ch in _TERMINATORS and i + 1 < len(buf):
                j = i
                while j + 1 < len(buf) and buf[j + 1] in _TERMINATORS:  # "?!" / "..."
                    j += 1
                if j + 1 >= len(buf):
                    return None  # need one more char to decide
                if buf[j + 1].isspace() and not (ch == "." and _is_abbrev(buf, i)):
                    return j + 1
        return None
