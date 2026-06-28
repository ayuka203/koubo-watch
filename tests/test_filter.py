"""Tests for src/filter.py — keyword classification."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from src.filter import (
    _normalize,
    classify,
    flatten_keyword_hits,
    is_excluded,
    load_keywords,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KEYWORDS_PATH = Path(__file__).parent.parent / "config" / "keywords.json"


@pytest.fixture(scope="module")
def keywords() -> dict:
    return load_keywords(KEYWORDS_PATH)


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------


def test_normalize_lowercases():
    assert _normalize("ABC") == "abc"


def test_normalize_nfc():
    # Composed form (NFC) vs decomposed form (NFD) for a Japanese character
    nfd_str = unicodedata.normalize("NFD", "が")
    nfc_str = unicodedata.normalize("NFC", "が")
    assert _normalize(nfd_str) == _normalize(nfc_str)


def test_normalize_empty():
    assert _normalize("") == ""


def test_normalize_mixed_case_ascii():
    assert _normalize("SMR") == "smr"


# ---------------------------------------------------------------------------
# load_keywords
# ---------------------------------------------------------------------------


def test_load_keywords_returns_dict(keywords):
    assert isinstance(keywords, dict)


def test_load_keywords_has_expected_top_keys(keywords):
    assert "原子力" in keywords
    assert "放射線" in keywords
    assert "送配電" in keywords
    assert "exclude" in keywords


def test_load_keywords_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_keywords(Path("/nonexistent/path/keywords.json"))


def test_load_keywords_invalid_json(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all", encoding="utf-8")
    with pytest.raises(Exception):  # json.JSONDecodeError
        load_keywords(bad_file)


def test_load_keywords_wrong_type(tmp_path):
    array_file = tmp_path / "array.json"
    array_file.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_keywords(array_file)


# ---------------------------------------------------------------------------
# is_excluded
# ---------------------------------------------------------------------------


def test_is_excluded_match(keywords):
    exclude_kws = keywords["exclude"]
    assert is_excluded("庁舎清掃業務の委託", exclude_kws) is True


def test_is_excluded_no_match(keywords):
    exclude_kws = keywords["exclude"]
    assert is_excluded("核融合炉の研究開発", exclude_kws) is False


def test_is_excluded_empty_text(keywords):
    assert is_excluded("", keywords["exclude"]) is False


def test_is_excluded_case_insensitive():
    # 'ALPS' appears in keywords in uppercase; test that lowercase matches
    kws = ["alps"]
    assert is_excluded("ALPS処理水に関する研究", kws) is True


def test_is_excluded_nfc_normalised():
    # Construct keyword list with NFD form
    nfd_kw = unicodedata.normalize("NFD", "廃炉")
    kws = [nfd_kw]
    nfc_text = unicodedata.normalize("NFC", "廃炉作業の公募")
    assert is_excluded(nfc_text, kws) is True


def test_is_excluded_partial_match():
    kws = ["印刷"]
    assert is_excluded("印刷物の調達に関する公募", kws) is True


# ---------------------------------------------------------------------------
# classify — basic matching
# ---------------------------------------------------------------------------


def test_classify_nuclear_废炉(keywords):
    result = classify("廃炉・汚染水対策に関する研究", None, keywords)
    assert "原子力" in result
    assert "廃炉・廃棄物" in result["原子力"]


def test_classify_nuclear_smr(keywords):
    result = classify("SMR安全評価手法の研究委託", None, keywords)
    assert "原子力" in result
    assert "次世代炉・核融合" in result["原子力"]


def test_classify_nuclear_httr_in_description(keywords):
    result = classify("高温ガス炉を活用した水素製造", "HTTRを用いた実証試験", keywords)
    assert "原子力" in result


def test_classify_radiation(keywords):
    result = classify("環境放射線モニタリング研究", "空間線量の測定", keywords)
    assert "放射線" in result
    assert "環境・除染" in result["放射線"]


def test_classify_grid_vpp(keywords):
    result = classify("VPPを活用したデマンドレスポンス実証", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_grid_market(keywords):
    result = classify("需給調整市場における調整力調達", None, keywords)
    assert "送配電" in result
    assert "系統運用・市場" in result["送配電"]


def test_classify_multiple_categories(keywords):
    """A single tender can match multiple top-level categories."""
    result = classify(
        "廃炉と電力系統安定化に関する研究開発",
        "燃料デブリ取り出しと系統保護の技術",
        keywords,
    )
    assert "原子力" in result
    assert "送配電" in result


def test_classify_multiple_subcategories(keywords):
    """A single tender can match multiple sub-categories within one category."""
    result = classify(
        "廃炉・高温ガス炉・核融合に関する総合研究",
        "燃料デブリおよびトカマク炉設計",
        keywords,
    )
    assert "原子力" in result
    subs = result["原子力"]
    assert "廃炉・廃棄物" in subs
    assert "次世代炉・核融合" in subs


def test_classify_no_match_returns_empty(keywords):
    result = classify("一般事務用品の調達について", "ボールペン、コピー用紙", keywords)
    assert result == {}


def test_classify_empty_title_and_description(keywords):
    result = classify("", None, keywords)
    assert result == {}


def test_classify_empty_title_with_none(keywords):
    result = classify("", "", keywords)
    assert result == {}


def test_classify_nfc_normalised_keyword(keywords):
    """Keywords with spaces (e.g. 'TRU 廃棄物') should match regardless of NFC."""
    result = classify("TRU 廃棄物の処理に関する研究", None, keywords)
    assert "原子力" in result
    assert "廃炉・廃棄物" in result["原子力"]


def test_classify_uppercase_ascii_keyword(keywords):
    """ASCII keywords like 'ALPS' should match case-insensitively."""
    result = classify("alps処理水の分析", None, keywords)
    assert "原子力" in result


def test_classify_exclude_key_ignored(keywords):
    """The 'exclude' key must not be treated as a category."""
    result = classify("職員採用のお知らせ", None, keywords)
    assert "exclude" not in result


def test_classify_result_is_sorted(keywords):
    """Sub-category lists should be consistently sorted."""
    result = classify(
        "廃炉 高速炉 再稼働 環境影響評価",
        None,
        keywords,
    )
    if "原子力" in result:
        subs = result["原子力"]
        assert subs == sorted(subs)


# ---------------------------------------------------------------------------
# flatten_keyword_hits
# ---------------------------------------------------------------------------


def test_flatten_keyword_hits_basic():
    classification = {
        "原子力": ["次世代炉・核融合", "廃炉・廃棄物"],
        "送配電": ["系統運用・市場"],
    }
    flat = flatten_keyword_hits(classification)
    assert sorted(flat) == flat  # should be sorted
    assert "次世代炉・核融合" in flat
    assert "廃炉・廃棄物" in flat
    assert "系統運用・市場" in flat


def test_flatten_keyword_hits_empty():
    assert flatten_keyword_hits({}) == []


def test_flatten_keyword_hits_single():
    result = {"放射線": ["環境・除染"]}
    assert flatten_keyword_hits(result) == ["環境・除染"]
