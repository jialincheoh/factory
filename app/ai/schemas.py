"""Structured outputs the language model must return.

LangChain turns these Pydantic models into the provider's structured-output
or tool schema, so the field descriptions double as instructions.
"""
from dataclasses import dataclass, field
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field

# The site lists factories, warehouses, industrial or commercial land, and whole commercial buildings
# (office buildings and towers). "other" lets the model say a description is something else (a shoplot,
# a single office unit, a house); such listings cannot be published.
PropertyType = Literal["factory", "warehouse", "land", "commercial_building", "other"]
PROPERTY_TYPES = ("factory", "warehouse", "land", "commercial_building")

DealType = Literal["sale", "rent"]
Tenure = Literal["freehold", "leasehold"]
Zoning = Literal["light_industrial", "medium_industrial", "heavy_industrial", "commercial"]
ZONINGS = get_args(Zoning)

AMPS_PER_KVA = 1000 / (415 * 3 ** 0.5)  # 415 V three-phase, about 1.39 A per kVA


class PropertyCard(BaseModel):
    """The structured facts shown on every ad and used by buyer search."""
    title: str = Field(description="Short factual title, e.g. 'Detached factory with 400A power in Shah Alam'")
    property_type: PropertyType = Field(description="factory, warehouse, land (industrial or commercial land without a "
                                                    "building), or commercial_building (a whole office building or tower); "
                                                    "'other' for something else, such as a shop, a single office unit or a house")
    deal_type: DealType = Field(description="'rent' if the property is for rent or lease (sewa); otherwise 'sale' (dijual)")
    location: Optional[str] = Field(None, description="Town or industrial area, e.g. 'Shah Alam', 'Senai', only if stated")
    state: Optional[str] = Field(None, description="State, e.g. Selangor, Johor, Penang, only if stated or obvious from the town")
    price: Optional[float] = Field(None, gt=0, description="Asking price, or monthly rent for rentals, as a plain number in RM")
    land_area_acres: Optional[float] = Field(None, gt=0, description="Land area in acres (ekar); convert from sqft (43,560 sqft per acre) or hectares (2.471 acres per ha)")
    size_sqft: Optional[int] = Field(None, gt=0, description="Built-up floor area in sqft")
    ceiling_height_ft: Optional[float] = Field(None, gt=0, description="Clear ceiling height in feet; convert metres (3.281 ft per m)")
    power_amps: Optional[int] = Field(None, gt=0, description="Electricity supply if quoted in amps (e.g. 400A three-phase)")
    power_kva: Optional[int] = Field(None, gt=0, description="Electricity supply if quoted in kVA")
    tenure: Optional[Tenure] = Field(None, description="freehold (pegangan bebas) or leasehold (pajakan)")
    lease_years_left: Optional[int] = Field(None, gt=0, description="Years remaining on a leasehold title, if stated")
    zoning: Optional[Zoning] = Field(None, description="Zoning if stated: light/medium/heavy industrial, or commercial")
    container_access: Optional[bool] = Field(None, description="True only if 40-ft container trailer access is stated or clearly visible")
    loading_bays: Optional[int] = Field(None, ge=0)
    features: list[str] = Field(
        default_factory=list,
        description="Lowercase feature tags supported by the description or clearly visible in the photos, "
                    "e.g. office, mezzanine, overhead crane, guardhouse, sprinkler, loading dock, substation, "
                    "near highway, fenced, flat land",
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
    cover_photo: Optional[int] = Field(
        None, ge=1, description="Number of the photo to show first with this draft: the one that best supports its "
                                "angle (e.g. loading bays for a logistics angle, the building front for an investment angle)")


class PhotoCaption(BaseModel):
    photo: int = Field(ge=1, description="Photo number, starting at 1")
    caption: str = Field(description="What the photo shows, at most 12 words, e.g. 'Loading bay with two roller "
                                     "shutters'. Only what is visible; no sizes, specs or condition you cannot see")


class ListingAnalysis(BaseModel):
    """Everything the model returns for one generation request."""
    detected_language: str = Field(description="Language of the agent's description, e.g. 'English', 'Malay', 'Chinese'")
    english_description: str = Field(description="Faithful English rendering of the description, polished but without new facts")
    card: PropertyCard
    mismatches: list[Mismatch] = Field(default_factory=list)
    drafts: list[AdDraft] = Field(description="Distinct ad drafts that differ in angle and tone")
    photo_captions: list[PhotoCaption] = Field(default_factory=list, description="One caption per photo")


class DraftVerdict(BaseModel):
    draft: int = Field(description="Draft number, starting at 1")
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Each factual claim in this draft that the facts do not support, quoted or closely "
                    "paraphrased; empty if the draft is fully supported")


