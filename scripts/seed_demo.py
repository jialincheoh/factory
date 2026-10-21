"""Fill a local database with made-up factory and warehouse listings so the buyer side
has something to search. Never run against production.

    python -m scripts.seed_demo

Creates (or resets) demo@example.com with password "demo-password-123" and
replaces that account's listings. Other accounts are not touched. Every
listing, price and photo here is invented.
"""
import io
import random
import uuid

from PIL import Image, ImageDraw

from app import config, db, geo, images, security

# title, type, deal, area, acres, built-up sqft, amps, kVA, ceiling ft, tenure, lease yrs, zoning,
# container, loading bays, price, features, headline, body
DEMO = [
    ("Detached factory with 600A power in Shah Alam", "factory", "sale", "Shah Alam", 2.0, 45000, 600, None, 32,
     "freehold", None, "medium_industrial", True, 4, 28_000_000, ["office", "guardhouse", "near highway"],
     "Freehold detached factory, 600A, minutes from the Federal Highway",
     "A two-acre freehold site with a 45,000 sqft single-storey factory and a three-storey office block. 600A three-phase supply, 32 ft clear height and four loading bays with 40-ft container access. Guardhouse and fenced perimeter. Suited to manufacturing or regional distribution."),
    ("Semi-D factory for rent in Bukit Raja", "factory", "rent", "Bukit Raja", 0.5, 12000, 200, None, 24,
     "leasehold", 60, "light_industrial", True, 1, 22_000, ["office", "mezzanine"],
     "Ready semi-D factory near Klang, 200A",
     "Semi-detached factory with 12,000 sqft built-up including a mezzanine office. 200A supply, 24 ft ceiling and container access through a wide side road. Light industrial zoning. Move in after minor touch-ups."),
    ("Logistics warehouse near Port Klang", "warehouse", "rent", "Port Klang", 3.5, 80000, None, 1000, 40,
     "leasehold", 45, "medium_industrial", True, 8, 95_000, ["sprinkler", "loading dock", "near highway"],
     "80,000 sqft warehouse, 8 docks, 15 minutes from Westport",
     "Purpose-built logistics warehouse on 3.5 acres. 40 ft clear height, sprinkler system, eight dock levellers and a wide container yard. 1,000 kVA substation on site. Close to Westport and Northport for import and export operations."),
    ("Detached factory in Batu Kawan", "factory", "sale", "Batu Kawan", 3.0, 60000, None, 1500, 35,
     "leasehold", 55, "medium_industrial", True, 6, 38_000_000, ["office", "sprinkler", "guardhouse"],
     "Modern Batu Kawan factory with 1,500 kVA supply",
     "Three-acre site in Batu Kawan with a 60,000 sqft factory built for electronics and precision manufacturing. 1,500 kVA power, 35 ft ceiling, six loading bays and a two-storey office. Leasehold with 55 years remaining."),
    ("Warehouse with high ceiling in Kulim", "warehouse", "sale", "Kulim", 2.5, 50000, 800, None, 38,
     "leasehold", 50, "light_industrial", True, 5, 24_000_000, ["office", "overhead crane"],
     "Kulim warehouse with overhead crane, 38 ft ceiling",
     "A 50,000 sqft warehouse on 2.5 acres with a 10-tonne overhead crane, 38 ft clear height and 800A supply. Five loading bays with container access. Near Kulim Hi-Tech Park."),
    ("Terrace factory for rent in Puchong", "factory", "rent", "Puchong", 0.15, 5500, 100, None, 18,
     "freehold", None, "light_industrial", False, 1, 9_500, ["office"],
     "Affordable terrace factory in Puchong",
     "Two-storey terrace factory with a ground-floor workshop and first-floor office, 5,500 sqft in total. 100A supply and 18 ft ceiling. Lorry access; not suitable for 40-ft containers. Good for light assembly, storage or a showroom."),
    ("Heavy industrial factory in Pasir Gudang", "factory", "sale", "Pasir Gudang", 4.0, 70000, None, 2000, 45,
     "leasehold", 40, "heavy_industrial", True, 6, 30_000_000, ["overhead crane", "office", "substation"],
     "Heavy industrial plant with 2,000 kVA and cranes",
     "Heavy industrial factory on four acres in Pasir Gudang, close to Johor Port. 70,000 sqft with 45 ft ceiling, two overhead cranes, 2,000 kVA substation and six loading bays. Suited to fabrication, chemicals or heavy engineering."),
]

PALETTES = [((198, 214, 230), (176, 184, 192)), ((214, 224, 236), (160, 170, 180)), ((222, 210, 190), (190, 180, 168))]


def _canvas(sky):
    img = Image.new("RGB", (1200, 800), sky)
    return img, ImageDraw.Draw(img)


def _label(d):
    d.rectangle([880, 744, 1180, 780], fill=(0, 0, 0))   # bottom right, clear of the card badges
    d.text((892, 754), "Demo photo (made up)", fill=(255, 255, 255))


def exterior(seed):
    sky, wall = PALETTES[seed % 3]
    img, d = _canvas(sky)
    d.rectangle([0, 600, 1200, 800], fill=(120, 120, 118))                      # yard
    d.rectangle([120, 330, 1080, 600], fill=wall)                              # shed
    for i in range(8):                                                          # sawtooth roof
        x = 120 + i * 120
        d.polygon([(x, 330), (x + 120, 330), (x + 120, 260)], fill=(140, 150, 160))
    for i in range(4):                                                          # roller doors
        x = 200 + i * 220
        d.rectangle([x, 440, x + 150, 600], fill=(225, 225, 220))
        for y in range(450, 600, 14):
            d.line([x, y, x + 150, y], fill=(200, 200, 196))
    d.rectangle([900, 360, 1060, 600], fill=(240, 240, 236))                    # office block
    for y in (380, 450, 520):
        d.rectangle([915, y, 1045, y + 45], fill=(120, 160, 190))
    _label(d)
    return img


