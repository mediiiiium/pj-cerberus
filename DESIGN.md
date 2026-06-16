# pj-vauban — 設計メモ

Claude Pro の範囲内で複数の AI とセキュリティツールを組み合わせ、バグ・脆弱性・シークレット漏洩をできるだけ早い段階で検出するマルチレイヤー・コードレビュー構成の設計メモ。

名前は、ルイ14世に仕えた軍事技師セバスティアン・ル・プレストル・ド・ヴォーバンにちなむ。星形要塞と稜堡システムで「縦深防御（defense in depth）」を体系化した人物で、力押しではなく手順と多重の防壁で段階的に守る思想を体現する。本構成も pre-commit → pre-push → CI → PR と防壁を重ね、最奥の主郭に人間の最終確認（**Zaion check**）を置く。

実装は `setup.sh` として完成している。

---

## アーキテクチャ概要

コミットの流れに沿って4層でチェックする。

| レイヤー | ツール | タイミング | コスト |
| :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | ローカル開発中 | Claude Pro |
| pre-commit | detect-secrets | コミット前（ローカル） | 無料 |
| pre-push | Gemini 2.0 Flash | プッシュ前（ローカル） | 無料 |
| CI | Semgrep / Dependabot | GitHub Actions | 無料 |
| PR レビュー | Qodo Merge | プルリクエスト時 | 無料（個人プラン・制限あり） |

---

## レイヤー別設計

### Layer 1 — pre-commit: シークレットスキャン

**ツール:** detect-secrets

コミット前にステージされたファイルをスキャンし、APIキー・パスワード等の混入を防ぐ。

**`.pre-commit-config.yaml`（各リポジトリに設置）:**

```yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']

  - repo: local
    hooks:
      - id: gemini-review
        name: Gemini Code Review
        entry: python3 scripts/gemini_review.py
        language: system
        stages: [pre-push]
        pass_filenames: false
        always_run: true
        verbose: true   # exit 0 でも結果を表示させる（無いと出力が握り潰される）
```

detect-secrets（pre-commit）と Gemini レビュー（pre-push）を両方ここで管理する。`.git/hooks/` を直接編集しないため、ファイルとしてコミット可能。

`.secrets.baseline` はリポジトリにコミットする。既知の偽陽性はここに登録して除外する。

---

### Layer 2 — pre-push: Gemini によるコードベース整合性チェック

**ツール:** Gemini 2.0 Flash（Google AI Studio 無料枠）

差分を Gemini に渡し、コードベース全体との設計上の矛盾・セキュリティ上の懸念を確認する。Semgrep が既知パターンを検出するのに対して、Gemini は文脈・設計レベルの問題を補う。

実体は [`scripts/gemini_review.py`](scripts/gemini_review.py)（setup.sh が各リポジトリにコピー設置する）。ドリフト防止のためここにはコード全文を載せず、振る舞いだけ記す。

**振る舞い:**

1. `get_diff()` で push 対象の差分を取得する。`@{push}` → `@{upstream}` → `origin/HEAD` の順に merge-base 起点で diff を取り、複数コミットをまとめて push しても全体をレビューできるようにする（いずれも失敗したら最後の手段として `HEAD~1..HEAD`）。
2. `GEMINI_API_KEY`（必須）と `GEMINI_MODEL`（任意・既定 `gemini-2.5-flash`）を env から読む。
3. 新 SDK（`from google import genai` → `genai.Client(...)`）で差分をレビューさせ、結果を標準出力に表示する。
4. **常に exit 0**（情報表示のみ・push はブロックしない）。

**制約・前提:**

- 使用パッケージは **`google-genai`（新 SDK）**。旧 `google-generativeai` ではない（import 形が異なる）。
- `GEMINI_API_KEY` 未設定・SDK 未インストール・API エラー（レート超過等）は、いずれもメッセージを出して**スキップ通過**する。CI では Gemini 無しで動く。
- **無料枠で使えるモデルは変わる。** 既定は `gemini-2.5-flash`（2026-06-16 時点で無料枠で稼働を実測確認）。旧 `gemini-2.0-flash` は無料枠が `limit: 0`（=実質不可）に外れたため使わない。他に `gemini-flash-latest` / `gemini-2.5-flash-lite` / `gemini-3.5-flash` 系も無料で通った。無料枠は変動するので、動かなくなったら `GEMINI_MODEL` で差し替える（`python3 -c "from google import genai,os; [print(m.name) for m in genai.Client(api_key=os.environ['GEMINI_API_KEY']).models.list()]"` で一覧確認）。
- 差分が 100,000 文字を超える場合は先頭のみレビューし、その旨を stderr に表示する（残りは未確認）。
- 結果は表示のみで push はブロックしない。ブロックしたい場合はスクリプト末尾を `exit 1` に変更する。
- pre-commit フックには `verbose: true` が必須。これが無いと exit 0 のフック出力はフレームワークに握り潰され、レビュー結果が画面に出ない。

