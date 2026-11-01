# Security and privacy

## Measures in place

**Accounts and sessions**

- Argon2 password hashing, with a dummy hash compared for unknown emails so response time does not reveal which
  accounts exist.
- Login throttling: 10 failed attempts per IP per 15 minutes.
- Signed, HttpOnly, SameSite=Lax session cookies, Secure in production, rotated at sign-in.
- CSRF tokens on every agent form, compared in constant time.

**Requests and responses**

- Public POST endpoints accept only JSON or a custom header, so a cross-site form cannot reach them.
- Content-Security-Policy with no inline scripts or styles and `frame-ancestors 'none'`.
- Jinja autoescaping, and JSON-LD escaped for safe placement inside a script tag.
- Parameterised SQL throughout. The one dynamic column list comes from a fixed dictionary in `card_from_form`.
- Agents can see and change only their own listings and requests, which is enforced in each query. Photos of unpublished
  listings are served only to their owner.

**Uploads**

- Photos are decoded and re-encoded as JPEG, which rejects non-images and strips EXIF, including GPS.
- Decompression bombs are refused by a pixel limit. Sizes are capped for photos, audio and PDFs.
- Stored files get random names, validated by pattern before use.

**Model-specific**

- Phone numbers, emails and IC numbers are masked before reaching the model or the search log, and in drafts.
- Descriptions, photos and agent feedback are marked as data in the prompts. Search tools are read-only, and the
  model cannot write SQL.
- Rate limits on every route that calls a model, because each call costs money and quota.
- LangSmith tracing removes photos and refuses to start outside local development, because the privacy notice does not
  cover it.

**Privacy**

- Buyer searches are stored with a random visitor ID that is not linked to any account. A bilingual notice at `/privacy`
  describes this, and it must change whenever the logging changes.
- The demo account's password is in this repository, so migration V9 deletes that account from any database.

## Known limits

These are open and would be next in line if traffic or risk grew.

| Limit | Effect | Fix |
|---|---|---|
| Rate limits and login counters are in memory, per process. | They reset on restart, and the effective limit grows with the number of instances. | Move the counters to Postgres or Redis before running more than one instance. |
| Login throttling is per IP only. | A distributed attempt on one account is not slowed. | Add a per-account counter. |

Secrets live in the environment or the host's secret store and are never committed. `.env` is git-ignored.
