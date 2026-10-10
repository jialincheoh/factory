"""Structured outputs the language model must return.

LangChain turns these Pydantic models into the provider's structured-output
or tool schema, so the field descriptions double as instructions.
"""
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field

PropertyType = Literal[
    "factory", "warehouse", "industrial_land", "commercial_land", "agricultural_land", "shop_office",
    "condo", "apartment", "landed", "townhouse", "bungalow", "studio", "other",
]
PROPERTY_TYPES = get_args(PropertyType)
LAND_TYPES = ("industrial_land", "commercial_land", "agricultural_land")
LANDED_TYPES = ("landed", "bungalow", "townhouse")  # "a house"

DealType = Literal["sale", "rent"]
Tenure = Literal["freehold", "leasehold"]
Zoning = Literal["light_industrial", "medium_industrial", "heavy_industrial", "commercial", "agricultural",
                 "residential", "mixed"]
ZONINGS = get_args(Zoning)

AMPS_PER_KVA = 1000 / (415 * 3 ** 0.5)  # 415 V three-phase, about 1.39 A per kVA


class PropertyCard(BaseModel):
    """The structured facts shown on every ad and used by buyer search."""
    title: str = Field(description="Short factual title, e.g. 'Detached factory with 400A power in Shah Alam'")
    property_type: PropertyType
    deal_type: DealType = Field(description="'rent' if the property is for rent or lease (sewa); otherwise 'sale' (dijual)")
    location: Optional[str] = Field(None, description="Town or industrial area, e.g. 'Shah Alam', 'Senai', only if stated")
    state: Optional[str] = Field(None, description="Malaysian state, e.g. Selangor, Johor, Penang, only if stated or obvious from the town")
    price: Optional[float] = Field(None, gt=0, description="Asking price, or monthly rent for rentals, as a plain number in RM")
    land_area_acres: Optional[float] = Field(None, gt=0, description="Land area in acres (ekar); convert from sqft (43,560 sqft per acre) or hectares (2.471 acres per ha)")
    size_sqft: Optional[int] = Field(None, gt=0, description="Built-up floor area in sqft")
    ceiling_height_ft: Optional[float] = Field(None, gt=0, description="Clear ceiling height in feet; convert metres (3.281 ft per m)")
    power_amps: Optional[int] = Field(None, gt=0, description="Electricity supply if quoted in amps (e.g. 400A three-phase)")
    power_kva: Optional[int] = Field(None, gt=0, description="Electricity supply if quoted in kVA")
    tenure: Optional[Tenure] = Field(None, description="freehold (pegangan bebas) or leasehold (pajakan)")
    lease_years_left: Optional[int] = Field(None, gt=0, description="Years remaining on a leasehold title, if stated")
    zoning: Optional[Zoning] = Field(None, description="Land use or industrial category if stated (light/medium/heavy industrial, ...)")
    container_access: Optional[bool] = Field(None, description="True only if 40-ft container trailer access is stated or clearly visible")
    loading_bays: Optional[int] = Field(None, ge=0)
    bedrooms: Optional[int] = Field(None, ge=0, description="Residential properties only")
    bathrooms: Optional[int] = Field(None, ge=0, description="Residential properties only")
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


class ListingAnalysis(BaseModel):
    """Everything the model returns for one generation request."""
    detected_language: str = Field(description="Language of the agent's description, e.g. 'English', 'Malay', 'Chinese'")
    english_description: str = Field(description="Faithful English rendering of the description, polished but without new facts")
    card: PropertyCard
    mismatches: list[Mismatch] = Field(default_factory=list)
    drafts: list[AdDraft] = Field(description="Distinct ad drafts that differ in angle and tone")


class SearchFilters(BaseModel):
    """Filters the buyer-side assistant can apply to published listings."""
    deal_type: Optional[DealType] = Field(None, description="'rent' for rent/lease, 'sale' to buy; omit if not said")
    property_types: list[PropertyType] = Field(
        default_factory=list,
        description="Any of these types; empty means all. For 'land' list all three land types; "
                    "for 'house' list landed, bungalow and townhouse")
    location: Optional[str] = Field(None, description="Town or industrial area, partial match, e.g. 'Shah Alam'")
    state: Optional[str] = Field(None, description="Malaysian state, e.g. 'Johor'")
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
    min_bedrooms: Optional[int] = Field(None, ge=0)
    features: list[str] = Field(default_factory=list, description="Lowercase feature tags that must all be present, e.g. ['overhead crane']")
    keywords: Optional[str] = Field(None, description="Free-text words to look for in the ad text")
