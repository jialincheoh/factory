"""Mask contact and identity details before text goes to the model provider or the search log.

Agents sometimes put their phone number in a description, and buyers sometimes
type theirs into the search box. Neither is needed by the model: buyers reveal
the agent's phone on the listing page (which is how enquiries are counted), and
searches need only the requirements. Pattern-based, so it catches the common
Malaysian formats rather than every possible one; the privacy notice still asks
buyers not to type personal details.
"""
import re

PHONE = "[phone]"
EMAIL = "[email]"
IC = "[IC number]"

_PATTERNS = [
    # MyKad number: YYMMDD-PB-###G, with or without dashes
    (IC, re.compile(r"(?<!\d)\d{6}-?\d{2}-?\d{4}(?!\d)")),
    (EMAIL, re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    # Mobile (01x) and landline (03-09) numbers, local or +60; separators are spaces or dashes,
    # never commas or dots, so prices (RM 4,500,000) and areas (0.5 acre) are left alone.
    (PHONE, re.compile(r"(?<![\w.,])(?:\+?6\s?0|0)[\s-]?(?:1\d|[3-9])[\s-]?\d{3,4}[\s-]?\d{4}(?!\d)")),
]


def mask(text: str) -> str:
    for placeholder, pattern in _PATTERNS:
        text = pattern.sub(placeholder, text)
    return text
