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
    pre_label_tender_type,
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


def test_classify_tender_type_hints_key_ignored(keywords):
    """The 'tender_type_hints' key must not be treated as a classify() category
    (regression: '調達' is a commissioned hint keyword, but must not make
    unrelated tenders match a bogus 'tender_type_hints' category)."""
    result = classify("一般事務用品の調達について", "ボールペン、コピー用紙", keywords)
    assert "tender_type_hints" not in result
    assert result == {}


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


# ---------------------------------------------------------------------------
# classify — broadened vocabulary (Jグランツ実態語彙)
# ---------------------------------------------------------------------------


def test_classify_solar_power_grant(keywords):
    """「太陽光発電補助金」は 送配電/再エネ統合・調整力 にヒットする。"""
    result = classify("太陽光発電補助金の交付について", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_hydrogen_supply_chain(keywords):
    """「水素サプライチェーン研究開発」は 送配電/再エネ統合・調整力 にヒットする。"""
    result = classify("水素サプライチェーン研究開発事業", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_energy_saving_equipment(keywords):
    """「省エネ機器導入」は 送配電/再エネ統合・調整力 にヒットする。"""
    result = classify("省エネ機器導入支援補助金", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_carbon_neutral_demo(keywords):
    """「カーボンニュートラル実証」は 送配電/再エネ統合・調整力 にヒットする。"""
    result = classify("カーボンニュートラル実証事業の公募", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_nuclear_keyword_matches(keywords):
    """「原子力」単独語が 原子力/廃炉・廃棄物 にヒットする。"""
    result = classify("原子力に関する研究開発", None, keywords)
    assert "原子力" in result


def test_classify_nuclear_safety_regulation(keywords):
    """「原子力規制」が 原子力/安全・規制・再稼働 にヒットする。"""
    result = classify("原子力規制委員会への申請支援", None, keywords)
    assert "原子力" in result
    assert "安全・規制・再稼働" in result["原子力"]


def test_classify_radioactive_material_decontamination(keywords):
    """「放射性物質」が 放射線/環境・除染 にヒットする。"""
    result = classify("放射性物質汚染対処のための調査", None, keywords)
    assert "放射線" in result
    assert "環境・除染" in result["放射線"]


def test_classify_wind_power(keywords):
    """「風力発電」が 送配電/再エネ統合・調整力 にヒットする。"""
    result = classify("風力発電事業への補助金", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_decarbonization(keywords):
    """「脱炭素」が 送配電/再エネ統合・調整力 にヒットする。"""
    result = classify("脱炭素社会実現に向けた技術開発", None, keywords)
    assert "送配電" in result
    assert "再エネ統合・調整力" in result["送配電"]


def test_classify_electricity_business(keywords):
    """「電気事業」が 送配電/系統運用・市場 にヒットする。"""
    result = classify("電気事業に関する制度調査", None, keywords)
    assert "送配電" in result
    assert "系統運用・市場" in result["送配電"]


# ---------------------------------------------------------------------------
# pre_label_tender_type
# ---------------------------------------------------------------------------


def test_pre_label_subsidy_signal(keywords):
    result = pre_label_tender_type("再エネ導入支援補助金の交付決定について", None, keywords)
    assert result == "subsidy"


def test_pre_label_commissioned_signal(keywords):
    result = pre_label_tender_type("系統安定化技術の調査委託業務", None, keywords)
    assert result == "commissioned"


def test_pre_label_no_signal_returns_unknown(keywords):
    result = pre_label_tender_type("廃炉技術に関する研究開発", None, keywords)
    assert result == "unknown"


def test_pre_label_signal_in_description(keywords):
    """description 側のシグナルも判定に使われる。"""
    result = pre_label_tender_type("案件名", "本業務は入札により発注する", keywords)
    assert result == "commissioned"


def test_pre_label_both_signals_fallback_unknown(keywords):
    """subsidy/commissioned 両方に一致した場合は unknown にフォールバックする
    （優先順位を決め打ちで確定させるより、AI 判定に委ねる方が安全）。"""
    result = pre_label_tender_type("助成金交付と業務委託を組み合わせた事業", None, keywords)
    assert result == "unknown"


def test_pre_label_empty_title_and_description(keywords):
    assert pre_label_tender_type("", None, keywords) == "unknown"


def test_pre_label_missing_hints_key_returns_unknown():
    """tender_type_hints キーが無い keywords dict でも例外にならず unknown を返す。"""
    result = pre_label_tender_type("補助金交付決定", None, {"exclude": []})
    assert result == "unknown"


def test_pre_label_case_and_nfc_insensitive(keywords):
    """大文字小文字・NFC正規化の違いを吸収する。"""
    import unicodedata

    nfd_title = unicodedata.normalize("NFD", "業務委託の入札公告")
    result = pre_label_tender_type(nfd_title, None, keywords)
    assert result == "commissioned"