def interior(seed):
    img, d = _canvas((70, 72, 76))
    d.polygon([(0, 800), (1200, 800), (900, 520), (300, 520)], fill=(150, 150, 146))  # floor
    d.rectangle([300, 120, 900, 520], fill=(95, 98, 104))                             # far wall
    for x in (200, 420, 780, 1000):                                                   # columns
        d.rectangle([x, 80, x + 24, 800 - abs(600 - x) // 6], fill=(185, 120, 50))
    for i in range(6):                                                                # high-bay lights
        x = 250 + i * 140
        d.ellipse([x, 140, x + 40, 160], fill=(255, 250, 220))
    d.rectangle([340, 430, 520, 520], fill=(60, 110, 160))                            # racking
    d.rectangle([680, 430, 860, 520], fill=(60, 110, 160))
    _label(d)
    return img


def loading_bay(seed):
    img, d = _canvas(PALETTES[seed % 3][0])
    d.rectangle([0, 560, 1200, 800], fill=(110, 110, 108))
    d.rectangle([0, 250, 1200, 560], fill=PALETTES[seed % 3][1])
    for i in range(3):
        x = 120 + i * 360
        d.rectangle([x, 330, x + 240, 560], fill=(60, 60, 64))
        d.rectangle([x - 10, 540, x + 250, 570], fill=(230, 190, 40))
    d.rectangle([560, 420, 1150, 640], fill=(200, 60, 50))                            # container on trailer
    d.ellipse([600, 620, 680, 700], fill=(30, 30, 30)); d.ellipse([1040, 620, 1120, 700], fill=(30, 30, 30))
    _label(d)
    return img


def photos_for(kind: str, seed: int):
    return [exterior(seed), interior(seed), loading_bay(seed)]


def _jpeg(img) -> bytes:
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def reset_demo_listings(conn, agent_id):
    ids = [r["listing_id"] for r in conn.execute(
        "SELECT listing_id FROM listings WHERE real_estate_agent_id = %s", [agent_id]).fetchall()]
    if not ids:
        return
    for r in conn.execute("SELECT file_name FROM listing_images WHERE listing_id = ANY(%s)", [ids]).fetchall():
        (config.UPLOAD_DIR / r["file_name"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM ad_events WHERE ad_id IN (SELECT ad_id FROM published_ads WHERE listing_id = ANY(%s))", [ids])
    conn.execute("DELETE FROM ad_daily_stats WHERE ad_id IN (SELECT ad_id FROM published_ads WHERE listing_id = ANY(%s))", [ids])
    conn.execute("DELETE FROM published_ads WHERE listing_id = ANY(%s)", [ids])
    conn.execute("DELETE FROM rankings WHERE request_id IN (SELECT request_id FROM generation_requests WHERE listing_id = ANY(%s))", [ids])
    conn.execute("DELETE FROM decisions WHERE request_id IN (SELECT request_id FROM generation_requests WHERE listing_id = ANY(%s))", [ids])
    conn.execute("DELETE FROM candidates WHERE request_id IN (SELECT request_id FROM generation_requests WHERE listing_id = ANY(%s))", [ids])
    conn.execute("DELETE FROM generation_requests WHERE listing_id = ANY(%s)", [ids])
    conn.execute("DELETE FROM outcomes WHERE listing_id = ANY(%s)", [ids])
    conn.execute("DELETE FROM listings WHERE listing_id = ANY(%s)", [ids])  # listing_images cascade


def main():
    rng = random.Random(7)
    with db.connect() as conn:
        agent = conn.execute("SELECT real_estate_agent_id FROM real_estate_agents WHERE email = 'demo@example.com'").fetchone()
        if agent:
            agent_id = agent["real_estate_agent_id"]
            reset_demo_listings(conn, agent_id)
        else:
            agent_id = uuid.uuid4()
            conn.execute("INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
                         "VALUES (%s, 'Demo Agent', 'demo@example.com', %s, '+60 12-000 0000')",
                         [agent_id, security.hash_password("demo-password-123")])
        for i, (title, ptype, deal, area, acres, sqft, amps, kva, ceiling, tenure, lease, zoning, container,
                bays, price, feats, headline, body) in enumerate(DEMO):
            state, lat, lng = geo.lookup(area)
            lat += rng.uniform(-0.02, 0.02)  # scatter around the town centre
            lng += rng.uniform(-0.02, 0.02)
            lid = uuid.uuid4()
            conn.execute(
                "INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                "location, state, land_area_acres, size_sqft, power_amps, power_kva, ceiling_height_ft, tenure, "
                "lease_years_left, zoning, container_access, loading_bays, price_listed, features, latitude, longitude, "
                "description_original) VALUES (%s,%s,'published',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [lid, agent_id, title, ptype, deal, area, state, acres, sqft, amps, kva, ceiling, tenure, lease, zoning,
                 container, bays, price, feats, lat, lng, body])
            for pos, img in enumerate(photos_for(ptype, i), 1):
                data, w, h = images.process_upload(_jpeg(img))
                conn.execute("INSERT INTO listing_images (image_id, listing_id, position, file_name, width, height) "
                             "VALUES (%s,%s,%s,%s,%s,%s)", [uuid.uuid4(), lid, pos, images.save(data, conn), w, h])
            conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                         "VALUES (%s,%s,'own_portal',now() - make_interval(hours => %s),%s,%s)",
                         [uuid.uuid4(), lid, i, headline, body])
    print(f"Seeded {len(DEMO)} made-up factory and warehouse listings. Agent login: demo@example.com / demo-password-123")


if __name__ == "__main__":
    main()
