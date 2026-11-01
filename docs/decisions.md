# Design decisions

Each entry gives the choice, the reason, and what would change it.

## One service and one database

Everything runs in one FastAPI process backed by Postgres, including vector search, LangGraph checkpoints, photos,
videos and the work queues.

**Why:** traffic is low and the host is a free plan, so another moving part would add failure modes with no benefit.
Everything also commits and backs up together.

**Would change if:** `ad_events` impressions outgrow the database first, since they grow fastest. Those would move to a
log pipeline. Video bytes would move to object storage once database size matters.

## Photos and videos stored in Postgres

Photos and videos are `BYTEA` rows, with `UPLOAD_DIR` as a disk cache that refills on demand.

**Why:** the production host's disk is wiped on every restart. Accepting a larger database was simpler than adding
object storage this early.

**Cost:** database size and backup time. `deploy/` already sketches a storage bucket for the larger setup.

## The model proposes, code decides

The model never writes SQL, never produces a number that gets computed, and never publishes anything. Its output goes
through a Pydantic schema, then through code or a person.

**Why:** a wrong power rating or tenure in an ad has real consequences, and prompt instructions alone do not prevent it.

## Every draft is stored, in a recorded random order

Rejected drafts, display position, shuffle seed, full prompt and raw response are all kept.

**Why:** without rejected drafts you cannot compare what agents chose with what they passed over. Without position and
seed you cannot separate "agents keep the best draft" from "agents keep whatever appears first". Model output is not
reproducible, so a version label alone cannot show what the agent saw.

**Cost:** storage, and `candidates.raw_response` holds the full analysis for each draft.

## Prompts are versioned by content

Each prompt version is pinned to a hash of its text. A test fails if the text changes without a new version, and the
monitor alerts if production logs one version with two hashes.

**Why:** evaluation results are meaningless if "v7" can quietly mean two different prompts.

## Market check: the model gathers, code decides

The Deep Agent is not told the listing's price, so it cannot look for evidence that fits it. It cites sources by IDs
that the search tool issued, so it cannot invent a link. Code drops a comparable unless its price and area appear in the
cited source, does all the arithmetic, and gives no verdict with fewer than three comparables.

It is labelled "Market check" and "Not a valuation", and only the listing's agent can see it. Asking prices are not sale
prices.

## Video tours use real photos only

ffmpeg adds camera movement over the listing's own photos, rather than a generative video model.

**Why:** generated footage could show a building or condition that does not exist, which is a misrepresentation of the
property. The same reasoning made ad visuals a choice among real photos, not generated images.

## Server-rendered pages with little JavaScript

**Why:** search engines are the main source of buyers, the filter form should work without scripts, and a strict
Content-Security-Policy with no inline scripts is easy to keep when there is little script at all.

**Cost:** rich agent interactions take more effort than they would in a client-side app.

## Provider chosen by configuration

`init_chat_model` takes `provider:model`, with an optional fallback model.

**Why:** quotas and model availability change often. The stand-in used by tests and the demo sits behind the same
interface.

**Cost:** structured output differs by provider, so behaviour needs re-checking with an evaluation after a switch.

## Locked dependencies

`requirements.in` lists direct dependencies and `requirements.txt` is the generated lock, so production and CI install
the same versions.

**Why:** LangChain's packages move quickly, and an unpinned rebuild could change behaviour with no code change.

## Migrations are applied by a script

Compose used to rely on Postgres's init directory. That directory runs files in name order, which puts `V10` before
`V2`, so `scripts/migrate.py` is now the only path, locally and in production.
