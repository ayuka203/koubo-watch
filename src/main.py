"""koubo-watch CLI エントリーポイント。

処理フロー:
  1. init_db() で DB 準備
  2. --rebuild-site-only でなければ fetchers から案件を取得し DB に保存
  3. --dry-run / --skip-ai でなければ AI 判定を実行して DB を更新
  4. build_site() で静的サイトを生成
  5. stats 出力

--classify-pending モード:
  DB から energy_system_score IS NULL のレコードを取得し AI 判定のみ走らせる別パス。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.db import TenderORM, get_session, init_db, upsert_tender
from src.filter import classify, is_excluded, load_keywords, pre_label_tender_type
from src.fetchers.jst import fetch_recent as jst_fetch
from src.fetchers.mext import fetch_recent as mext_fetch
from src.fetchers.nedo import fetch_recent as nedo_fetch
from src.fetchers.jgrants import fetch_recent as jgrants_fetch
from src.generator import build_site

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
PUBLIC_DIR = _PROJECT_ROOT / "public"
_CONFIG_DIR = _PROJECT_ROOT / "config"
_KEYWORDS_PATH = _CONFIG_DIR / "keywords.json"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="koubo-watch: 官公庁公募案件の収集と要約")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="AI 呼ばず、fetch + filter + DB 投入まで",
    )
    parser.add_argument(
        "--rebuild-site-only",
        action="store_true",
        help="新規 fetch せず、DB から site のみ再生成",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="過去案件取り込みモード",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="--backfill 用、YYYY-MM-DD 形式",
    )
    parser.add_argument(
        "--skip-ai",
        action="store_true",
        help="AI 判定をスキップ（--backfill と組み合わせて使用）",
    )
    parser.add_argument(
        "--classify-pending",
        action="store_true",
        help="energy_system_score が NULL の既存レコードに AI 判定を流す",
    )
    parser.add_argument(
        "--max-tenders",
        type=int,
        default=100,
    )
    args = parser.parse_args()

    # Validate --since format
    since_date: date | None = None
    if args.since is not None:
        try:
            since_date = date.fromisoformat(args.since)
        except ValueError:
            print(
                f"ERROR: --since は YYYY-MM-DD 形式で指定してください。got: {args.since!r}",
                file=sys.stderr,
            )
            return 1

    # ------------------------------------------------------------------
    # 1. DB 準備
    # ------------------------------------------------------------------
    init_db()

    # ------------------------------------------------------------------
    # --classify-pending モード（別パス）
    # ------------------------------------------------------------------
    if args.classify_pending:
        return _run_classify_pending(args.max_tenders)

    stats: dict = {
        "fetched": 0,
        "truncated_to": None,  # set to int when --max-tenders trims the list
        "excluded": 0,
        "no_category": 0,
        "upserted": 0,
        "ai_classified": 0,
        "ai_skipped": 0,
        "failed": 0,
        # tender_type pre-labelling (filter.pre_label_tender_type の結果内訳)
        "tender_type_subsidy_pre_labeled": 0,  # 助成型と確定 → AI呼び出し自体をスキップ
        "tender_type_commissioned_pre_labeled": 0,  # 受注型と確定 → スコアのみ AI 算出
        "tender_type_unknown_pre_labeled": 0,  # 未確定 → AI が tender_type も含め確定
    }
    no_category_examples: list[str] = []

    if not args.rebuild_site_only:
        # --------------------------------------------------------------
        # 2. fetch + filter + DB 投入
        # --------------------------------------------------------------
        try:
            keywords = load_keywords(_KEYWORDS_PATH)
        except (FileNotFoundError, ValueError) as exc:
            print(f"FAIL: keywords 読み込みエラー: {exc}", file=sys.stderr)
            return 1

        exclude_keywords: list[str] = keywords.get("exclude", [])

        fetchers = [
            ("jst", jst_fetch),
            ("mext", mext_fetch),
            ("nedo", nedo_fetch),
            ("jgrants", jgrants_fetch),
        ]

        raw_tenders = []
        for name, fn in fetchers:
            try:
                results = fn()
                logger.info("%s: %d 件取得", name, len(results))
                raw_tenders.extend(results)
            except Exception as exc:
                print(f"FAIL: fetcher {name}: {exc}", file=sys.stderr)
                stats["failed"] += 1

        stats["fetched"] = len(raw_tenders)

        # Apply max-tenders limit
        if len(raw_tenders) > args.max_tenders:
            logger.info(
                "取得件数 %d 件 > --max-tenders %d、切り詰めます。",
                len(raw_tenders),
                args.max_tenders,
            )
            stats["truncated_to"] = args.max_tenders
            raw_tenders = raw_tenders[: args.max_tenders]

        # --backfill --since フィルタ
        if args.backfill and since_date is not None:
            raw_tenders = [
                t for t in raw_tenders
                if t.posted_date is None or t.posted_date >= since_date
            ]
            logger.info("backfill filter: %d 件残", len(raw_tenders))

        skip_ai = args.dry_run or args.skip_ai

        for tender in raw_tenders:
            try:
                # 除外フィルタ
                if is_excluded(tender.title, exclude_keywords):
                    stats["excluded"] += 1
                    continue

                # カテゴリ判定
                categories = classify(tender.title, tender.description, keywords)
                if not categories:
                    logger.debug(
                        "skip (no category match): source=%s title=%s",
                        tender.source,
                        tender.title[:100],
                    )
                    no_category_examples.append(tender.title)
                    stats["no_category"] += 1
                    continue

                # tender_type 事前ラベリング（キーワードシグナルによる仮判定）
                pre_label = pre_label_tender_type(tender.title, tender.description, keywords)
                if pre_label == "subsidy":
                    stats["tender_type_subsidy_pre_labeled"] += 1
                elif pre_label == "commissioned":
                    stats["tender_type_commissioned_pre_labeled"] += 1
                else:
                    stats["tender_type_unknown_pre_labeled"] += 1

                # DB 投入（pre_label を仮の tender_type として保存）
                with get_session() as sess:
                    row = upsert_tender(sess, tender, categories, tender_type=pre_label)
                    tender_id = row.id

                stats["upserted"] += 1

                # AI 判定
                if skip_ai:
                    stats["ai_skipped"] += 1
                    continue

                # pre_label が "subsidy" でも AI 判定はスキップしない(security監査
                # MEDIUM対応、案A採用)。理由: pre-label はキーワード単一シグナルの
                # 仮判定に過ぎず、"支援金"等の広めの語が commissioned 案件の説明文に
                # 混ざるだけで subsidy に確定してしまう。以前はここで AI 呼び出し
                # 自体をスキップしていたため、pre-label 側の誤判定を訂正する機会が
                # 一切なく永久に非表示になるリスクがあった（"unknown 巻き戻り防止"
                # とは逆に、"subsidy 固定化" が過剰保護になっていた）。
                # commissioned/unknown は元々 AI 呼び出し必須だったので、この変更で
                # 追加コストが発生するのは pre-label subsidy 分のみ（母数は少ない
                # 想定）。案B(内部的に unknown のまま残し表示のみ抑制)は、
                # generator._is_active の判定ロジックに tender_type 以外の抑制フラグ
                # を追加する必要があり設計変更が大きいため見送った。
                _run_ai_for_id(tender_id, stats)

            except Exception as exc:
                print(f"FAIL: {tender.url}: {exc}", file=sys.stderr)
                stats["failed"] += 1

    # ------------------------------------------------------------------
    # 3. サイト生成
    # ------------------------------------------------------------------
    with get_session() as sess:
        all_tenders = sess.query(TenderORM).all()

    build_site(PUBLIC_DIR, all_tenders)

    # ------------------------------------------------------------------
    # 4. stats 出力
    # ------------------------------------------------------------------
    print("\n=== koubo-watch 実行結果 ===", file=sys.stderr)
    if not args.rebuild_site_only:
        print(f"  取得:            {stats['fetched']}", file=sys.stderr)
        if stats.get("truncated_to") is not None:
            print(
                f"  上限切詰め:       {stats['fetched']} → {stats['truncated_to']}",
                file=sys.stderr,
            )
        print(f"  除外フィルタ:     {stats['excluded']}", file=sys.stderr)
        print(f"  カテゴリなし:     {stats['no_category']}", file=sys.stderr)
        if no_category_examples:
            print("    例:", file=sys.stderr)
            for t in no_category_examples[:3]:
                print(f"      - {t[:80]}", file=sys.stderr)
        print(f"  DB 投入:         {stats['upserted']}", file=sys.stderr)
        print(
            f"  tender_type 仮判定: "
            f"subsidy={stats['tender_type_subsidy_pre_labeled']} "
            f"commissioned={stats['tender_type_commissioned_pre_labeled']} "
            f"unknown={stats['tender_type_unknown_pre_labeled']}",
            file=sys.stderr,
        )
        print(f"  AI 判定:         {stats['ai_classified']}", file=sys.stderr)
        print(f"  AI スキップ:      {stats['ai_skipped']}", file=sys.stderr)
        print(f"  失敗:            {stats['failed']}", file=sys.stderr)
    print(f"  サイト出力先:     {PUBLIC_DIR}", file=sys.stderr)

    return 0 if stats.get("failed", 0) == 0 else 1


def _run_ai_for_id(tender_id: int, stats: dict) -> None:
    """DB から tender を取得し AI 判定を実行して保存する。"""
    from src.classifier import classify_tender

    with get_session() as sess:
        row = sess.query(TenderORM).filter_by(id=tender_id).first()
        if row is None:
            logger.warning("tender_id=%d が見つかりません", tender_id)
            return
        title = row.title
        description = row.description

    try:
        assessment = classify_tender(title, description)
    except RuntimeError as exc:
        logger.error("AI 判定失敗 id=%d: %s", tender_id, type(exc).__name__)
        logger.debug("AI 判定失敗 詳細 id=%d", tender_id, exc_info=True)
        stats["failed"] += 1
        return

    with get_session() as sess:
        row = sess.query(TenderORM).filter_by(id=tender_id).first()
        if row is None:
            logger.warning(
                "AI 判定後に Tender id=%d が DB から消失しました (skipping persist)",
                tender_id,
            )
            return
        row.energy_system_score = float(assessment.energy_system_score)
        row.ai_reason = assessment.reason
        row.is_research = assessment.is_research
        # tender_type: AI が確定判定 (commissioned/subsidy) を返した場合のみ上書き。
        # AI が "unknown" を返した場合、pre_label 由来の既存の確定値
        # (commissioned/subsidy) を unknown で書き潰さない。
        if assessment.tender_type != "unknown":
            row.tender_type = assessment.tender_type

    stats["ai_classified"] += 1
    logger.info(
        "AI 判定完了 id=%d score=%d is_research=%s",
        tender_id,
        assessment.energy_system_score,
        assessment.is_research,
    )


def _run_classify_pending(max_tenders: int) -> int:
    """energy_system_score IS NULL のレコードに AI 判定を流す。"""
    logger.info("--classify-pending モード開始")
    # Use same keys as _run_ai_for_id to avoid KeyError
    stats = {"ai_classified": 0, "failed": 0}

    with get_session() as sess:
        pending = (
            sess.query(TenderORM)
            .filter(TenderORM.energy_system_score.is_(None))
            .limit(max_tenders)
            .all()
        )
        pending_ids = [r.id for r in pending]

    logger.info("pending: %d 件", len(pending_ids))

    for tender_id in pending_ids:
        _run_ai_for_id(tender_id, stats)

    # サイト再生成
    with get_session() as sess:
        all_tenders = sess.query(TenderORM).all()
    build_site(PUBLIC_DIR, all_tenders)

    print("\n=== classify-pending 実行結果 ===")
    print(f"  AI 判定:  {stats['ai_classified']}")
    print(f"  失敗:     {stats['failed']}")

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
