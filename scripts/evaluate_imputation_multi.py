"""Imputation accuracy by number of missing house fields: every combination, k = 1 to 7.

    uv run python scripts/evaluate_imputation_multi.py   # or: make evaluate-imputation-multi

Same data, methods and metrics as ``scripts/evaluate_imputation.py`` (imported from it): fit on
the model's train split, evaluate on its held-out split minus copies of train rows (5,297 rows).
Each of the 127 non-empty subsets of the seven house fields is masked on every row (MCAR,
deterministic: no random masking), and two methods are compared:

- ``served``: what the API does, the exact sqft fill (``services.imputation.fill_sqft_identity``)
  then the committed KNN imputer;
- ``median``: ``SimpleImputer(strategy="median")`` fitted on train, the baseline.

Metrics per combination and method: MAPE against the true sale price, and the median and p90 of
the prediction shift |price(imputed) - price(complete)| / price(complete).

Before writing anything, the k = 1 and k = 7 rows are compared with the existing
``docs/imputation/evaluation_results.json`` (methods ``knn+sqft_fill`` and ``median``); the
script fails if any value differs.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import itertools
import json
import logging
import pickle
import statistics
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import joblib
import numpy as np
from sklearn.impute import SimpleImputer

SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_single_evaluation() -> ModuleType:
    """``scripts/evaluate_imputation.py``, loaded by path: scripts/ is no package."""
    spec = importlib.util.spec_from_file_location(
        "evaluate_imputation", SCRIPTS_DIR / "evaluate_imputation.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


single = _load_single_evaluation()  # also puts src/ on sys.path

from core.config import Settings  # noqa: E402
from core.imputer_artifact import compare_fitted_state, fit_imputer, load_model_split  # noqa: E402
from services.imputation import HOUSE_FEATURES  # noqa: E402

logger = logging.getLogger("evaluate_imputation_multi")

OUT_DIR = single.REPO_ROOT / "docs" / "imputation"
DEFAULT_OUT_JSON = OUT_DIR / "missing_count_results.json"
DEFAULT_MARKDOWN = OUT_DIR / "missing_count_tables.md"
SQFT_FIELDS = ("sqft_living", "sqft_above", "sqft_basement")
# The existing evaluation's names for the two methods compared here.
REFERENCE_METHOD = {"served": "knn+sqft_fill", "median": "median"}
METRICS = ("price_mape_pct", "shift_median_pct", "shift_p90_pct")


def combinations() -> list[tuple[str, ...]]:
    """Every non-empty subset of the house fields, by size, in the model's feature order."""
    return [
        combo
        for k in range(1, len(HOUSE_FEATURES) + 1)
        for combo in itertools.combinations(HOUSE_FEATURES, k)
    ]


def combination_name(combo: tuple[str, ...]) -> str:
    return "+".join(combo)


def loses_sqft_fill(combo: tuple[str, ...]) -> bool:
    """True when two or three area fields are missing, so the identity cannot recover them."""
    return sum(field in combo for field in SQFT_FIELDS) >= 2


def reference_name(combo: tuple[str, ...]) -> str:
    return single.ALL_FEATURES if len(combo) == len(HOUSE_FEATURES) else combo[0]


def check_against_reference(
    rows: dict[str, dict[str, Any]], reference: dict[str, Any]
) -> list[str]:
    """Differences between the k = 1 / k = 7 rows and the existing evaluation's results."""
    problems = []
    for name, row in rows.items():
        if row["k"] not in (1, len(HOUSE_FEATURES)):
            continue
        expected = reference["scenarios"][reference_name(tuple(row["combination"]))]
        for method, ref_method in REFERENCE_METHOD.items():
            for metric in METRICS:
                got, want = row[method][metric], expected[ref_method][metric]
                if got != want:
                    problems.append(f"{name} {method} {metric}: {got!r} != {want!r}")
    return problems


