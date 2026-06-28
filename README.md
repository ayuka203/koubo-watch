# koubo-watch

日本の官公庁公募案件を毎日自動収集して静的サイト化するシステム。

## 第 1 段階（本リポ）

- データ取得（Jグランツ API / NEDO HTML / JST RSS / 文科省 RSS）
- キーワードカテゴリ分類（原子力・放射線・送配電）
- SQLite DB 保存（SQLAlchemy 2.0 ORM）

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
│   │   ├── jgrants.py   — Jグランツ API クライアント
│   │   ├── nedo.py      — NEDO HTML スクレイパー
│   │   ├── jst.py       — JST RSS パーサー
│   │   └── mext.py      — 文科省 RSS パーサー
│   ├── filter.py        — キーワードカテゴリ分類
│   ├── db.py            — SQLite ORM
│   └── models.py        — Pydantic スキーマ
├── config/
│   └── keywords.json    — 分類キーワード辞書
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
