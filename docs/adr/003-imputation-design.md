# ADR 003: Impute missing house features with the notebook's KNN, plus an exact sqft fill

- **Status:** Accepted
- **Date:** 2026-11-08
- **Phase:** 3 (branch `feat/phase-3-imputation`)

## Context

- `POST /predict` must accept `null` (or an omitted field) for every field except `zipcode`. The
  model (`model.pkl`, `RobustScaler -> KNeighborsRegressor`) can't take missing values and must
  not be retrained.
- `notebooks/imputation_experiment.ipynb` chose `KNNImputer(n_neighbors=5, weights="distance")`
  on raw values. It measured only the imputation error of each feature, never the effect on
  the predicted price.
- The Phase 3 rule (`.claude/CLAUDE.md`): faithful to the notebook first. Deviations need
  evidence from `scripts/evaluate_imputation.py`, the human's approval and this ADR.
- How the imputer is fitted, stored and loaded is in [ADR 002](002-persisted-imputer-artifact.md).
- Evaluation ([`../imputation_report.md`](../imputation_report.md)): the model's held-out split,
  5,297 rows after removing 107 repeat sales that also occur in train. Each house feature was
  masked on its own, then all seven at once. Complete-data MAPE is 17.77 %.
  - Notebook KNN: one missing feature costs at most +1.4 pp of MAPE (`sqft_lot`, 19.15 %); the
    others cost 0.5 pp or less. With only `zipcode` given, MAPE is 35.05 %, worse than a median
    imputer (30.63 %).
  - The sqft identity `sqft_living = sqft_above + sqft_basement` holds on 100 % of the rows.
    Using it when one sqft field is missing is exact: MAPE back to 17.77 %, prediction shift 0.
  - Scaling (`RobustScaler` before KNN) lowers the imputation error on every feature, but moves
    MAPE by at most 0.3 pp on one feature, and makes the zipcode-only case much worse
    (35.05 → 43.53 %).
  - Rounding imputed values (bedrooms to integers, floors to 0.5, bathrooms to 0.25, clip at 0)
    changes MAPE by 0.1 pp or less (0.5 pp in the zipcode-only case).
- `future_unseen_examples.csv` can't be used for this: all 100 rows are copies of
  `kc_house_data.csv` rows, and 76 are in the imputer's donor set, so a masked value is
  recovered from the row itself (median shift 0.00 % with `sqft_lot` masked, against 6.05 % on
  the 24 clean rows).

## Decision

**Decision taken by the human on the evaluation: adopt the sqft fill only.** The request pipeline
(`services/imputation.py`) is:

1. **Validation.** An omitted field and `null` both mean "missing". NaN and ±Infinity are
   invalid input (422), not missing. `zipcode` is a required 5-digit string; an unknown
   zipcode is a 422.
2. **Nothing missing:** skip imputation. Complete requests take the v0 path unchanged, and the
   characterization test keeps passing.
3. **Exact sqft fill:** if exactly one of `sqft_living`, `sqft_above`, `sqft_basement` is
   missing, compute it from the identity. If the result is negative, the given values
   contradict each other: the field is left to KNN.
4. **KNN** for whatever is still missing: the persisted notebook imputer (raw values, no
   scaling, no rounding), fitted on the 33 model features of the model's train split, used
   only with `transform`. A non-finite result is an error, not a prediction.
5. **Response:** `imputed_fields` lists every field that was missing, however it was filled, in
   model order (empty when nothing was).

## Alternatives

| Option | Pros | Cons | Why not chosen |
| --- | --- | --- | --- |
| **Notebook KNN only** | Most faithful | Ignores an exact relation; a missing sqft field costs up to 0.5 pp MAPE and ~180 ms of KNN | The sqft fill is strictly better and can't make anything worse |
| **Median imputer** | Simple; better for `sqft_lot` (18.49 vs 19.15 %) and zipcode-only (30.63 vs 35.05 %) | Worse on the other six features | The notebook's choice wins on most features. A targeted fallback is a follow-up (below) |
| **Scaling inside the imputer** | Lower imputation error everywhere; −0.3 pp MAPE on `sqft_lot` | Zipcode-only MAPE +8.5 pp; a scaler artifact to maintain | Small gains don't justify one large regression |
| **Rounding / clipping imputed values** | Plausible values (no 2.6 bedrooms) | No measurable accuracy effect; the response doesn't expose imputed values | Adds code without a benefit. If revisited, `floors` rounds to 0.5, not to an integer (CLAUDE.md said "round floors"; corrected) |
| **All three candidates** | — | Dominated by scaling's regression (43.80 %) | — |
| **Reject requests with missing fields** | No guessed values | Fails the requirement | Out of scope |

## Consequences

- One missing house feature costs little accuracy on average (≤ +1.4 pp MAPE), but single
  predictions can move: the p90 shift is 7–23 % depending on the feature. Clients learn which
  fields were guessed from `imputed_fields`.
- **The zipcode-only case is weak.** With no house features, unscaled demographics are the only
  distances. They're identical within a zipcode, so all same-zipcode donors tie at distance 0,
  and the imputer takes five of them in index order. That's effectively five arbitrary houses
  from the zipcode. A median (global or per zipcode) fallback for this case, and per feature for
  `sqft_lot`, looks better but **was not evaluated as a pipeline** and isn't pre-approved: it
  needs its own evaluation and ADR.
- **Untested path:** the negative-result fallback (contradictory sqft values go to KNN) is
  covered by unit tests, but the evaluation never measured it. Real data never has a negative
  result, because the identity holds on every row.
- **Latency:** a request that reaches KNN costs about 180 ms more CPU than a complete one;
  the sqft fill avoids that for one missing sqft field
  ([`../benchmarks/phase-3-imputation.md`](../benchmarks/phase-3-imputation.md)). Phase 4 profiles it.
  *Update (Phase 6):* that was the Phase 3 cost. After Phase 4 the difference is about 18 ms:
  22.46 ms of server CPU per `one-null-knn` request against 4.89 ms per complete one, `main`
  with one worker ([`../benchmarks/final.md`](../benchmarks/final.md)).
- **Limits of the evidence:** missingness was simulated as MCAR, one feature or all seven; only
  one split; no confidence intervals, so differences under ~0.1 pp are noise. Pairs of missing
  fields were only tested for "no 500" (property-based tests), not for accuracy.
- Changing the method later means re-running `make evaluate-imputation`, refitting with
  `make fit-imputer` if the artifact changes, and a new ADR that supersedes this one.
