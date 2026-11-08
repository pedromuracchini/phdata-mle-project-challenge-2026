"""Evaluate imputation methods: how well they recover values, and what missing data costs.

    uv run python scripts/evaluate_imputation.py      # or: make evaluate-imputation

Every imputer is fitted on the model's own train split (the committed artifact's fit data) and
evaluated on the model's held-out split, minus the held-out rows whose 33 features also occur
in the train split (repeat sales of the same house), so no row is imputed from a copy of itself.

Scenarios: each house feature missing on its own, and all seven missing (only ``zipcode``).
Metrics, per scenario and method:

1. imputation error of the masked feature: RMSE, MAE and NRMSE (RMSE / train std);
2. prediction shift: |price(imputed) - price(complete)| / price(complete), median and p90;
3. price error against the true sale price: MAPE, compared with the complete-data MAPE.

Appendix: prediction shift on ``future_unseen_examples.csv``, split into rows that are copies
of train rows (leaked) and the rest, to show why that file is not used for the main numbers.

Methods: ``median`` (SimpleImputer baseline), ``knn`` (the committed artifact: the notebook's
KNNImputer(k=5, distance) on raw values) and candidate deviations, each on top of ``knn``:
``knn+scaled`` (RobustScaler fitted on train, KNN in scaled space), ``knn+sqft_fill``
(services.imputation.fill_sqft_identity, the served implementation, then KNN), ``knn+rounding``
(bedrooms to integer, floors to 0.5, bathrooms to 0.25, clip at 0, on imputed cells), and
``all_candidates`` (the three together). Deterministic: no randomness besides the fixed split.

Writes JSON results and Markdown tables (paths below); the analysis lives in
docs/imputation_report.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from numpy.typing import NDArray

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sklearn.impute import KNNImputer, SimpleImputer  # noqa: E402
from sklearn.preprocessing import RobustScaler  # noqa: E402

from core.config import SRC_DIR, Settings  # noqa: E402
from core.imputer_artifact import (  # noqa: E402
    N_NEIGHBORS,
    WEIGHTS,
    compare_fitted_state,
    fit_imputer,
    load_model_split,
)
from services.imputation import HOUSE_FEATURES, fill_sqft_identity  # noqa: E402

logger = logging.getLogger("evaluate_imputation")

DEFAULT_JSON = REPO_ROOT / "docs" / "imputation" / "evaluation_results.json"
DEFAULT_MARKDOWN = REPO_ROOT / "docs" / "imputation" / "evaluation_tables.md"
FUTURE_UNSEEN = SRC_DIR / "data" / "future_unseen_examples.csv"

ROUNDING_STEP = {"bedrooms": 1.0, "floors": 0.5, "bathrooms": 0.25}
ALL_FEATURES = "all 7 (zipcode only)"
SCENARIOS: list[tuple[str, tuple[str, ...]]] = [(f, (f,)) for f in HOUSE_FEATURES] + [
    (ALL_FEATURES, HOUSE_FEATURES)
]

Imputer = Callable[[pd.DataFrame], pd.DataFrame]
FloatArray = NDArray[np.float64]


# --- methods ----------------------------------------------------------------------------------


def sklearn_imputer(fitted: Any) -> Imputer:
    def impute(frame: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(fitted.transform(frame), columns=frame.columns, index=frame.index)

    return impute


def scaled_knn(x_train: pd.DataFrame) -> Imputer:
    scaler = RobustScaler().fit(x_train)
    knn = KNNImputer(n_neighbors=N_NEIGHBORS, weights=WEIGHTS).fit(scaler.transform(x_train))

    def impute(frame: pd.DataFrame) -> pd.DataFrame:
        filled = scaler.inverse_transform(knn.transform(scaler.transform(frame)))
        result = pd.DataFrame(filled, columns=frame.columns, index=frame.index)
        # Inverse scaling is not bit-exact: keep every observed value exactly as given.
        return result.where(frame.isna(), frame)

    return impute


def with_sqft_fill(impute: Imputer) -> Imputer:
    def wrapped(frame: pd.DataFrame) -> pd.DataFrame:
        filled = fill_sqft_identity(frame)
        return impute(filled) if filled.isna().to_numpy().any() else filled

    return wrapped


def with_rounding(impute: Imputer) -> Imputer:
    def wrapped(frame: pd.DataFrame) -> pd.DataFrame:
        result = impute(frame)
        for column in HOUSE_FEATURES:
            was_missing = frame[column].isna()
            values = result.loc[was_missing, column].clip(lower=0)
            step = ROUNDING_STEP.get(column)
            if step is not None:
                values = (values / step).round() * step
            result.loc[was_missing, column] = values
        return result

    return wrapped


def build_methods(x_train: pd.DataFrame, committed_knn: Any) -> dict[str, Imputer]:
    knn = sklearn_imputer(committed_knn)
    scaled = scaled_knn(x_train)
    return {
        "median": sklearn_imputer(SimpleImputer(strategy="median").fit(x_train)),
        "knn": knn,
        "knn+scaled": scaled,
        "knn+sqft_fill": with_sqft_fill(knn),
        "knn+rounding": with_rounding(knn),
        "all_candidates": with_rounding(with_sqft_fill(scaled)),
    }


# --- metrics ----------------------------------------------------------------------------------


def percentile_abs_pct(shift: FloatArray, pct: float) -> float:
    return float(np.percentile(shift, pct) * 100)


@dataclass
class Evaluation:
    model: Any
    x_eval: pd.DataFrame
    y_eval: pd.Series
    train_std: pd.Series
    complete_prediction: FloatArray

    def scenario(self, impute: Imputer, masked: tuple[str, ...]) -> dict[str, Any]:
        frame = self.x_eval.astype("float64")
        frame.loc[:, list(masked)] = np.nan
        imputed = impute(frame)
        prediction = self.model.predict(imputed)
        shift = np.abs(prediction - self.complete_prediction) / self.complete_prediction
        result: dict[str, Any] = {
            "shift_median_pct": percentile_abs_pct(shift, 50),
            "shift_p90_pct": percentile_abs_pct(shift, 90),
            "price_mape_pct": float(np.mean(np.abs(prediction - self.y_eval) / self.y_eval) * 100),
        }
        if len(masked) == 1:
            column = masked[0]
            error = imputed[column].to_numpy() - self.x_eval[column].to_numpy()
            rmse = float(np.sqrt(np.mean(error**2)))
            result |= {
                "rmse": rmse,
                "mae": float(np.mean(np.abs(error))),
                "nrmse": rmse / float(self.train_std[column]),
            }
        return result


def exclude_train_copies(
    x_train: pd.DataFrame, x: pd.DataFrame, y: pd.Series | None = None
) -> tuple[pd.DataFrame, pd.Series | None, int]:
    train_rows = set(map(tuple, x_train.to_numpy(dtype=float)))
    copy = np.array([tuple(row) in train_rows for row in x.to_numpy(dtype=float)])
    return x[~copy], (y[~copy] if y is not None else None), int(copy.sum())


def future_unseen_appendix(
    model: Any, methods: dict[str, Imputer], x_train: pd.DataFrame, settings: Settings
) -> dict[str, Any]:
    houses = pd.read_csv(FUTURE_UNSEEN, dtype={"zipcode": str})
    demographics = pd.read_csv(settings.demographics_path, dtype={"zipcode": str})
    unseen = houses.merge(demographics, how="left", on="zipcode")[list(x_train.columns)]
    clean, _, n_leaked = exclude_train_copies(x_train, unseen)
    leaked = unseen.drop(index=clean.index)
    appendix: dict[str, Any] = {"n_leaked": n_leaked, "n_clean": len(clean), "rows": {}}
    for subset_name, subset in (("leaked", leaked), ("clean", clean)):
        complete = model.predict(subset)
        dummy_y = pd.Series(complete, index=subset.index)
        evaluation = Evaluation(model, subset, dummy_y, x_train.std(), complete)
        appendix["rows"][subset_name] = {
            name: evaluation.scenario(methods["knn"], masked)["shift_median_pct"]
            for name, masked in SCENARIOS
        }
    return appendix


# --- report -----------------------------------------------------------------------------------


def markdown_tables(results: dict[str, Any]) -> str:
    methods = list(results["methods"])
    scenarios = [name for name, _ in SCENARIOS]
    rows = results["scenarios"]
    out = [
        "<!-- Generated by scripts/evaluate_imputation.py; do not edit by hand. -->",
        "",
        f"Evaluation rows: {results['data']['n_eval']} held-out rows "
        f"({results['data']['n_excluded_copies']} excluded as copies of train rows). "
        f"Complete-data MAPE: {results['complete_mape_pct']:.2f} %.",
        "",
    ]

    def table(title: str, metric: str, fmt: str, scenario_names: list[str]) -> None:
        out.extend([f"### {title}", "", "| Missing | " + " | ".join(methods) + " |"])
        out.append("| --- |" + " ---: |" * len(methods))
        for scenario in scenario_names:
            cells = []
            values = [rows[scenario][m][metric] for m in methods]
            best = min(values)
            for value in values:
                cell = format(value, fmt)
                cells.append(f"**{cell}**" if value == best else cell)
            out.append(f"| {scenario} | " + " | ".join(cells) + " |")
        out.append("")

    single = [name for name in scenarios if name != ALL_FEATURES]
    table("Imputation error: NRMSE (RMSE / train std), lower is better", "nrmse", ".3f", single)
    table("Imputation error: MAE (feature units)", "mae", ".3f", single)
    table(
        "Prediction shift: median |Δ%| vs complete prediction", "shift_median_pct", ".2f", scenarios
    )
    table("Prediction shift: p90 |Δ%| vs complete prediction", "shift_p90_pct", ".2f", scenarios)
    table(
        f"Price error vs true price: MAPE % (complete data: {results['complete_mape_pct']:.2f})",
        "price_mape_pct",
        ".2f",
        scenarios,
    )

    appendix = results["future_unseen_appendix"]
    out.extend(
        [
            "### Appendix: future_unseen_examples, knn, median |Δ%|",
            "",
            f"{appendix['n_leaked']} rows are copies of train rows (leaked), "
            f"{appendix['n_clean']} are not.",
            "",
            "| Missing | leaked rows | clean rows |",
            "| --- | ---: | ---: |",
        ]
    )
    for scenario in scenarios:
        leaked = appendix["rows"]["leaked"][scenario]
        clean = appendix["rows"]["clean"][scenario]
        out.append(f"| {scenario} | {leaked:.2f} | {clean:.2f} |")
    out.append("")
    return "\n".join(out)


def evaluate(settings: Settings) -> dict[str, Any]:
    split = load_model_split(settings)
    with settings.model_path.open("rb") as handle:
        model = pickle.load(handle)
    committed_knn = joblib.load(settings.imputer_path)
    problems = compare_fitted_state(committed_knn, fit_imputer(split.x_train))
    if problems:
        raise SystemExit(f"committed imputer differs from a refit: {problems}")

    x_eval, y_eval, n_copies = exclude_train_copies(split.x_train, split.x_test, split.y_test)
    assert y_eval is not None
    complete = model.predict(x_eval)
    evaluation = Evaluation(model, x_eval, y_eval, split.x_train.std(), complete)
    methods = build_methods(split.x_train, committed_knn)

    scenarios: dict[str, dict[str, Any]] = {}
    for name, masked in SCENARIOS:
        logger.info("scenario: %s missing", name)
        scenarios[name] = {
            method: evaluation.scenario(impute, masked) for method, impute in methods.items()
        }

    return {
        "data": {
            "fit": "model train split",
            "n_train": len(split.x_train),
            "n_eval": len(x_eval),
            "n_excluded_copies": n_copies,
        },
        "complete_mape_pct": float(np.mean(np.abs(complete - y_eval) / y_eval) * 100),
        "methods": list(methods),
        "scenarios": scenarios,
        "future_unseen_appendix": future_unseen_appendix(model, methods, split.x_train, settings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    results = evaluate(Settings())
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(results, indent=2) + "\n")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown_tables(results))
    logger.info("wrote %s and %s", args.json, args.markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