def summarize_by_k(rows: dict[str, dict[str, Any]], complete_mape: float) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = [
        {
            "k": 0,
            "n_combinations": 1,
            "served_mape": {"median": complete_mape, "min": complete_mape, "max": complete_mape},
            "median_mape_median": complete_mape,
            "best": "(none missing)",
            "worst": "(none missing)",
            "served_p90_shift": {"median": 0.0, "min": 0.0, "max": 0.0},
            "served_beats_median": 0,
        }
    ]
    for k in range(1, len(HOUSE_FEATURES) + 1):
        group = [(name, row) for name, row in rows.items() if row["k"] == k]
        mape = [row["served"]["price_mape_pct"] for _, row in group]
        p90 = [row["served"]["shift_p90_pct"] for _, row in group]
        best = min(group, key=lambda item: item[1]["served"]["price_mape_pct"])
        worst = max(group, key=lambda item: item[1]["served"]["price_mape_pct"])
        summary.append(
            {
                "k": k,
                "n_combinations": len(group),
                "served_mape": {
                    "median": statistics.median(mape),
                    "min": min(mape),
                    "max": max(mape),
                },
                "median_mape_median": statistics.median(
                    row["median"]["price_mape_pct"] for _, row in group
                ),
                "best": best[0],
                "worst": worst[0],
                "served_p90_shift": {
                    "median": statistics.median(p90),
                    "min": min(p90),
                    "max": max(p90),
                },
                "served_beats_median": sum(
                    row["served"]["price_mape_pct"] < row["median"]["price_mape_pct"]
                    for _, row in group
                ),
            }
        )
    return summary


def evaluate(settings: Settings) -> dict[str, Any]:
    split = load_model_split(settings)
    with settings.model_path.open("rb") as handle:
        model = pickle.load(handle)
    committed_knn = joblib.load(settings.imputer_path)
    problems = compare_fitted_state(committed_knn, fit_imputer(split.x_train))
    if problems:
        raise SystemExit(f"committed imputer differs from a refit: {problems}")

    x_eval, y_eval, n_copies = single.exclude_train_copies(
        split.x_train, split.x_test, split.y_test
    )
    assert y_eval is not None
    complete = model.predict(x_eval)
    evaluation = single.Evaluation(model, x_eval, y_eval, split.x_train.std(), complete)
    methods = {
        "served": single.with_sqft_fill(single.sklearn_imputer(committed_knn)),
        "median": single.sklearn_imputer(SimpleImputer(strategy="median").fit(split.x_train)),
    }

    rows: dict[str, dict[str, Any]] = {}
    for combo in combinations():
        name = combination_name(combo)
        logger.info("k=%d: %s", len(combo), name)
        row: dict[str, Any] = {
            "k": len(combo),
            "combination": list(combo),
            "loses_sqft_fill": loses_sqft_fill(combo),
        }
        for method, impute in methods.items():
            result = evaluation.scenario(impute, combo)
            row[method] = {metric: result[metric] for metric in METRICS}
        rows[name] = row

    complete_mape = float(np.mean(np.abs(complete - y_eval) / y_eval) * 100)
    return {
        "data": {
            "fit": "model train split",
            "n_train": len(split.x_train),
            "n_eval": len(x_eval),
            "n_excluded_copies": n_copies,
        },
        "complete_mape_pct": complete_mape,
        "methods": {"served": "sqft fill, then committed KNN", "median": "SimpleImputer(median)"},
        "by_k": summarize_by_k(rows, complete_mape),
        "combinations": rows,
    }


# --- report -----------------------------------------------------------------------------------


