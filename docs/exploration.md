# Exploration of the original system (v0-original)

Phase 1 record of how the service behaves **before** any change. Everything here was
observed on tag `v0-original` (commit `998c82f`), running in the image
`mle-api:v0-original` (environment in [`environment/baseline-freeze.txt`](environment/baseline-freeze.txt)).
It describes facts. Problems are noted as findings for later phases, not fixed here.

## 1. Request flow: JSON to prediction

The service has two modules on the request path: `src/main.py` builds the app and
`src/api/endpoints.py` holds the routes. In the image, `src/` is copied to `/app` and uvicorn runs
`main:app` from `/app` with `--reload` (single process, file watcher on).

### Startup (`src/main.py`)

1. `FastAPI()` app created, `CORSMiddleware` added (`allow_origins=["*"]`,
   `allow_credentials=True`, all methods and headers).
2. Router from `api.endpoints` included. **No artifacts are loaded at startup**; there is no
   `lifespan`.

### `POST /predict` (`src/api/endpoints.py:30-57`)

The route is `async def`, so every step below runs **on the event loop thread**, blocking all
other requests while it runs.

| Step | Line | What happens | Disk I/O |
| --- | --- | --- | --- |
| 1 | 31 | FastAPI parses the body and validates it against `HomeFeatures` (Pydantic v2): 7 numeric fields + `zipcode: str`, all required. Unknown fields are silently ignored (default `extra="ignore"`). | — |
| 2 | 33-34 | `open("model/model.pkl")` + `pickle.load`: the whole model is unpickled. | **READ `model/model.pkl`** (every request) |
| 3 | 36-37 | `open("model/model_features.json")` + `json.load`: feature order. | **READ `model/model_features.json`** (every request) |
| 4 | 39 | `pd.DataFrame([home_features.dict()])`: 1-row frame, 8 columns. `.dict()` is deprecated in Pydantic v2 (warning on every call). | — |
| 5 | 42 | `pd.read_csv("data/zipcode_demographics.csv", dtype={"zipcode": str})`: all 70 rows × 27 columns parsed. | **READ `data/zipcode_demographics.csv`** (every request) |
| 6 | 43-45 | Boolean filter on `zipcode`, drop `zipcode`, reset index → 0 or 1 rows of 26 demographic columns. | — |
| 7 | 48 | `pd.concat(axis=1)` of the input row and the demographic row. If the zipcode matched nothing, the demographic columns are all `NaN`. | — |
| 8 | 49 | `print(input_data)`: the DataFrame is formatted and written to stdout. | **WRITE stdout** (every request) |
| 9 | 52 | `input_data[model_features]`: selects and orders the 33 features. `zipcode` is dropped here. | — |
| 10 | 55 | `model.predict(input_data)`: `RobustScaler` → `KNeighborsRegressor` (k=5, brute force or tree search over the training set). | — |
| 11 | 57 | `{"predicted_price": prediction[0]}`: `numpy.float64`, serialised as a JSON number. No model version, no imputation info. | — |

All paths are **relative to the process working directory** (`/app` in the API image;
`Dockerfile.test` adds the `/app/model` and `/app/data` symlinks to make them resolve when run
from `/app` with `src/` underneath). Nothing is cached between requests: each request repeats
steps 2, 3 and 5.

### Other routes and modules

- `GET /health` (`endpoints.py:21-27`): returns `{"status": "healthy"}` without checking that
  artifacts can be loaded.
- `src/utils/loader.py`: `load_model`, `load_features`, `get_demographics`. **Not imported
  anywhere** in `src/` or `tests/`. `get_demographics` reads the CSV without
  `dtype={"zipcode": str}`, so `zipcode` becomes `int64` and a `str` lookup can never match;
  it would always return `{}`. Dead code today.
- `src/model/create_model.py` (identical to root `create_model.py` except a trailing newline):
  the training script. Not run by the service.

## 2. `model.pkl`

Inspected inside `mle-api:v0-original` (scikit-learn 1.7.2), loaded with
`-W error::UserWarning`: no `InconsistentVersionWarning`, so the pickle matches the installed
version.

| Property | Value |
| --- | --- |
| Type | `sklearn.pipeline.Pipeline` (not a bare estimator) |
| Step 1 | `robustscaler`: `RobustScaler()` defaults (`with_centering=True`, `with_scaling=True`, `quantile_range=(25.0, 75.0)`) |
| Step 2 | `kneighborsregressor`: `KNeighborsRegressor()` defaults (`n_neighbors=5`, `weights="uniform"`, `metric="minkowski"`, `p=2`, `algorithm="auto"`) |
| Scaler | **Yes, inside the pipeline.** Callers must pass raw (unscaled) features. |
| `n_features_in_` | 33 |
| `feature_names_in_` | present; **identical, same order** to `model_features.json` |
| Training data | `kc_house_data.csv` (8 columns + price), left-joined with `zipcode_demographics.csv` on `zipcode`, `train_test_split(random_state=42)` (default 75 % train). See `create_model.py`. |