---

### Layer 3 — CI: Semgrep + Dependabot

#### Semgrep（SAST）

**`.github/workflows/semgrep.yml`:**

```yaml
name: Semgrep
on:
  push:
    branches: [main]
  pull_request:
jobs:
  semgrep:
    runs-on: ubuntu-latest
    container:
      image: semgrep/semgrep
    steps:
      - uses: actions/checkout@v4
      - run: semgrep scan --config=auto --error
```

`--config=auto` は言語を自動判別して公開ルールセットを適用する。Community 版のルールセットのみで pro ルールは含まれない。

#### Dependabot（依存関係スキャン）

**`.github/dependabot.yml`（npm の例）:**

```yaml
version: 2
updates:
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      all-dependencies:
        patterns:
          - "*"
```

`groups` を設定しないと依存関係ごとに個別 PR が作成される。`groups` でまとめることで週1本に抑えられる。

GAS（Google Apps Script）・WordPress 等は Dependabot の対象外のため設定不要。

---

### Layer 4 — PR: Qodo Merge

GitHub Marketplace から Qodo Merge を GitHub App として連携すると、PR 作成時にロジック・セキュリティ・テストカバレッジの観点で自動コメントされる。

個人プランは無料でプライベートリポジトリに対応しているが、月あたりの PR 数や機能に制限がある。最新の制限は [Qodo の料金ページ](https://www.qodo.ai/pricing/) で確認すること。

---

## セットアップ手順

pj-vauban の `setup.sh` を対象リポジトリに対して実行するだけで全ファイルが展開される。

```bash
# Usage: bash setup.sh <target-repo-path> [npm|pip|none]

bash ~/pj-vauban/setup.sh ~/now-on-tap      npm
bash ~/pj-vauban/setup.sh ~/podcast         pip
bash ~/pj-vauban/setup.sh ~/pj-sora         pip
bash ~/pj-vauban/setup.sh ~/brewdrop        none
bash ~/pj-vauban/setup.sh ~/delivery_route  none   # delivery-app
bash ~/pj-vauban/setup.sh ~/delivery_log    none
bash ~/pj-vauban/setup.sh ~/mediiiiium      none   # mediiiiium-web
```

### setup.sh 実行後の手動作業

```bash
# 1. Gemini API キーを環境変数に設定（~/.zshrc 等に追記推奨）
export GEMINI_API_KEY="your_api_key"

# 2. GitHub Marketplace から Qodo Merge を各リポジトリに連携

# 3. 作成されたファイルをコミット
git add .pre-commit-config.yaml .secrets.baseline \
        scripts/gemini_review.py \
        .github/workflows/semgrep.yml
# Dependabot を設定した場合は追加
git add .github/dependabot.yml
git commit -m "add pj-vauban security setup"
git push
```

---

## ワークフロー

```
ローカル実装（Claude Code）
    ↓
git commit
    → detect-secrets がシークレット検出（問題があればコミット失敗）
    ↓
git push
    → Gemini pre-push フックが差分レビュー（結果を表示して通過）
    → GitHub Actions で Semgrep が自動実行
    → Dependabot が依存関係を週次でスキャン
    ↓
PR 作成
    → Qodo Merge が自動コメント
    ↓
Zaion check（人間の最終確認・最後の砦）→ マージ
```

---

## コスト整理

| ツール | 無料範囲 |
| :--- | :--- |
| Claude Code | Claude Pro に含まれる |
| Gemini 2.0 Flash | AI Studio の無料枠（レートリミットあり） |
| detect-secrets | 完全無料（OSS） |
| Semgrep Community | 完全無料（プライベートリポジトリ含む）|
| Dependabot | GitHub 標準機能（無料） |
| Qodo Merge | 個人プラン無料（制限あり・要確認） |

---

## 注意・限界

**ツールの限界:**

- detect-secrets は新しいシークレットパターンを自動学習しない。ルールは定期的に更新する必要がある。
- Semgrep は既知の脆弱性パターンの検出が主で、アプリ固有のビジネスロジックのバグは検出できない。
- Gemini はコンテキストウィンドウが大きいが、実際に「理解」して矛盾を検出できるかは差分の性質による。false positive・false negative の両方が起きる。
- Dependabot のバージョンアップが安全かどうかの判断は人間が行う。

**人間確認の形骸化リスク（Zaion check）:**

最終の人間確認を **Zaion check** と呼ぶ。すべての防壁を通り抜けたものを最後に止める守護神（最終ライン）であり、ここが形骸化すると多層構成全体が意味を失う。複数の AI ツールが「問題なし」と判断した後の人間確認は、注意力が下がりやすい。このシステムはバグや脆弱性を発見する機会を増やすものであり、人間がコードを読む習慣の代替ではない。AI の指摘がない状態でのマージは「問題がないこと」を意味しない。Zaion check だけは自動化・省略しない。
