# Imputation report (Phase 3)

How well the imputer recovers missing house features, and what missing data costs in price
accuracy. Numbers come from `scripts/evaluate_imputation.py` (`make evaluate-imputation`). The
generated tables are in [`imputation/evaluation_tables.md`](imputation/evaluation_tables.md),
and the raw results are in [`imputation/evaluation_results.json`](imputation/evaluation_results.json).
Two runs produce byte-identical output. The decision taken on these numbers is recorded in
[ADR 003](adr/003-imputation-design.md).

## Setup

| Item | Choice | Why |
| --- | --- | --- |
| Fit data | The model's train split (16,209 rows), rebuilt as `create_model.py` does | Same rows as the model and the committed imputer. |
| Evaluation data | The model's held-out split: 5,404 rows, **minus 107** whose 33 features also occur in train (repeat sales), leaving **5,297** | No row is imputed from a copy of itself. These rows have true prices. |
| Missingness | Each house feature masked on its own (MCAR, all rows), plus all seven at once (`zipcode` only) | Matches the request-time pattern of one or a few nulls, plus the worst case. |
| Metrics | (1) imputation error of the masked feature: RMSE, MAE, NRMSE = RMSE / train std; (2) prediction shift vs the complete-data prediction: median and p90 of \|Δ%\|; (3) MAPE against the true sale price | (1) is the notebook's metric. (2) and (3) measure the effect on the price, which the notebook never measured. |

**Why not `future_unseen_examples.csv`?** All 100 rows are copies of `kc_house_data.csv` rows,
and 76 have an exact copy in the train split, which is the imputer's donor set. A masked value on
those rows is recovered from the row itself. The appendix in the tables shows the effect: with
`sqft_lot` masked, the median shift is 0.00 % on the leaked rows and 6.05 % on the 24 clean ones.

### Methods

