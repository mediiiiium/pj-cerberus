# pj-cerberus — 設計メモ

Claude Pro の範囲内で複数の AI とセキュリティツールを組み合わせ、バグ・脆弱性・シークレット漏洩をできるだけ早い段階で検出するマルチレイヤー・コードレビュー構成の設計メモ。

実装はまだない。

---

## アーキテクチャ概要

コミットの流れに沿って4層でチェックする。

| レイヤー | ツール | タイミング | コスト |
| :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | ローカル開発中 | Claude Pro |
| pre-commit | detect-secrets | コミット前（ローカル） | 無料 |
| pre-push | Gemini 2.0 Flash + Semgrep | プッシュ前（ローカル） | 無料 |
| CI | Semgrep / Dependabot | GitHub Actions | 無料 |
| PR レビュー | Qodo Merge | プルリクエスト時 | 無料（個人プラン・制限あり） |

---

## レイヤー別設計

### Layer 1 — pre-commit: シークレットスキャン

**ツール:** detect-secrets

コミット前にステージされたファイルをスキャンし、APIキー・パスワード等の混入を防ぐ。
truffleHog は git 履歴の遡りスキャンに向いているが、pre-commit の用途には detect-secrets が軽量で適切。

**`.pre-commit-config.yaml`:**

```yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
```

**初回セットアップ:**

```bash
pip install detect-secrets pre-commit
detect-secrets scan > .secrets.baseline   # 既存ファイルのベースライン作成
pre-commit install
```

`.secrets.baseline` はリポジトリにコミットする。既知の偽陽性はここに登録して除外する。

---

### Layer 2 — pre-push: Gemini によるコードベース整合性チェック

**ツール:** Gemini 2.0 Flash（Google AI Studio 無料枠）

差分を Gemini に渡し、コードベース全体との設計上の矛盾・既知パターンから外れたバグ・セキュリティ上の懸念を確認する。Semgrep は既知パターンの検出が主なので、これを補う位置づけ。

**スクリプト構成:**

```
scripts/
  gemini_review.py    # Gemini API を呼び出すスクリプト
.git/hooks/pre-push   # フックからスクリプトを呼び出す
```

**`scripts/gemini_review.py`:**

```python
#!/usr/bin/env python3
import sys
import os
import google.generativeai as genai

PROMPT_TEMPLATE = """以下のコード差分をレビューしてください。

観点:
- 既存コードとの設計上の矛盾（命名規則・パターンの一貫性）
- 並行処理・非同期処理のバグの可能性
- エラーハンドリングの漏れ
- セキュリティ上の懸念（入力検証・認証・シークレットの扱いなど）

指摘がなければ「問題なし」とだけ返してください。
指摘がある場合はファイル名・行番号・内容を箇条書きで返してください。

---
{diff}
"""

def review(diff: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY が未設定のためスキップします。", file=sys.stderr)
        return "スキップ"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content(PROMPT_TEMPLATE.format(diff=diff))
    return response.text

if __name__ == "__main__":
    diff = sys.stdin.read()
    print(review(diff))
```

**`.git/hooks/pre-push`:**

```bash
#!/bin/bash

BRANCH=$(git rev-parse --abbrev-ref HEAD)
DIFF=$(git diff "origin/${BRANCH}"...HEAD 2>/dev/null)

# リモートに追跡ブランチがない場合（新規ブランチ）は直前コミットとの差分
if [ -z "$DIFF" ]; then
  DIFF=$(git diff HEAD~1..HEAD 2>/dev/null)
fi

if [ -z "$DIFF" ]; then
  exit 0
fi

echo "--- Gemini レビュー中 ---"
RESULT=$(echo "$DIFF" | python3 scripts/gemini_review.py)
echo "$RESULT"
echo "-------------------------"

# 「問題なし」またはスキップなら自動通過
if echo "$RESULT" | grep -qE "問題なし|スキップ"; then
  exit 0
fi

# 指摘があれば確認を求める
read -p "指摘があります。このままプッシュしますか？ [y/N] " confirm
[[ "$confirm" == "y" ]] || exit 1
```

```bash
chmod +x .git/hooks/pre-push
pip install google-generativeai
```

**制約:**

- `GEMINI_API_KEY` が未設定の場合はスキップして通過する（CI では Gemini なしで動く）
- 差分が非常に大きい場合（数千行）はトークン上限に当たる可能性がある
- AI の指摘は参考情報であり、プッシュをブロックするかは開発者が判断する

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

`--config=auto` は Semgrep が言語を自動判別して公開ルールセットを適用する。Community 版のルールセットのみで pro ルールは含まれない。

#### Dependabot（依存関係スキャン）

**`.github/dependabot.yml`:**

```yaml
version: 2
updates:
  - package-ecosystem: "npm"   # 言語に合わせて変更（pip / cargo / go / etc.）
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      all-dependencies:
        patterns:
          - "*"
```

`groups` を設定しないと依存関係ごとに個別 PR が作成され、リポジトリが PR まみれになる。`groups` でまとめることで週1本に抑えられる。

---

### Layer 4 — PR: Qodo Merge

GitHub Marketplace から Qodo Merge を GitHub App として連携すると、PR 作成時にロジック・セキュリティ・テストカバレッジの観点で自動コメントされる。

**無料プランの制限（2025年時点）:**

個人プランは無料でプライベートリポジトリに対応しているが、月あたりの PR 数や機能に制限がある。最新の制限は [Qodo の料金ページ](https://www.qodo.ai/pricing/) で確認すること。変更される可能性があるためここには記載しない。

---

## ワークフロー

```
ローカル実装（Claude Code）
    ↓
git commit
    → detect-secrets がシークレット検出（問題があればコミット失敗）
    ↓
git push 前
    → Semgrep ローカル実行（任意）
    → Gemini pre-push フックが差分レビュー → 指摘があれば確認
    ↓
git push
    → GitHub Actions で Semgrep が自動実行
    → Dependabot が依存関係を週次でスキャン
    ↓
PR 作成
    → Qodo Merge が自動コメント
    ↓
人間が最終確認 → マージ
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

**人間確認の形骸化リスク:**

複数の AI ツールが「問題なし」と判断した後の人間確認は、注意力が下がりやすい。このシステムはバグや脆弱性を発見する機会を増やすものであり、人間がコードを読む習慣の代替ではない。AI の指摘がない状態でのマージは「問題がないこと」を意味しない。
