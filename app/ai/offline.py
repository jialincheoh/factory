"""Rule-based stand-in used when LLM_MODEL=offline.

It lets the site run and the tests pass without an API key. It does not look
at photos or translate; the UI says so on every result it produces.
"""
import re
from typing import Callable

from .schemas import AdDraft, ListingAnalysis, Mismatch, PropertyCard, SearchFilters

FEATURES = {
    "pool": ["pool", "swimming"],
    "garden": ["garden", "yard"],
    "balcony": ["balcony"],
    "parking": ["parking", "car park", "carpark", "garage"],
    "gym": ["gym"],
    "sea view": ["sea view", "ocean view"],
    "furnished": ["furnished"],
    "renovated": ["renovated"],
}
TYPES = {
    "condo": ["condo", "condominium"],
    "apartment": ["apartment", "flat"],
    "townhouse": ["townhouse", "terrace"],
    "bungalow": ["bungalow", "villa"],
    "studio": ["studio"],
    "landed": ["landed", "house"],
}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_NUM = r"(\d+|" + "|".join(NUMBER_WORDS) + r")"


def _num(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


def _money(amount: str, unit: str | None) -> float:
    value = float(amount.replace(",", ""))
    unit = (unit or "").lower()
    if unit == "k":
        value *= 1_000
    elif unit in ("m", "mil", "million"):
        value *= 1_000_000
    return value


def _features(text: str) -> list[str]:
    return [tag for tag, words in FEATURES.items() if any(w in text for w in words)]


def _type(text: str) -> str | None:
    for tag, words in TYPES.items():
        if any(re.search(rf"\b{w}s?\b", text) for w in words):
            return tag
    return None


def _count(text: str, nouns: str) -> int | None:
    m = re.search(rf"\b{_NUM}[\s-]*(?:{nouns})", text)
    return _num(m.group(1)) if m else None


class OfflineAssistant:
    model_name = "offline-rules"

    def __init__(self, currency: str = "$"):
        self.currency = currency

    def write_ads(self, description: str, images: list[tuple[bytes, str]], n_drafts: int):
        text = description.lower()
        price = None
        m = re.search(r"(?:\$|rm|usd|sgd|price|asking)\s*([\d,]+(?:\.\d+)?)\s*(k|m|mil|million)?\b", text)
        if m:
            price = _money(m.group(1), m.group(2))
        size = re.search(r"([\d,]{3,})\s*(?:sq\.?\s*ft|sqft|square feet)", text)
        loc = re.search(r"\bin ([A-Z][\w'-]*(?: [A-Z][\w'-]*)*)", description)

        card = PropertyCard(
            title="",
            property_type=_type(text) or "other",
            location=loc.group(1) if loc else None,
            bedrooms=_count(text, "bed|bedroom|br|bilik"),
            bathrooms=_count(text, "bath|bathroom"),
            size_sqft=int(size.group(1).replace(",", "")) if size else None,
            price=price,
            features=_features(text),
        )
        beds = f"{card.bedrooms}-bedroom " if card.bedrooms else ""
        kind = card.property_type if card.property_type != "other" else "home"
        where = f" in {card.location}" if card.location else ""
        card.title = f"{beds}{kind}{where}".strip().capitalize()

        perks = ", ".join(card.features) or "a practical layout"
        angles = [
            (f"Your next home: {card.title}", f"{description.strip()} Highlights include {perks}. Arrange a viewing today."),
            (f"Room to grow - {card.title}", f"Ideal for families. {description.strip()} Features: {perks}."),
            (f"Well-priced {kind}{where}", f"A sound choice for owners and investors alike. {description.strip()}"),
        ]
        drafts = [AdDraft(headline=h[:70], body=b) for h, b in (angles * n_drafts)[:n_drafts]]
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
        return analysis, f"[offline rules, {len(images)} photo(s)]\n{description}"

    def search_chat(self, history: list[dict], run_search: Callable[[SearchFilters], list[dict]]):
        # Read requirements from every buyer turn so follow-ups refine the search.
        text = " ".join(t["content"] for t in history if t["role"] == "user").lower()
        f = SearchFilters(features=_features(text), property_type=_type(text))
        f.min_bedrooms = _count(text, "bed|bedroom|br|bilik")
        f.min_bathrooms = _count(text, "bath|bathroom")
        under = re.findall(r"(?:under|below|less than|max|up to)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(k|m|mil|million)?\b", text)
        if under:
            f.max_price = _money(*under[-1])
        over = re.findall(r"(?:over|above|more than|at least|min)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(k|m|mil|million)?\b", text)
        if over:
            f.min_price = _money(*over[-1])

        results = run_search(f)
        n = len(results)
        reply = (f"I found {n} listing{'s' if n != 1 else ''} that match{'es' if n == 1 else ''}." if n
                 else "Nothing matches all of that yet. Try dropping a feature or raising the budget.")
        return reply + " (Offline mode: simple keyword matching.)", f, results
