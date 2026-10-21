"""Approximate centres of Malaysian towns and industrial areas.

Used to drop a first map pin when an agent names a town, and by the offline
search rules to recognise place names. Agents move the pin to the exact spot
on the review page. Coordinates are town centres, accurate to a few km.
"""

STATES = [
    "Johor", "Kedah", "Kelantan", "Kuala Lumpur", "Labuan", "Melaka", "Negeri Sembilan", "Pahang", "Penang",
    "Perak", "Perlis", "Putrajaya", "Sabah", "Sarawak", "Selangor", "Terengganu",
]

# name: (state, latitude, longitude)
AREAS = {
    "Shah Alam": ("Selangor", 3.0733, 101.5185),
    "Klang": ("Selangor", 3.0449, 101.4456),
    "Port Klang": ("Selangor", 2.9990, 101.3920),
    "Bukit Raja": ("Selangor", 3.0870, 101.4600),
    "Kota Kemuning": ("Selangor", 3.0050, 101.5400),
    "Subang Jaya": ("Selangor", 3.0500, 101.5850),
    "Puchong": ("Selangor", 3.0250, 101.6170),
    "Bandar Kinrara": ("Selangor", 3.0500, 101.6450),
    "Petaling Jaya": ("Selangor", 3.1073, 101.6067),
    "Sungai Buloh": ("Selangor", 3.2050, 101.5800),
    "Rawang": ("Selangor", 3.3210, 101.5770),
    "Kajang": ("Selangor", 2.9930, 101.7870),
    "Semenyih": ("Selangor", 2.9500, 101.8430),
    "Bangi": ("Selangor", 2.9170, 101.7820),
    "Balakong": ("Selangor", 3.0300, 101.7500),
    "Cyberjaya": ("Selangor", 2.9220, 101.6500),
    "Kuala Lumpur": ("Kuala Lumpur", 3.1390, 101.6869),
    "Nilai": ("Negeri Sembilan", 2.8160, 101.7970),
    "Seremban": ("Negeri Sembilan", 2.7260, 101.9380),
    "Senawang": ("Negeri Sembilan", 2.6950, 101.9750),
    "Melaka": ("Melaka", 2.1896, 102.2501),
    "Johor Bahru": ("Johor", 1.4927, 103.7414),
    "Senai": ("Johor", 1.6000, 103.6500),
    "Kulai": ("Johor", 1.6560, 103.6030),
    "Pasir Gudang": ("Johor", 1.4700, 103.9000),
    "Tebrau": ("Johor", 1.5500, 103.7800),
    "Batu Pahat": ("Johor", 1.8500, 102.9330),
    "Bayan Lepas": ("Penang", 5.2950, 100.2600),
    "Perai": ("Penang", 5.3800, 100.3900),
    "Batu Kawan": ("Penang", 5.2400, 100.4300),
    "Bukit Minyak": ("Penang", 5.3200, 100.4600),
    "Butterworth": ("Penang", 5.4000, 100.3700),
    "Kulim": ("Kedah", 5.3650, 100.5600),
    "Ipoh": ("Perak", 4.5975, 101.0901),
    "Kuantan": ("Pahang", 3.8077, 103.3260),
    "Gebeng": ("Pahang", 3.9700, 103.3800),
    "Kota Kinabalu": ("Sabah", 5.9804, 116.0735),
    "Kuching": ("Sarawak", 1.5533, 110.3592),
}
ALIASES = {"prai": "Perai", "jb": "Johor Bahru", "pj": "Petaling Jaya", "kl": "Kuala Lumpur", "malacca": "Melaka",
           "pulau pinang": "Penang", "georgetown": "Penang", "bk5": "Bandar Kinrara",
           "bandar puteri puchong": "Puchong", "bandar puchong jaya": "Puchong"}

MALAYSIA_CENTRE = (4.2, 102.0)


def lookup(name: str | None):
    """(state, lat, lng) for a town name, or None."""
    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key).lower()
    # Exact name first, then the longest known name inside it ("Bandar Kinrara, Puchong" is Bandar Kinrara).
    for area in sorted(AREAS, key=lambda a: (a.lower() != key, -len(a))):
        if area.lower() == key or area.lower() in key:
            return AREAS[area]
    return None


def locate(*texts: str | None):
    """(state, lat, lng) for the first text that names a known town, or None."""
    for text in texts:
        found = lookup(text) or lookup(find_area(text or ""))
        if found:
            return found
    return None


def find_area(text: str) -> str | None:
    """First known town mentioned in free text (longest names first)."""
    low = f" {text.lower()} "
    for area in sorted(AREAS, key=len, reverse=True):
        if f" {area.lower()}" in low:
            return area
    for alias, area in ALIASES.items():
        if f" {alias} " in low and area in AREAS:
            return area
    return None


def find_state(text: str) -> str | None:
    low = text.lower()
    for state in STATES:
        if state.lower() in low:
            return state
    if "pulau pinang" in low:
        return "Penang"
    return None
