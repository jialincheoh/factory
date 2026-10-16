# Ad draft rubric

How to score one ad draft. Used for the hand-labelled evaluation sets, for
checking agreement between two people, and later as the instructions for an
automatic judge (only once that judge agrees with the human scores).

The facts are the property card, the agent's description and the photo
findings. Nothing else counts as a fact.

## Gates (pass or fail)

A draft that fails either gate scores 0 overall, whatever its other scores.

**G1. Factual support.** Every factual claim is backed by the facts: numbers
(land and built-up area, power, ceiling height, price, loading bays,
distances, travel times), features, access, tenure, zoning, condition, nearby
places and roads. Unit conversions and rounding of stated facts are fine.
General selling language with nothing to check ("ideal for logistics") is fine.

- Pass: card says 400A; draft says "400A three-phase power".
- Fail: draft says "5 minutes to Westport"; no distance or port is in the facts.

**G2. Safe to publish.** No phone number, email address or placeholder such as
[phone] (buyers reveal the phone on the listing page). No feature that the
photo check flagged as a warning. Nothing that is not a factory or warehouse.

- Fail: "Call 012-345 6789 now".
- Fail: "high ceiling" when the photo check warned the shed has a low roof.

## Scores

**S1. Buyer specs up front (0 to 2).** Industrial buyers screen on location and
road access, land and built-up area, power, ceiling height, tenure and
container access.

- 2: every one of these that is on the card appears in the headline or the
  first two sentences.
- 1: most appear, but one important spec on the card is missing or buried at
  the end.
- 0: generic text; the reader cannot tell the power, size or location.

**S2. Clear and correct (0 to 2).**

- 2: correct English, 60 to 120 words, headline at most 70 characters, units
  right (A versus kVA, acres versus sqft).
- 1: one slip (a long sentence that is hard to follow, a headline over length).
- 0: errors a buyer would notice, or a wrong unit.

**S3. Distinct angle (0 to 2), judged across the drafts of one request.**

- 2: a clear angle of its own (operations and logistics, investment and
  yield, expansion and manufacturing) that the other drafts do not take.
- 1: some difference, but it shares most sentences or its angle with another draft.
- 0: a near copy of another draft.

**Overall:** 0 if a gate fails, otherwise S1 + S2 + S3 (0 to 6).

## Procedure

1. Two people (the site owner and one agent) score the same 30 drafts on
   their own, using only this page.
2. Compare: for each gate and score, agreement of 0.6 or more (Cohen's kappa)
   before the rubric is used. Where they disagree, add the disputed draft
   here as an example and score again.
3. Proposed launch bar, to revisit with real data: at least 95% of drafts pass
   G1 and every draft passes G2, and the median overall score is 4 or more.
4. Then check the rubric against what agents actually do: do drafts that score
   higher get kept more often and edited less?