def markdown_tables(results: dict[str, Any]) -> str:
    rows = results["combinations"]
    data = results["data"]
    out = [
        "<!-- Generated by scripts/evaluate_imputation_multi.py; do not edit by hand. -->",
        "",
        f"Evaluation rows: {data['n_eval']} held-out rows ({data['n_excluded_copies']} excluded "
        f"as copies of train rows). Complete-data MAPE: {results['complete_mape_pct']:.2f} %. "
        "`served` = exact sqft fill, then the committed KNN imputer; `median` = "
        "SimpleImputer(median). Every combination is masked on every row (no random masking). "
        "p90 shift = 90th percentile of |price(imputed) − price(complete)| / price(complete).",
        "",
        "### Summary by number of missing fields (served method)",
        "",
        "MAPE and p90 shift are summarized across the k-field combinations: median, then "
        "[min – max]. k = 0 is the complete-data prediction.",
        "",
        "| k | combinations | served MAPE: median [min – max] | median-imputer MAPE: median "
        "| served p90 shift: median [min – max] | served beats median | best | worst |",
        "| ---: | ---: | --- | ---: | --- | ---: | --- | --- |",
    ]
    for s in results["by_k"]:
        mape, p90 = s["served_mape"], s["served_p90_shift"]
        out.append(
            f"| {s['k']} | {s['n_combinations']} "
            f"| {mape['median']:.2f} [{mape['min']:.2f} – {mape['max']:.2f}] "
            f"| {s['median_mape_median']:.2f} "
            f"| {p90['median']:.2f} [{p90['min']:.2f} – {p90['max']:.2f}] "
            f"| {s['served_beats_median']} / {s['n_combinations']} "
            f"| {s['best']} | {s['worst']} |"
        )
    out.append("")

    def combo_table(title: str, names: list[str], note: str = "") -> None:
        out.extend([f"### {title}", ""])
        if note:
            out.extend([note, ""])
        out.extend(
            [
                "| Missing | loses sqft fill | served MAPE | median MAPE | served p90 shift "
                "| median p90 shift |",
                "| --- | :---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for name in names:
            row = rows[name]
            served, median = row["served"], row["median"]
            mape = [served["price_mape_pct"], median["price_mape_pct"]]
            p90 = [served["shift_p90_pct"], median["shift_p90_pct"]]

            def bold(values: list[float], i: int) -> str:
                cell = f"{values[i]:.2f}"
                return f"**{cell}**" if values[i] == min(values) else cell

            out.append(
                f"| {name} | {'yes' if row['loses_sqft_fill'] else ''} "
                f"| {bold(mape, 0)} | {bold(mape, 1)} | {bold(p90, 0)} | {bold(p90, 1)} |"
            )
        out.append("")

    pairs = [name for name, row in rows.items() if row["k"] == 2]
    combo_table(
        "k = 2: all 21 pairs",
        pairs,
        "Lower of served / median in bold. *loses sqft fill*: two of `sqft_living`, "
        "`sqft_above`, `sqft_basement` are missing, so the identity cannot recover them and "
        "both go to KNN.",
    )
    for k in range(3, len(HOUSE_FEATURES) + 1):
        names = [name for name, row in rows.items() if row["k"] == k]
        combo_table(f"k = {k}: all {len(names)} combinations", names)
    singles = [name for name, row in rows.items() if row["k"] == 1]
    combo_table(
        "k = 1 (same values as evaluation_tables.md: `knn+sqft_fill` and `median`)", singles
    )
    return "\n".join(out)


def write_csv(results: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["k", "combination", "served_mape", "median_mape", "p90_shift"])
        for name, row in results["combinations"].items():
            writer.writerow(
                [
                    row["k"],
                    name,
                    round(row["served"]["price_mape_pct"], 2),
                    round(row["median"]["price_mape_pct"], 2),
                    round(row["served"]["shift_p90_pct"], 2),
                ]
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--reference", type=Path, default=single.DEFAULT_JSON)
    parser.add_argument("--csv", type=Path, default=None, help="also write a flat CSV here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    start = time.perf_counter()
    results = evaluate(Settings())
    problems = check_against_reference(
        results["combinations"], json.loads(args.reference.read_text())
    )
    if problems:
        for problem in problems:
            logger.error("mismatch with %s: %s", args.reference, problem)
        return 1
    logger.info("k = 1 and k = 7 rows match %s exactly", args.reference)

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(results, indent=2) + "\n")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown_tables(results))
    if args.csv is not None:
        write_csv(results, args.csv)
    logger.info("wrote %s and %s in %.0f s", args.json, args.markdown, time.perf_counter() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
