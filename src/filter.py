"""Keyword-based category classifier for koubo-watch.

All string comparisons are performed after Unicode NFC normalisation and
ASCII lowercasing so that half-width/full-width differences do not cause
missed matches.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_keywords(path: Path) -> dict:
    """Load and return the keywords dictionary from a JSON file.

    Raises FileNotFoundError if the path does not exist, and ValueError if
    the JSON is malformed or has an unexpected top-level structure.
    """
    if not path.exists():
        raise FileNotFoundError(f"keywords file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"keywords.json must be a JSON object, got {type(data)}")
    return data


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Apply Unicode NFC normalisation then convert to lower-case."""
    return unicodedata.normalize("NFC", s).lower()


# ---------------------------------------------------------------------------
# Exclusion check
# ---------------------------------------------------------------------------


def is_excluded(text: str, exclude_keywords: list[str]) -> bool:
    """Return True if *text* contains any of the exclusion keywords.

    The comparison is NFC-normalised and case-insensitive.
    """
    if not text:
        return False
    normalised = _normalize(text)
    for kw in exclude_keywords:
        if _normalize(kw) in normalised:
            return True
    return False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    title: str,
    description: str | None,
    keywords: dict,
) -> dict[str, list[str]]:
    """Return matched sub-categories for each top-level category.

    ``keywords`` is the full dict loaded by :func:`load_keywords`.  The
    special ``"exclude"`` key is ignored here (callers should check
    :func:`is_excluded` separately).

    Returns a dict like::

        {
            "原子力": ["廃炉・廃棄物", "次世代炉・核融合"],
            "送配電": ["インフラ・保安・規制"],
        }

    An empty dict means no category matched.
    """
    combined = _normalize((title or "") + " " + (description or ""))

    result: dict[str, list[str]] = {}

    for category, sub_dict in keywords.items():
        if category == "exclude":
            continue
        if not isinstance(sub_dict, dict):
            # Unexpected structure — skip gracefully
            continue

        matched_subs: list[str] = []
        for sub_name, kw_list in sub_dict.items():
            if not isinstance(kw_list, list):
                continue
            for kw in kw_list:
                if _normalize(kw) in combined:
                    matched_subs.append(sub_name)
                    break  # One match per sub-category is enough

        if matched_subs:
            # Sort sub-categories alphabetically for deterministic output
            result[category] = sorted(matched_subs)

    return result


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def flatten_keyword_hits(classification: dict[str, list[str]]) -> list[str]:
    """Return a flat sorted list of all matched sub-category names."""
    hits: list[str] = []
    for sub_list in classification.values():
        hits.extend(sub_list)
    return sorted(hits)
