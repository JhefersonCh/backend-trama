# ruff: noqa: E501
"""Photo classification with a constrained JSON answer + defensive normalization of whatever the model returns."""

import base64
import json
import logging
import re
import unicodedata
from typing import Any

log = logging.getLogger("trama.media")

DEVICES = ("modem", "router", "ont", "pantalla", "otro")
LABELS = ("internet", "los", "pon", "power", "wifi", "lan", "otro")
COLORS = ("verde", "roja", "naranja", "apagada")
MAX_LEDS = 12

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "device": {"type": "string", "enum": list(DEVICES)},
        "leds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "enum": list(LABELS)},
                    "color": {"type": "string", "enum": list(COLORS)},
                    "blink": {"type": "boolean"},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                },
                "required": ["label", "color", "blink", "bbox"],
            },
        },
        "error_text": {"type": ["string", "null"]},
        "screen_text": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["device", "leds", "error_text", "screen_text", "confidence"],
}

PROMPT = (
    "Eres un clasificador de fotos de equipos de internet del hogar (módem, router, ONT de fibra o la pantalla "
    "de un dispositivo). Mira la foto y responde SOLO con un JSON, sin texto extra.\n"
    "- device: modem | router | ont | pantalla | otro.\n"
    "- leds: una entrada por cada luz indicadora visible. label: internet | los | pon | power | wifi | lan | otro "
    "(LOS es la luz de pérdida de señal de la fibra; PON es la luz de enlace óptico). "
    "color: verde | roja | naranja | apagada. blink: true si parece parpadear (si no lo puedes saber, false). "
    "bbox: [x, y, ancho, alto] de la luz, números entre 0 y 1 relativos al tamaño de la imagen "
    "(x, y = esquina superior izquierda).\n"
    "- error_text: texto de error legible en una pantalla o etiqueta, o null.\n"
    "- screen_text: si la imagen es una captura o foto de una pantalla con texto (por ejemplo la salida de ipconfig, "
    "ping, tracert o una página de error), transcribe ese texto tal cual, línea por línea, sin resumir; si no, null.\n"
    "- confidence: número entre 0 y 1 con qué tan seguro estás.\n"
    "No inventes luces que no se vean. Si no es un equipo de red, device = otro y leds = []."
)

_DEVICE = {
    "modem": "modem", "módem": "modem", "router": "router", "enrutador": "router", "ont": "ont", "onu": "ont",
    "ont/onu": "ont", "pantalla": "pantalla", "screen": "pantalla", "monitor": "pantalla", "display": "pantalla",
}  # fmt: skip
_LABEL = {
    "internet": "internet", "wan": "internet", "globe": "internet", "globo": "internet", "online": "internet",
    "los": "los", "alarm": "los", "alarma": "los", "pon": "pon", "optical": "pon", "fibra": "pon",
    "power": "power", "pwr": "power", "alimentacion": "power", "encendido": "power", "energia": "power",
    "wifi": "wifi", "wi-fi": "wifi", "wlan": "wifi", "wireless": "wifi", "inalambrico": "wifi",
    "lan": "lan", "ethernet": "lan", "eth": "lan",
}  # fmt: skip
_COLOR = {
    "verde": "verde", "green": "verde", "roja": "roja", "rojo": "roja", "red": "roja", "naranja": "naranja",
    "orange": "naranja", "ambar": "naranja", "amber": "naranja", "amarillo": "naranja", "yellow": "naranja",
    "apagada": "apagada", "apagado": "apagada", "off": "apagada", "none": "apagada", "ninguno": "apagada",
    "sin luz": "apagada", "oscura": "apagada",
}  # fmt: skip


