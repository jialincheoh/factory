"""Fill a local database with made-up listings so the buyer side has
something to search. Never run against production.

    python -m scripts.seed_demo

Creates demo@example.com with password "demo-password-123" (local only).
"""
import io
import uuid

from PIL import Image, ImageDraw

from app import db, images, security

DEMO = [
    ("3-bedroom bungalow with pool in Hillcrest", "bungalow", "Hillcrest", 3, 3, 2400, 1_250_000, ["pool", "garden", "parking"],
     "Sunny family bungalow with a private pool", "A bright three-bedroom bungalow on a quiet street, with a private pool, a lawn for the kids and covered parking for two cars."),
    ("2-bedroom condo near the river", "condo", "Riverside", 2, 2, 950, 520_000, ["gym", "balcony", "parking"],
     "Riverside condo with balcony views", "Wake up to river views from your balcony. Two bedrooms, two baths, a residents' gym and secure parking, ten minutes from the city centre."),
    ("4-bedroom townhouse with garden", "townhouse", "Oakfield", 4, 3, 1900, 780_000, ["garden", "renovated"],
     "Renovated townhouse with a real garden", "Recently renovated four-bedroom townhouse with an open kitchen and a mature back garden. Walking distance to two schools."),
    ("Studio apartment in the city centre", "studio", "City Centre", 0, 1, 420, 260_000, ["furnished", "gym"],
     "Fully furnished city studio", "A compact, fully furnished studio in the heart of the city. Building gym, concierge and a train station across the road."),
    ("3-bedroom condo with pool and sea view", "condo", "Harbourside", 3, 2, 1350, 890_000, ["pool", "sea view", "gym", "parking"],
     "Sea-view condo with resort facilities", "Three bedrooms with uninterrupted sea views, a 50-metre pool, gym and two parking bays. Ideal for families or as a rental investment."),
]
COLOURS = [(196, 168, 130), (120, 150, 170), (150, 170, 120), (180, 140, 150), (110, 160, 175)]


def placeholder(i: int, label: str) -> bytes:
    img = Image.new("RGB", (800, 600), COLOURS[i % len(COLOURS)])
    d = ImageDraw.Draw(img)
    d.polygon([(200, 330), (400, 170), (600, 330)], fill=(250, 248, 244))
    d.rectangle([250, 330, 550, 500], fill=(250, 248, 244))
    d.rectangle([370, 400, 430, 500], fill=COLOURS[i % len(COLOURS)])
    d.text((20, 20), label, fill=(255, 255, 255))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def main():
    with db.connect() as conn:
        agent = conn.execute("SELECT real_estate_agent_id FROM real_estate_agents WHERE email = 'demo@example.com'").fetchone()
        if agent:
            agent_id = agent["real_estate_agent_id"]
        else:
            agent_id = uuid.uuid4()
            conn.execute("INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
                         "VALUES (%s, 'Demo Agent', 'demo@example.com', %s, '+00 1234 5678')",
                         [agent_id, security.hash_password("demo-password-123")])
        for i, (title, ptype, loc, bd, ba, sqft, price, feats, headline, body) in enumerate(DEMO):
            lid = uuid.uuid4()
            conn.execute(
                "INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, location, "
                "bedrooms, bathrooms, size_sqft, price_listed, features, description_original) "
                "VALUES (%s,%s,'published',%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [lid, agent_id, title, ptype, loc, bd, ba, sqft, price, feats, body])
            data, w, h = images.process_upload(placeholder(i, "Demo photo (made up)"))
            conn.execute("INSERT INTO listing_images (image_id, listing_id, position, file_name, width, height) "
                         "VALUES (%s,%s,1,%s,%s,%s)", [uuid.uuid4(), lid, images.save(data), w, h])
            conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                         "VALUES (%s,%s,'own_portal',now(),%s,%s)", [uuid.uuid4(), lid, headline, body])
    print(f"Seeded {len(DEMO)} made-up listings. Agent login: demo@example.com / demo-password-123")


if __name__ == "__main__":
    main()
