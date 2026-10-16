"""Rule-based stand-in used when LLM_MODEL=offline.

It lets the site run and the tests pass without an API key. It does not look
at photos or translate; the UI says so on every result it produces. It knows
the common English and Malay terms for factories and warehouses.
"""
import re
import time
from typing import Callable

from .. import geo
from . import pii
from .llm import DECLINE_REPLIES
from .schemas import (AMPS_PER_KVA, AdDraft, AdResult, ChatResult, ListingAnalysis, Mismatch, PropertyCard,
                      SearchFilters)

FEATURES = {
    "overhead crane": ["overhead crane", "crane"],
    "mezzanine": ["mezzanine"],
    "office": ["office"],
    "sprinkler": ["sprinkler"],
    "guardhouse": ["guardhouse", "guard house"],
    "substation": ["substation", "pencawang"],
    "fenced": ["fenced", "fencing", "pagar"],
    "flat land": ["flat land", "tanah rata"],
    "near highway": ["highway", "lebuhraya"],
    "parking": ["parking", "car park", "carpark"],
}
# Order matters: the first match wins.
TYPES = [
    ("warehouse", ["warehouse", "gudang", "logistics hub"]),
    ("factory", ["factory", "factories", "kilang", "plant"]),
]
# Requests the site cannot serve, checked only when no factory or warehouse word is present.
OUT_OF_SCOPE = [
    ("residential", ["house", "condo", "apartment", "rumah", "kondo", "bungalow", "terrace"]),
    ("shop_or_office", ["shop lot", "shoplot", "shop office", "office space", "kedai"]),
]
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(NUMBER_WORDS) + r")"
_MONEY = r"(?:rm|\$)?\s*([\d,]+(?:\.\d+)?)\s*(k|m|mil|million|juta)?\b"


def _n(token: str) -> float:
    return float(token.replace(",", "")) if token[0].isdigit() else NUMBER_WORDS[token]


def _money(amount: str, unit: str | None) -> float:
    value = float(amount.replace(",", ""))
    unit = (unit or "").lower()
    if unit == "k":
        value *= 1_000
    elif unit in ("m", "mil", "million", "juta"):
        value *= 1_000_000
    return value


def _features(text: str) -> list[str]:
    return [tag for tag, words in FEATURES.items() if any(w in text for w in words)]


def _types(text: str) -> list[str]:
    for tag, words in TYPES:
        if any(re.search(rf"\b{w}s?\b", text) for w in words):
            return [tag]
    return []


def _acres(text: str):
    m = re.search(rf"{_NUM}\s*(?:acres?|ekar)\b", text)
    if m:
        return _n(m.group(1))
    m = re.search(rf"{_NUM}\s*(?:hectares?|ha)\b", text)
    return round(_n(m.group(1)) * 2.471, 2) if m else None


def _amps(text: str):
    m = re.search(r"(\d[\d,]*)\s*(?:a|amp|amps|ampere)\b", text)
    if m:
        return int(m.group(1).replace(",", "")), None
    m = re.search(r"(\d[\d,]*)\s*kva\b", text)
    return (None, int(m.group(1).replace(",", ""))) if m else (None, None)


def _ceiling(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ft|feet|')\s*(?:clear\s*)?(?:ceiling|height|tinggi)", text) or \
        re.search(r"(?:ceiling|height)[^\d]{0,15}(\d+(?:\.\d+)?)\s*(?:ft|feet|')", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*m(?:etres?|eters?)?\s*(?:ceiling|height)", text)
    return round(float(m.group(1)) * 3.281, 1) if m else None


def _tenure(text: str):
    if "freehold" in text or "pegangan bebas" in text:
        return "freehold"
    if "leasehold" in text or "pajakan" in text:
        return "leasehold"
    return None


def _zoning(text: str):
    for z in ("light", "medium", "heavy"):
        if re.search(rf"\b{z}[\s-]*industr", text):
            return f"{z}_industrial"
    return None


def _deal(text: str):
    if re.search(r"\b(rent|rental|lease|sewa|to let|for let)\b", text):
        return "rent"
    if re.search(r"\b(sale|buy|dijual|jual)\b", text):
        return "sale"
    return None


def _container(text: str) -> bool:
    return bool(re.search(r"container|trailer|40\s*-?\s*f(?:ee)?t", text))


def _record(recorder, step: str, started: float) -> None:
    if recorder is not None:
        recorder.calls.append({"step": step, "model": OfflineAssistant.model_name, "temperature": None,
                               "latency_ms": int((time.monotonic() - started) * 1000), "ok": True, "error": None,
                               "served_by": OfflineAssistant.model_name, "input_tokens": None, "output_tokens": None})