class DraftCheck(BaseModel):
    """The checker's verdict on every draft."""
    verdicts: list[DraftVerdict]


class FixedDraft(BaseModel):
    draft: int = Field(description="Number of the draft being fixed")
    headline: str = Field(description="Ad headline, at most 70 characters")
    body: str = Field(description="Ad body in marketable English, 60-120 words, no claims beyond the facts")


class FixedDrafts(BaseModel):
    drafts: list[FixedDraft]


@dataclass
class AdResult:
    """What write_ads hands back to the web app (not a model output)."""
    analysis: ListingAnalysis
    prompt_text: str
    model_name: str                 # model(s) that actually answered, joined with "+"
    checks: list[dict] = field(default_factory=list)  # per draft, in the order of analysis.drafts


@dataclass
class ChatResult:
    """What search_chat hands back to the web app (not a model output)."""
    reply: str
    filters: Optional["SearchFilters"]  # None when the model did not search
    results: Optional[list[dict]]
    model_name: str
    declined: Optional[str] = None      # reason, when the request was outside what the site lists


class Transcript(BaseModel):
    """What a buyer said in a voice message."""
    transcript: str = Field(description="What the speaker said, word for word, in the language spoken; empty if "
                                        "there is no speech")
    language: str = Field(description="Language spoken, e.g. 'English', 'Malay', 'Chinese', or 'mixed'")


OutOfScopeReason = Literal["residential", "shop_or_office", "land_only", "unrelated"]


class OutOfScope(BaseModel):
    reason: OutOfScopeReason = Field(
        description="residential: houses, condos, apartments; shop_or_office: shop lots, shop offices, single office units "
                    "(whole office buildings are in scope); "
                    "land_only: farmland or residential land (industrial and commercial land are in scope); "
                    "unrelated: not about finding property")


class SearchFilters(BaseModel):
    """Filters the buyer-side assistant can apply to published listings."""
    deal_type: Optional[DealType] = Field(None, description="'rent' for rent/lease, 'sale' to buy; omit if not said")
    property_types: list[Literal["factory", "warehouse", "land", "commercial_building"]] = Field(
        default_factory=list,
        description="Any of these types; empty means all")
    location: Optional[str] = Field(None, description="Town or industrial area, partial match, e.g. 'Shah Alam'")
    state: Optional[str] = Field(None, description="State, e.g. 'Johor'")
    min_price: Optional[float] = Field(None, ge=0)
    max_price: Optional[float] = Field(None, ge=0, description="Total price for sale, monthly rent for rent")
    min_land_acres: Optional[float] = Field(None, ge=0)
    max_land_acres: Optional[float] = Field(None, ge=0)
    min_built_up_sqft: Optional[int] = Field(None, ge=0)
    min_ceiling_ft: Optional[float] = Field(None, ge=0)
    min_power_amps: Optional[int] = Field(None, ge=0, description="Minimum supply in amps; convert kVA with 1 kVA = 1.39 A")
    tenure: Optional[Tenure] = None
    zoning: list[Zoning] = Field(default_factory=list, description="Any of these zonings; empty means any")
    container_access: Optional[bool] = Field(None, description="True if the buyer needs container trailer access")
    min_loading_bays: Optional[int] = Field(None, ge=0)
    features: list[str] = Field(default_factory=list, description="Lowercase feature tags that must all be present, e.g. ['overhead crane']")
    keywords: Optional[str] = Field(None, description="Free-text words to look for in the ad text")
    semantic_query: Optional[str] = Field(
        None, description="What the buyer wants the property for, or qualities that are not a filter above, in a few "
                          "words, e.g. 'food processing', 'cold storage', 'heavy machinery assembly'. Ranks results by "
                          "meaning; it does not exclude any listing")
