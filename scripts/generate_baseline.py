"""Record baseline predictions from a running v0-original container.

Sends every row of ``src/data/future_unseen_examples.csv`` to ``POST /predict`` and writes
``tests/fixtures/baseline_predictions.json`` with, per row: the row identifier, the exact
payload sent and the predicted price. The ``meta`` block records the image the predictions
came from and its library versions, so the characterization test can tell whether it runs in
a comparable environment.

Standard library only: it runs on the host, whose Python and packages differ from the image.

Usage (the container must already be running):

    docker run -d --rm -p 8000:8000 --name housing-api-v0 mle-api:v0-original
    python3 scripts/generate_baseline.py --image mle-api:v0-original

Only the 8 fields of the v0 request schema are sent; the CSV's other columns are dropped and
``zipcode`` is sent as a string, as the v0 schema requires.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "src" / "data" / "future_unseen_examples.csv"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "baseline_predictions.json"

# v0 request schema (src/api/endpoints.py, HomeFeatures) and the type each field is sent as.
INT_FIELDS = ("bedrooms",)
FLOAT_FIELDS = ("bathrooms", "sqft_living", "sqft_lot", "floors", "sqft_above", "sqft_basement")
STR_FIELDS = ("zipcode",)

VERSIONS_SNIPPET = (
    "import json, platform, sklearn, numpy, pandas; "
    "print(json.dumps({'python': platform.python_version(), 'scikit-learn': sklearn.__version__,"
    " 'numpy': numpy.__version__, 'pandas': pandas.__version__}))"
)

logger = logging.getLogger("generate_baseline")


def build_payload(row: dict[str, str]) -> dict[str, Any]:
    """Map one CSV row to a v0 /predict payload with schema-correct types."""
    payload: dict[str, Any] = {}
    for name in INT_FIELDS:
        payload[name] = int(row[name])
    for name in FLOAT_FIELDS:
        payload[name] = float(row[name])
    for name in STR_FIELDS:
        payload[name] = row[name].strip()
    return payload


def predict(base_url: str, payload: dict[str, Any], timeout: float) -> float:
    """POST one payload and return ``predicted_price``; fail loudly on anything but 200."""
    request = urllib.request.Request(
        f"{base_url}/predict",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected status {response.status} for {payload}")
        body = json.load(response)
    return float(body["predicted_price"])


def image_meta(image: str) -> dict[str, Any]:
    """Image id and library versions of the image the predictions come from."""
    image_id = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    versions = json.loads(
        subprocess.run(
            ["docker", "run", "--rm", image, "python", "-c", VERSIONS_SNIPPET],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return {"image": image, "image_id": image_id, "versions": versions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--image", required=True, help="image the running container uses")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with args.csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    records = []
    for index, row in enumerate(rows):
        payload = build_payload(row)
        records.append(
            {
                "row_id": index,
                "input": payload,
                "predicted_price": predict(args.base_url, payload, args.timeout),
            }
        )
    logger.info("recorded %d predictions from %s", len(records), args.base_url)

    fixture = {
        "meta": {
            **image_meta(args.image),
            "source_csv": args.csv.resolve().relative_to(REPO_ROOT).as_posix(),
            "row_id": "0-based index of the data row in source_csv (header excluded)",
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator": "scripts/generate_baseline.py",
        },
        "predictions": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, indent=2) + "\n")
    logger.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
