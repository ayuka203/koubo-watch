"""既存 DB の tender_type を再分類する一回限りの移行スクリプト。

背景: tender_type 列は 2026-07-20 に新設された（Fable裁定）。マイグレーション
直後の既存レコードは全て tender_type='unknown' のまま。表示対象になりうる
（締切未到来 or 締切 NULL）レコードだけを対象に Haiku で分類し直す。
締切超過分は表示されず実害がないため、unknown のまま放置してよい。

Usage:
    python -m scripts.reclassify_tender_type --dry-run   # 対象件数だけ確認
    python -m scripts.reclassify_tender_type              # 実際に分類・更新する
    python -m scripts.reclassify_tender_type --max-tenders 50
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from src.classifier import classify_tender
from src.db import TenderORM, get_session, init_db

logger = logging.getLogger(__name__)


def _select_candidate_ids() -> list[int]:
    """未分類(unknown)かつ表示対象になりうる（締切未到来 or 締切 NULL）レコードの id 一覧を返す。

    tender_type が commissioned/subsidy に確定済みのレコードは対象外とする。
    再実行時に確定値が unknown へ巻き戻るのを防ぐため（src.main._run_ai_for_id
    と同じ「unknown 上書き禁止」原則をここでも守る）。
    """
    today = date.today()
    with get_session() as sess:
        rows = (
            sess.query(TenderORM)
            .filter((TenderORM.deadline.is_(None)) | (TenderORM.deadline >= today))
            .filter((TenderORM.tender_type.is_(None)) | (TenderORM.tender_type == "unknown"))
            .all()
        )
        return [r.id for r in rows]


def _reclassify_one(tender_id: int) -> str | None:
    """1 件を AI 分類し tender_type を更新する。

    AI が "unknown" と判定した場合は DB への書き込みを行わない（既存の
    確定値を巻き戻さないため。呼び出し時点で確定値が入っていることは
    無い想定だが、念のため src.main._run_ai_for_id と同じガードを置く）。

    Returns
    -------
    str | None
        AI が返した tender_type ("commissioned"/"subsidy"/"unknown")。
        レコード消失や AI 呼び出し失敗時は None。
    """
    with get_session() as sess:
        row = sess.query(TenderORM).filter_by(id=tender_id).first()
        if row is None:
            logger.warning("id=%d が見つかりません（削除済み？）", tender_id)
            return None
        title = row.title
        description = row.description

    try:
        assessment = classify_tender(title, description)
    except RuntimeError as exc:
        logger.error("id=%d の AI 判定に失敗しました: %s", tender_id, type(exc).__name__)
        logger.debug("詳細 id=%d", tender_id, exc_info=True)
        return None

    with get_session() as sess:
        row = sess.query(TenderORM).filter_by(id=tender_id).first()
        if row is None:
            logger.warning("AI 判定後に id=%d が DB から消失しました", tender_id)
            return None
        # AI が確定判定 (commissioned/subsidy) を返した場合のみ上書きする。
        # AI が "unknown" を返した場合、既存の確定値を unknown で書き潰さない
        # (src.main._run_ai_for_id と同じガード)。
        if assessment.tender_type != "unknown":
            row.tender_type = assessment.tender_type

    return assessment.tender_type


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="既存 DB の表示対象レコード（締切未到来 or NULL）に tender_type を再分類する"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="対象件数だけ表示して終了する（AI 呼び出しなし）",
    )
    parser.add_argument(
        "--max-tenders",
        type=int,
        default=None,
        help="処理件数の上限（省略時は全件）",
    )
    args = parser.parse_args()

    init_db()

    candidate_ids = _select_candidate_ids()
    print(
        f"表示対象になりうるレコード（締切未到来 or 締切NULL）: {len(candidate_ids)} 件",
        file=sys.stderr,
    )

    if args.max_tenders is not None and len(candidate_ids) > args.max_tenders:
        candidate_ids = candidate_ids[: args.max_tenders]
        print(f"--max-tenders により {len(candidate_ids)} 件に制限します", file=sys.stderr)

    if args.dry_run:
        print("dry-run モードのため、ここで終了します（AI 呼び出しなし）", file=sys.stderr)
        return 0

    counts: dict[str, int] = {"commissioned": 0, "subsidy": 0, "unknown": 0, "failed": 0}
    for tender_id in candidate_ids:
        result = _reclassify_one(tender_id)
        if result is None:
            counts["failed"] += 1
        else:
            counts[result] = counts.get(result, 0) + 1

    print("\n=== reclassify_tender_type 実行結果 ===", file=sys.stderr)
    print(f"  commissioned: {counts['commissioned']}", file=sys.stderr)
    print(f"  subsidy:      {counts['subsidy']}", file=sys.stderr)
    print(f"  unknown:      {counts['unknown']}", file=sys.stderr)
    print(f"  失敗:          {counts['failed']}", file=sys.stderr)

    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
