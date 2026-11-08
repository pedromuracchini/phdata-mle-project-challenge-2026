"""Fit the KNN imputer once and save it as a committed artifact, or check the committed one.

    uv run python scripts/fit_imputer.py          # write imputer.joblib + imputer_metadata.json
    uv run python scripts/fit_imputer.py --check  # refit in memory, exit 1 on any difference

(``make fit-imputer`` / ``make check-imputer``.) Always run through uv in the locked
environment: the metadata records the library versions, and ``--check`` fails if they differ.
Paths come from ``core/config.py`` (``APP_*`` environment variables).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# src/ modules are top-level packages (pytest.ini pythonpath, mypy_path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.config import Settings  # noqa: E402
from core.imputer_artifact import check_artifact, fit_and_save  # noqa: E402

logger = logging.getLogger("fit_imputer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="refit in memory and compare with the committed artifact; write nothing",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()

    if args.check:
        problems = check_artifact(settings)
        for problem in problems:
            logger.error("%s", problem)
        if problems:
            logger.error("imputer artifact check FAILED (%d problems)", len(problems))
            return 1
        logger.info("imputer artifact matches a refit: %s", settings.imputer_path)
        return 0

    result = fit_and_save(settings)
    logger.info(
        "wrote %s (%d rows x %d columns, sha256 %s) and %s",
        settings.imputer_path,
        result.metadata["fit_data"]["n_rows"],
        len(result.metadata["columns"]),
        result.metadata["artifact_sha256"],
        settings.imputer_metadata_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
