"""Audio intake: sniff the container from magic bytes, duration check where it can be read without ffmpeg."""

import io
import wave

MAX_SECONDS = 180

# mime -> (extension, magic check)


def sniff(raw: bytes) -> tuple[str, str] | None:
    """(mime, ext) from the first bytes, or None when it is not one of webm/ogg/mp3/wav/m4a."""
    h = raw[:16]
    if h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if h[:4] == b"OggS":
        return "audio/ogg", "ogg"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm", "webm"
    if h[:3] == b"ID3" or (len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        return "audio/mpeg", "mp3"
    if h[4:8] == b"ftyp":
        return "audio/mp4", "m4a"
    return None


def duration_seconds(raw: bytes, mime: str) -> float | None:
    """Only WAV is cheap to measure (stdlib). Others: None -> the caller falls back to the size limit."""
    if mime != "audio/wav":
        return None
    try:
        with wave.open(io.BytesIO(raw)) as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except (wave.Error, EOFError):
        return None