Feature order (33): 7 house features, then 26 demographic features:

```
 1 bedrooms              12 non_farm_qty            23 per_urbn
 2 bathrooms             13 medn_hshld_incm_amt     24 per_sbrbn
 3 sqft_living           14 medn_incm_per_prsn_amt  25 per_farm
 4 sqft_lot              15 hous_val_amt            26 per_non_farm
 5 floors                16 edctn_less_than_9_qty   27 per_less_than_9
 6 sqft_above            17 edctn_9_12_qty          28 per_9_to_12
 7 sqft_basement         18 edctn_high_schl_qty     29 per_hsd
 8 ppltn_qty             19 edctn_some_clg_qty      30 per_some_clg
 9 urbn_ppltn_qty        20 edctn_assoc_dgre_qty    31 per_assoc
10 sbrbn_ppltn_qty       21 edctn_bchlr_dgre_qty    32 per_bchlr
11 farm_ppltn_qty        22 edctn_prfsnl_qty        33 per_prfsnl
```

Because the regressor is a uniform-weight KNN, every prediction is the mean of 5 training
prices (e.g. the reference payload below returns exactly `253980.0`).

## 3. Data facts relevant to later phases

Checked in `mle-api:v0-original` with pandas 2.3.3:

| Fact | Result |
| --- | --- |
| `sqft_living == sqft_above + sqft_basement` in `kc_house_data.csv` | holds for **21 613 / 21 613** rows |
| same identity in `future_unseen_examples.csv` | holds for **100 / 100** rows |
| zipcodes in `kc_house_data.csv` missing from demographics | none (70 zipcodes in demographics) |
| zipcodes in `future_unseen_examples.csv` missing from demographics | none |
| `future_unseen_examples.csv` columns | 18 (the 8 API fields plus `waterfront`, `view`, `condition`, `grade`, `yr_built`, `yr_renovated`, `lat`, `long`, `sqft_living15`, `sqft_lot15`); no `id`, `date` or `price` |
| `zipcode` dtype when read without `dtype=` | `int64` (all Seattle zips start with 98, so no leading zeros are lost in this data, but the type is wrong) |

## 4. Notebook vs CLAUDE.md

`notebooks/imputation_experiment.ipynb` against the "Imputation pipeline" section and hard rules
of `.claude/CLAUDE.md`. **Not resolved here**: per CLAUDE.md, disagreements are flagged and
decided in Phase 3.

| # | Topic | Notebook | CLAUDE.md | Kind |
| --- | --- | --- | --- | --- |
| 1 | Imputer | `KNNImputer(n_neighbors=5, weights="distance")` | "impute with KNN (configuration taken from the notebook)" | Agree (CLAUDE.md defers) |
| 2 | Feature scaling before KNN imputation | **None**: imputer runs on raw values (`sqft_lot` in the 10⁵ range next to `bathrooms` ~2), so distances are dominated by large-scale columns | "Imputer and scaler are fitted once … saved as artifacts", implying a scaler | **Conflict** |
| 3 | Columns the imputer sees | 17 numeric `kc_house_data` columns: the 7 API house fields **plus** `waterfront`, `view`, `condition`, `grade`, `yr_built`, `yr_renovated`, `lat`, `long`, `sqft_living15`, `sqft_lot15` | API only receives the 7 house fields + `zipcode` (+ demographics via lookup) | **Conflict**: 10 of the notebook's columns are not available at request time |
| 4 | Demographics in imputation | Not used; `zipcode` excluded | Step 2 (demographics lookup) runs before step 4 (KNN), suggesting demographics may be imputation inputs | **Unclear** |
| 5 | Data the imputer is fitted on | `fit_transform` on the **whole** dataset (21 613 rows, including the 25 % the model held out), with synthetic NaNs | "fitted once … on training data" | **Unclear**: full dataset or the model's train split? Fitted on data with or without NaNs? |
| 6 | Deterministic fill (`sqft_living = sqft_above + sqft_basement`) | Not present | Step 3, before KNN | **CLAUDE.md only** (identity holds on 100 % of the data, §3) |
| 7 | Post-processing (round `bedrooms`/`floors`, `bathrooms` to 0.25, clip at 0) | Not present; imputed values left continuous | Step 5 | **CLAUDE.md only** |
| 8 | Missingness pattern evaluated | MCAR, 15 % per column, all 17 columns at once | API: any field except `zipcode` may be `null`, pattern unspecified | Gap: request-time pattern (often 1–2 fields) never evaluated |
| 9 | Evaluation metric | Per-feature RMSE/MAE of imputed values only; no downstream price error | Phase 3 must pick a method; CLAUDE.md does not define a metric | Gap |
| 10 | Notebook's own numbers | Aggregate avg RMSE: Mean 4095 < Median 4161 < **KNN 4480**; KNN chosen because it wins on 7 hand-picked features | "impute with KNN" | Note: recommendation rests on a feature subset, not the aggregate |
| 11 | Response | n/a | Response adds `imputed_fields` and `model_version` | API contract change (v0 returns only `predicted_price`) |

