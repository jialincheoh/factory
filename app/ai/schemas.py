"""Structured outputs the language model must return.

LangChain turns these Pydantic models into the provider's structured-output
or tool schema, so the field descriptions double as instructions.
"""
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field

PropertyType = Literal["condo", "apartment", "landed", "townhouse", "bungalow", "studio", "other"]
PROPERTY_TYPES = get_args(PropertyType)


class PropertyCard(BaseModel):
    """The structured facts shown on every ad and used by buyer search."""
    title: str = Field(description="Short factual title, e.g. '3-bedroom condo with pool in Riverside'")
    property_type: PropertyType
    location: Optional[str] = Field(None, description="Area or neighbourhood, only if stated in the description")
    bedrooms: Optional[int] = Field(None, ge=0)
    bathrooms: Optional[int] = Field(None, ge=0)
    size_sqft: Optional[int] = Field(None, gt=0)
    price: Optional[float] = Field(None, gt=0, description="Asking price as a plain number, only if stated")
    features: list[str] = Field(
        default_factory=list,
        description="Lowercase feature tags supported by the description or clearly visible in the photos, "
                    "e.g. pool, garden, balcony, parking, gym, sea view, furnished, renovated",
    )


class Mismatch(BaseModel):
    """A place where the photos and the description disagree or cannot be confirmed."""
    claim: str = Field(description="What the description says, or what a photo shows that the description omits")
    finding: str = Field(description="What the photos show instead, or why it cannot be confirmed")
    severity: Literal["info", "warning"] = Field(
        description="'warning' if a buyer could feel misled; 'info' for omissions or things worth mentioning")


class AdDraft(BaseModel):
    headline: str = Field(description="Ad headline, at most 70 characters")
    body: str = Field(description="Ad body in marketable English, 60-120 words, no claims beyond the facts")


class ListingAnalysis(BaseModel):
    """Everything the model returns for one generation request."""
    detected_language: str = Field(description="Language of the agent's description, e.g. 'English', 'Malay', 'Chinese'")
    english_description: str = Field(description="Faithful English rendering of the description, polished but without new facts")
    card: PropertyCard
    mismatches: list[Mismatch] = Field(default_factory=list)
    drafts: list[AdDraft] = Field(description="Distinct ad drafts that differ in angle and tone")


class SearchFilters(BaseModel):
    """Filters the buyer-side assistant can apply to published listings."""
    property_type: Optional[PropertyType] = Field(
        None, description="'landed' matches any house (bungalow, townhouse, landed); omit if the buyer has no preference")
    min_bedrooms: Optional[int] = Field(None, ge=0)
    max_bedrooms: Optional[int] = Field(None, ge=0)
    min_bathrooms: Optional[int] = Field(None, ge=0)
    min_price: Optional[float] = Field(None, ge=0)
    max_price: Optional[float] = Field(None, ge=0)
    location: Optional[str] = Field(None, description="Area or neighbourhood name, partial match")
    features: list[str] = Field(default_factory=list, description="Lowercase feature tags that must all be present, e.g. ['pool']")
    keywords: Optional[str] = Field(None, description="Free-text words to look for in the ad text")