| Method | What it is |
| --- | --- |
| `median` | `SimpleImputer(strategy="median")` fitted on train. Simple baseline. |
| `knn` | **Notebook-faithful, what the API serves today**: the committed `KNNImputer(n_neighbors=5, weights="distance")`, raw values, over the 33 model features. |
| `knn+scaled` | Candidate: `RobustScaler` fitted on train (the model's own scaler type), KNN in scaled space, inverse-transformed. |
| `knn+sqft_fill` | Candidate: if exactly one of `sqft_living`, `sqft_above`, `sqft_basement` is missing, compute it from `sqft_living = sqft_above + sqft_basement` (the identity holds on 100 % of rows), then KNN for the rest. |
| `knn+rounding` | Candidate: on imputed cells only, round `bedrooms` to an integer, `floors` to 0.5, `bathrooms` to 0.25, and clip at 0. |
| `all_candidates` | The three candidates together. |

**Discrepancy found:** CLAUDE.md's rounding candidate said "round `floors`". But `floors` takes
the values 1, 1.5, 2, 2.5, 3 and 3.5, so rounding to an integer would destroy real values. The
evaluation rounds to the nearest 0.5. CLAUDE.md must be corrected if rounding is adopted.

## Results

Complete-data MAPE on the evaluation rows: **17.77 %**. Headline numbers for the served method
(`knn`) and the baseline:

| Missing | median: MAPE | knn: MAPE | knn: p90 shift | best single candidate (MAPE / p90 shift) |
| --- | ---: | ---: | ---: | --- |
| bedrooms | 18.58 | 17.87 | 15.15 % | scaled: 17.83 / 14.62 % |
| bathrooms | 18.81 | 17.91 | 13.50 % | scaled: 17.89 / 12.66 % |
| sqft_living | 19.81 | 18.05 | 8.20 % | **sqft_fill: 17.77 / 0.00 %** |
| sqft_lot | **18.49** | 19.15 | 22.84 % | scaled: 18.83 / 22.09 % |
| floors | 17.87 | 17.59 | 6.91 % | scaled: 17.58 / 6.19 % |
| sqft_above | 20.08 | 18.27 | 9.02 % | **sqft_fill: 17.77 / 0.00 %** |
| sqft_basement | 17.90 | 17.73 | 9.33 % | **sqft_fill: 17.77 / 0.00 %** |
| all 7 (zipcode only) | **30.63** | 35.05 | 61.62 % | rounding: 34.52 / 59.93 %; scaled: 43.53 / 83.70 % |

### What the numbers say

1. **One missing feature costs little in price accuracy.** With `knn`, MAPE goes from 17.77 % to
   at most 19.15 % (`sqft_lot`, +1.4 pp); every other feature costs 0.5 pp or less. Individual
   predictions can still move: the p90 shift is 7–23 %.
2. **The median shift is usually 0 %, and that's expected.** The model is a uniform 5-NN, so the
   price changes only when the imputed value changes the model's neighbour set. Read the p90.
3. **KNN beats the median baseline on every feature except `sqft_lot`.** This is the same as
   the notebook: lot size is weakly correlated with the other features and heavy-tailed. KNN's
   MAPE there is 19.15 % against the median's 18.49 %.
4. **`zipcode` only is the weak spot: KNN is worse than the median** (MAPE 35.05 % vs 30.63 %,
   p90 shift 61.6 % vs 53.5 %).
   - With all house features missing, only the demographic columns are left to compute
     distances, and they are identical for every house in the zipcode.
   - So all same-zipcode donors are at distance 0, and the imputer takes five of them in
     arbitrary index order, with equal weights.
   - The result is effectively five random houses from the zipcode, whereas the median is a
     central value.
   - Missing everything costs 13–17 pp of MAPE depending on the method (median +12.9 pp, `knn`
     +17.3 pp): the house features carry most of the signal.
5. **Unscaled, the demographic columns decide the neighbours.** Their raw values (in the 10³–10⁵
   range) make same-zipcode houses always nearest, so in practice `knn` is "KNN within the
   zipcode". Scaling changes that: neighbours can come from other zipcodes.

### Candidates

| Candidate | Effect | Verdict |
| --- | --- | --- |
| **sqft fill** | Exact for the three sqft features when one is missing: error 0, MAPE back to 17.77 %, p90 shift 0 %. No effect elsewhere. It's deterministic and needs no fitting. | **Helps, no downside.** |
| **scaling** | Lower imputation error on every feature (NRMSE −5 % to −25 %). p90 shift better on 6 of 7 features (−0.5 to −1.4 pp; `sqft_basement` +0.1 pp). MAPE within ±0.1 pp, except `sqft_lot` (19.15 → 18.83 %) and `sqft_living` (+0.1). But zipcode-only gets much worse: MAPE 35.05 → 43.53 %, p90 shift 61.6 → 83.7 %. | **Mixed: small gains, one large regression.** |
| **rounding** | MAE better for the discrete features (bedrooms 0.561 → 0.513, floors 0.197 → 0.178), NRMSE slightly worse, MAPE within ±0.1 pp (zipcode-only −0.5 pp). Imputed values become plausible (no 2.6 bedrooms), but the response doesn't expose them. | **Neutral on accuracy.** |
| all three | Dominated by scaling's zipcode-only regression (MAPE 43.80 %). | Not a candidate as a bundle. |

## Recommendation

**Decision (human, Phase 3): adopt the sqft fill only.** Implemented in
`services/imputation.py` (`fill_sqft_identity`, which the evaluation script also uses), and recorded in
[ADR 003](adr/003-imputation-design.md). Items 2–4 are not implemented.


1. **Adopt the sqft fill.** It's the only candidate that strictly improves on the faithful
   imputer, removes the cost of a missing sqft feature entirely, and can't make anything worse.
2. **Do not adopt scaling.** The single-feature gains are small (at most 0.3 pp MAPE, on `sqft_lot`). They don't
   justify a 8.5 pp MAPE regression in the zipcode-only case.
3. **Do not adopt rounding.** It has no measurable effect on price accuracy and only adds code.
   If plausibility of imputed values ever matters (for example, if Phase 5 exposes them), revisit
   it with `floors` rounded to 0.5.
4. **Evaluate, but don't adopt yet: a fallback when every house feature is missing.** This
   isn't one of the pre-agreed candidates, so it would need its own approval and ADR. The global
   median already beats KNN there (30.6 % vs 35.1 % MAPE), and a per-zipcode median may do better
   still. The same idea applies per feature: `sqft_lot` is better served by the median.

## More than one missing field

Every combination of missing house fields (k = 1 to 7, 127 combinations) on the same 5,297 rows,
served method (sqft fill, then KNN) against the median imputer, from
`scripts/evaluate_imputation_multi.py` (`make evaluate-imputation-multi`, about 19 minutes). The
script checks that its k = 1 and k = 7 rows match the tables above exactly. Tables:
[`imputation/missing_count_tables.md`](imputation/missing_count_tables.md); raw results:
[`imputation/missing_count_results.json`](imputation/missing_count_results.json).

| k missing | combinations | served MAPE: median [min – max] | served p90 shift: median | served beats median |
| ---: | ---: | --- | ---: | ---: |
| 0 | 1 | 17.77 | 0.00 % | – |
| 1 | 7 | 17.77 [17.59 – 19.15] | 6.91 % | 6 / 7 |
| 2 | 21 | 17.91 [17.59 – 23.83] | 16.60 % | 19 / 21 |
| 3 | 35 | 19.17 [17.81 – 26.40] | 25.70 % | 32 / 35 |
| 4 | 35 | 19.71 [18.22 – 28.61] | 27.91 % | 32 / 35 |
| 5 | 21 | 23.01 [19.02 – 29.44] | 38.36 % | 18 / 21 |
| 6 | 7 | 28.84 [20.05 – 30.75] | 48.27 % | 5 / 7 |
| 7 | 1 | 35.05 | 61.62 % | 0 / 1 |

- **Accuracy depends on which fields are missing, not on how many.** As long as one area field
  can be recovered, the cost stays small: with only `sqft_living` known (k = 6), MAPE is 20.05 %.
- **Losing the sqft fill is what hurts.** Three pairs lose it: `sqft_living+sqft_above` (MAPE
  23.83 %, the worst pair), `sqft_living+sqft_basement` (19.44 %) and `sqft_above+sqft_basement`
  (18.37 %). The other 18 pairs stay within 1.6 pp of complete data.
- **The median imputer wins in two situations only:** `sqft_lot` missing (as at k = 1), or two or
  more area fields missing together with other fields (k ≥ 3). That is the same weak spot as the
  zipcode-only case: KNN within the zipcode, without the house's size, does worse than a central
  value.

## Limitations


- **Missingness is simulated as MCAR, one feature or all seven.** Real requests may be missing
  data for reasons that correlate with price. Pairs of missing features beyond the sqft identity
  were not evaluated separately; the property-based tests only show that they return 200.
- **One split, the model's own.** No confidence intervals. On 5,297 rows, differences under about
  0.1 pp MAPE are within noise; for example, `floors` missing gives a *lower* MAPE than complete
  data.
- **Only accuracy is measured here.** For latency, see `docs/benchmarks/phase-3-imputation.md`.