def _plain(v: Any) -> str:
    s = unicodedata.normalize("NFKD", str(v).strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return _plain(v) in ("true", "si", "yes", "1", "parpadea", "blink", "blinking")
    return bool(v)


def _bbox(raw: Any, size: tuple[int, int] | None) -> list[float] | None:
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    nums = [_num(x) for x in raw]
    if any(n is None for n in nums):
        return None
    x, y, w, h = (float(n) for n in nums)  # type: ignore[arg-type]
    peak = max(abs(x), abs(y), abs(w), abs(h))
    if peak > 1.5:  # the model answered in percent, 0-1000 or pixels instead of 0..1
        div = 100.0 if peak <= 100 else 1000.0 if peak <= 1000 else None
        if div:
            x, y, w, h = x / div, y / div, w / div, h / div
        elif size:
            x, y, w, h = x / size[0], y / size[1], w / size[0], h / size[1]
        else:
            return None
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    w, h = min(max(w, 0.0), 1.0 - x), min(max(h, 0.0), 1.0 - y)
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


def extract_json(text: str) -> Any:
    """Parse the model's answer even if it is wrapped in a code fence or has chatter around the object."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if 0 <= a < b:
        return json.loads(t[a : b + 1])
    raise ValueError("no JSON object in the model answer")


def normalize(raw: Any, size: tuple[int, int] | None = None) -> dict[str, Any]:
    """Whatever the model produced -> the contract `analysis` shape. Never raises for odd values."""
    if not isinstance(raw, dict):
        raise ValueError("model answer is not a JSON object")
    dev = _DEVICE.get(_plain(raw.get("device", "")), "otro")
    leds_in = raw.get("leds")
    if isinstance(leds_in, dict):
        leds_in = [leds_in]
    leds: list[dict[str, Any]] = []
    for item in leds_in if isinstance(leds_in, list) else []:
        if not isinstance(item, dict):
            continue
        color = _COLOR.get(_plain(item.get("color", "")))
        if color is None:  # an LED whose colour we cannot read is worse than no LED: facts would be wrong
            continue
        label = _LABEL.get(_plain(item.get("label", "")), "otro")
        led: dict[str, Any] = {"label": label, "color": color, "blink": _bool(item.get("blink", False))}
        led["bbox"] = _bbox(item.get("bbox"), size)
        leds.append(led)
        if len(leds) >= MAX_LEDS:
            break
    err = raw.get("error_text")
    err = " ".join(str(err).split())[:300] if isinstance(err, str | int | float) and str(err).strip() else None
    if err and _plain(err) in ("null", "none", "ninguno", "n/a", "no hay", "sin texto"):
        err = None
    conf = _num(raw.get("confidence"))
    if conf is not None and conf > 1:  # "85" meaning percent
        conf = conf / 100 if conf <= 100 else 1.0
    st = raw.get("screen_text")
    st = str(st).strip()[:2000] if isinstance(st, str) and st.strip() and _plain(st) not in ("null", "none") else None
    return {
        "device": dev,
        "leds": leds,
        "error_text": err,
        "screen_text": st,
        "confidence": round(min(max(conf if conf is not None else 0.5, 0.0), 1.0), 2),
    }


def _body_params(kind: str, profile_params: dict[str, Any]) -> dict[str, Any]:
    """Constrained output: llama.cpp enforces the JSON schema by grammar; other OpenAI-compatible servers get
    json_object and the schema is already described in the prompt."""
    if kind == "llamacpp":
        fmt: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {"name": "foto_equipo", "strict": True, "schema": SCHEMA},
        }
    else:
        fmt = {"type": "json_object"}
    extra = dict(profile_params.get("extra_body") or {})
    extra["response_format"] = fmt
    return {**profile_params, "extra_body": extra}


async def classify(lease: Any, jpeg: bytes, size: tuple[int, int]) -> dict[str, Any]:
    """Run one classification on a leased vision model. Raises on transport/parse failure (caller handles)."""
    from .images import shrink_for_model

    params = dict(lease.profile.params or {})
    small = shrink_for_model(jpeg, int(params.get("vision_max_px") or 896))
    url = "data:image/jpeg;base64," + base64.b64encode(small).decode()
    prompt = PROMPT
    if lease.adapter.kind != "llamacpp":
        prompt += "\nEsquema JSON: " + json.dumps(SCHEMA, ensure_ascii=False)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    params.setdefault("temperature", 0.1)
    params.setdefault("max_tokens", 400)
    text = ""
    async for d in lease.adapter.chat(
        params.get("router_model") or lease.profile.model_ref,
        messages,
        None,
        False,
        _body_params(lease.adapter.kind, params),
    ):
        text += d.content
    if not text.strip():
        raise ValueError("empty answer from the vision model")
    return normalize(extract_json(text), size)
