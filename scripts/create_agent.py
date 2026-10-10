"""Create a real estate agent account.

    python -m scripts.create_agent --email you@example.com --name "Your Name" --phone "+00 0000 0000"

The password is prompted for, never passed on the command line (it would end
up in shell history).
"""
import argparse
import getpass
import uuid

from app import db, security


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--phone", default=None)
    args = p.parse_args()
    password = getpass.getpass("Password (12+ characters): ")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
            "VALUES (%s, %s, %s, %s, %s)",
            [uuid.uuid4(), args.name, args.email.strip().lower(), security.hash_password(password), args.phone])
    print(f"Created {args.email}")


if __name__ == "__main__":
    main()