## 5. Edge cases against the running container

Container: `docker run -d --rm -p 8000:8000 --name housing-api-v0 mle-api:v0-original`.
Each case was sent with:

```sh
curl -s -w '%{http_code}' -X POST localhost:8000/predict \
  -H 'Content-Type: application/json' -d '<payload>'
```

Reference payload (`BASE`):
`{"bedrooms":3,"bathrooms":2.0,"sqft_living":1500.0,"sqft_lot":5000.0,"floors":1.0,"sqft_above":1200.0,"sqft_basement":300.0,"zipcode":"98042"}`

| Case | Payload change vs `BASE` | Status | Response body |
| --- | --- | --- | --- |
| Complete (reference) | none | **200** | `{"predicted_price":253980.0}` |
| Unknown zipcode | `"zipcode":"00000"` | **500** | `Internal Server Error` (plain text) |
| Missing field | `bathrooms` omitted | **422** | `{"detail":[{"type":"missing","loc":["body","bathrooms"],"msg":"Field required","input":{…full payload…}}]}` |
| Null field | `"bathrooms":null` | **422** | `{"detail":[{"type":"float_type","loc":["body","bathrooms"],"msg":"Input should be a valid number","input":null}]}` |
| Zipcode as int | `"zipcode":98042` | **422** | `{"detail":[{"type":"string_type","loc":["body","zipcode"],"msg":"Input should be a valid string","input":98042}]}` |
| Extra fields | `+ "waterfront":1,"grade":13,"lat":47.5` | **200** | `{"predicted_price":253980.0}` (same as reference: extras ignored) |
| Missing zipcode | `zipcode` omitted | **422** | `{"detail":[{"type":"missing","loc":["body","zipcode"],"msg":"Field required","input":{…}}]}` |
| Null zipcode | `"zipcode":null` | **422** | `{"detail":[{"type":"string_type","loc":["body","zipcode"],"msg":"Input should be a valid string","input":null}]}` |

Server-side observations for the same run (`docker logs housing-api-v0`):

- **Unknown zipcode**: full traceback in the server log ending in
  `ValueError: Input X contains NaN. KNeighborsRegressor does not accept missing values encoded
  as NaN natively…` raised from `model.predict` (`endpoints.py:55`). The demographic columns are
  `NaN` after the empty lookup and the concat (step 7). The client gets no hint of the cause.
- **422 cases**: rejected by Pydantic before the handler runs, so no file is read.
- **200 cases**: the 1×34 DataFrame is printed to stdout (`print`, `endpoints.py:49`), e.g.
  `[1 rows x 34 columns]`, in addition to uvicorn's access log line. No request id, no latency.
- 422 bodies echo the full request payload back in `input`.

## 6. Findings for later phases

Recorded, not fixed (Phase 1 does not touch `src/`).

- **Performance (Phase 4)**: three file reads + one unpickle + one CSV parse per request, all on
  the event loop (`async def`); plus `print` of a DataFrame. See
  [`benchmarks/baseline.md`](benchmarks/baseline.md).
- **Missing data (Phase 3)**: `null` is rejected with 422 for every field; unknown zipcode is an
  unhandled 500 rather than a 422.
- **Production readiness (Phase 5)**: no structured logging, `print` to stdout, `/health` does not
  check artifacts, CORS wide open with credentials, `.dict()` deprecation warning per request,
  dead `utils/loader.py` with a zipcode dtype bug.
- **Environment (Phase 2)**: see the Phase 2 notes in the Phase 1 summary and
  `environment/baseline-freeze.txt` (unpinned deps and base image, Python 3.10 vs `>=3.11` in
  CLAUDE.md, `uvicorn --reload` in the production image).