class OfflineAssistant:
    model_name = "offline-rules"

    def __init__(self, currency: str = "$"):
        self.currency = currency

    def settings(self) -> dict:
        return {"rules": "offline"}

    def write_ads(self, description: str, images: list[tuple[bytes, str]], n_drafts: int, recorder=None):
        started = time.monotonic()
        description = pii.mask(description)
        text = description.lower()
        price = None
        m = re.search(r"(?:rm|\$|price|harga|asking|rent|sewa)\s*:?\s*(?:rm|\$)?\s*([\d,]+(?:\.\d+)?)\s*(k|m|mil|million|juta)?\b", text)
        if m:
            price = _money(m.group(1), m.group(2))
        built = re.search(r"([\d,]{3,})\s*(?:sq\.?\s*ft|sqft|square feet|kaki persegi)\s*(?:built|floor|bangunan)?", text)
        area = geo.find_area(description)
        amps, kva = _amps(text)
        types = _types(text)
        card = PropertyCard(
            title="",
            property_type=types[0] if types else "other",
            deal_type=_deal(text) or "sale",
            location=area,
            state=geo.lookup(area)[0] if area else geo.find_state(description),
            price=price,
            land_area_acres=_acres(text),
            size_sqft=int(built.group(1).replace(",", "")) if built else None,
            ceiling_height_ft=_ceiling(text),
            power_amps=amps,
            power_kva=kva,
            tenure=_tenure(text),
            zoning=_zoning(text),
            container_access=True if _container(text) else None,
            features=_features(text),
        )
        kind = card.property_type.replace("_", " ") if card.property_type != "other" else "property"
        where = f" in {card.location}" if card.location else ""
        deal = "for rent" if card.deal_type == "rent" else "for sale"
        card.title = f"{kind.capitalize()} {deal}{where}"

        specs = [s for s in [
            f"{card.land_area_acres:g} acres of land" if card.land_area_acres else None,
            f"{card.size_sqft:,} sqft built-up" if card.size_sqft else None,
            f"{card.power_amps}A power" if card.power_amps else (f"{card.power_kva} kVA power" if card.power_kva else None),
            f"{card.ceiling_height_ft:g} ft ceiling" if card.ceiling_height_ft else None,
            card.tenure,
            "40-ft container access" if card.container_access else None,
        ] if s]
        spec_line = ", ".join(specs) or "practical specifications"
        angles = [
            (f"{card.title}", f"{description.strip()} Key specs: {spec_line}. Contact us to arrange a site visit."),
            (f"Ready for operations{where}", f"Built for logistics and manufacturing: {spec_line}. {description.strip()}"),
            (f"Strategic {kind}{where}", f"A sound long-term asset for owner-occupiers and investors. {description.strip()}"),
        ]
        # Without a model the photos are not looked at: each draft simply leads with a different photo.
        drafts = [AdDraft(headline=h[:70], body=b, cover_photo=i % len(images) + 1 if images else None)
                  for i, (h, b) in enumerate((angles * n_drafts)[:n_drafts])]
        analysis = ListingAnalysis(
            detected_language="not detected (offline mode)",
            english_description=description.strip(),
            card=card,
            mismatches=[Mismatch(
                claim=f"{len(images)} photo(s) uploaded",
                finding="Photos were not checked against the description: the site is running in offline mode "
                        "without a language model.",
                severity="info",
            )],
            drafts=drafts,
        )
        # Drafts reuse the description and card as written, so there is nothing to check.
        _record(recorder, "analyse", started)
        return AdResult(analysis, f"[offline rules, {len(images)} photo(s)]\n{description}", self.model_name,
                        [{"checked": False, "unsupported_claims": [], "earlier_versions": []} for _ in drafts])

    def search_chat(self, history: list[dict], run_search: Callable[[SearchFilters], list[dict]],
                    current: SearchFilters | None = None, recorder=None) -> ChatResult:
        started = time.monotonic()
        latest = history[-1]["content"].lower() if history else ""
        if not _types(latest):
            for reason, words in OUT_OF_SCOPE:
                if any(re.search(rf"\b{w}s?\b", latest) for w in words):
                    _record(recorder, "search_chat", started)
                    return ChatResult(DECLINE_REPLIES[reason], None, None, self.model_name, declined=reason)
        # Start from the filter bar, then apply requirements from every buyer
        # turn so follow-ups refine the search.
        f = current.model_copy(deep=True) if current else SearchFilters()
        for turn in (t for t in history if t["role"] == "user"):
            text = pii.mask(turn["content"]).lower()
            f.deal_type = _deal(text) or f.deal_type
            f.property_types = _types(text) or f.property_types
            f.features = sorted(set(f.features) | set(_features(text)))
            area = geo.find_area(turn["content"])
            if area:
                f.location, f.state = area, None
            elif geo.find_state(turn["content"]):
                f.state, f.location = geo.find_state(turn["content"]), None
            acres = re.search(rf"(?:at least|min(?:imum)?|over|above|more than)?\s*{_NUM}\s*(?:acres?|ekar)", text)
            if acres:
                f.min_land_acres = _n(acres.group(1))
            amps, kva = _amps(text)
            if amps or kva:
                f.min_power_amps = amps or round(kva * AMPS_PER_KVA)
            f.min_ceiling_ft = _ceiling(text) or f.min_ceiling_ft
            f.tenure = _tenure(text) or f.tenure
            if _zoning(text):
                f.zoning = [_zoning(text)]
            if _container(text):
                f.container_access = True
            under = re.findall(r"(?:under|below|less than|max|up to|budget|bawah)\s*" + _MONEY, text)
            if under:
                f.max_price = _money(*under[-1])

        results = run_search(f)
        n = len(results)
        reply = (f"I found {n} listing{'s' if n != 1 else ''} that match{'es' if n == 1 else ''}." if n
                 else "Nothing matches all of that yet. Try dropping a requirement or widening the area.")
        _record(recorder, "search_chat", started)
        return ChatResult(reply + " (Offline mode: simple keyword matching.)", f, results, self.model_name)
