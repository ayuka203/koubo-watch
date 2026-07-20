# koubo-watch

日本の官公庁公募案件を毎日自動収集して静的サイト化するシステム。

## 第 1 段階（本リポ）

- データ取得（NEDO HTML / JST HTML / 文科省 RSS。Jグランツ API は補助金専用のため無効化中 — 2026-07-20 Fable裁定）
- キーワードカテゴリ分類（原子力・放射線・送配電）
- tender_type 判定（commissioned=受注型 / subsidy=助成型 / unknown）。事前ラベリング
  （キーワード）+ AI 判定の多段防御で、助成金案件（subsidy）を表示対象から除外する
- SQLite DB 保存（SQLAlchemy 2.0 ORM）

## 設計資産: Anthropic 構造化出力パターンの正典

`src/classifier.py` は、本プロジェクト群における **Anthropic API 構造化出力の正典（reference implementation）** である。以降の他プロジェクト（reg-harness の `d1_citations.py` / `read_inbox.py` 等）は、このパターンを複製・移植して利用している。踏襲すべき要点:

- `tools` + `tool_choice` による構造化出力（`output_config` は実在しないパラメータ。過去に本番障害を起こしたため使用禁止）
- `system` パラメータでロールを固定し、入力由来のプロンプトインジェクションを防ぐ
- `_sanitize_input()` による NFC 正規化・制御文字除去・注入パターン検知（日英両対応）
- pydantic レスポンスモデル + `_fix_schema()`（`additionalProperties: false` の再帰付与）

新規に Anthropic 連携を実装する際は、まず本ファイルを参照すること。

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# .env を編集して設定
```

## テスト実行

```bash
python -m pytest tests/ -v
```

全テストは実 HTTP を使用せずオフラインで実行可能。

## ディレクトリ構成

```
koubo-watch/
├── src/
│   ├── fetchers/
│   │   ├── jgrants.py   — Jグランツ API クライアント（無効化中。fetch_recent() は常に空リストを返す）
│   │   ├── nedo.py      — NEDO HTML スクレイパー
│   │   ├── jst.py       — JST 調達情報 HTML スクレイパー（旧 RSS は廃止済み）
│   │   └── mext.py      — 文科省 RSS パーサー
│   ├── filter.py        — キーワードカテゴリ分類 + tender_type 事前ラベリング
│   ├── classifier.py    — AI 判定（エネルギーシステム関連度スコア + tender_type確定）
│   ├── db.py            — SQLite ORM（tender_type 列のマイグレーションを含む）
│   └── models.py        — Pydantic スキーマ
├── scripts/
│   └── reclassify_tender_type.py  — 既存 DB の tender_type 再分類（一回限りの移行用）
├── config/
│   └── keywords.json    — 分類キーワード辞書（tender_type_hints を含む）
├── tests/
│   └── fixtures/        — オフラインテスト用サンプルデータ
└── data/
    └── koubo.sqlite      — SQLite DB（git 管理対象）
```

## 環境変数

| 変数名 | デフォルト | 説明 |
|---|---|---|
| `KOUBO_DB_PATH` | `data/koubo.sqlite` | SQLite DB ファイルパス |
| `ANTHROPIC_API_KEY` | — | Anthropic API キー（第 2 段階で使用） |
