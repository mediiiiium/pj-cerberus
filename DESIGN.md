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
# pj-vauban managed — setup.sh が生成。再実行で上書きされる（後述のマーカー方式）。
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
        exclude: '(^|/)(node_modules|\.venv|venv|vendor|dist|build|__pycache__)/'

  - repo: local
    hooks:
      - id: gemini-review
        name: Gemini Code Review
        entry: python scripts/gemini_review.py
        language: python                       # pre-commit が隔離 venv を作る
        additional_dependencies: [google-genai]  # その venv に依存を入れる
        stages: [pre-push]
        pass_filenames: false
        always_run: true
        verbose: true   # exit 0 でも結果を表示させる（無いと出力が握り潰される）
```

`language: python` + `additional_dependencies` にすることで、google-genai は **pre-commit が管理する隔離環境**に入る。グローバル python に依存しないため「別の python3 で動いて import 失敗→無言スキップ」を防げる（`language: system` の弱点）。初回 push 時に venv 構築でやや時間がかかる。

detect-secrets（pre-commit）と Gemini レビュー（pre-push）を両方ここで管理する。`.git/hooks/` を直接編集しないため、ファイルとしてコミット可能。

`.secrets.baseline` はリポジトリにコミットする。既知の偽陽性はここに登録して除外する。

**⚠️ baseline は導入直後に必ず棚卸しする:**

`detect-secrets scan > .secrets.baseline` は「**生成時点で見つかった秘密をすべて『既知』として記録**」し、以後のスキャンから除外する。つまり導入前にすでにコミット済みの**本物の鍵があっても自動でホワイトリスト化**され、二度と警告されない。秘密漏洩を防ぐツールが既存の漏洩を見逃す入口になりうる。

そのため、baseline を生成したら**必ず一度 audit を通し**、各エントリが偽陽性か本物かを人が仕分けする:

```bash
detect-secrets audit .secrets.baseline
# 各検出を y（本物）/ n（偽陽性）で仕分け。
# 本物が見つかったら、その秘密をローテーション（無効化＋再発行）し、
# 履歴からの除去も検討する（git filter-repo 等）。baseline で蓋をしない。
```

setup.sh は baseline 新規生成時にこの棚卸しを促すメッセージを表示する。

---

### Layer 2 — pre-push: Gemini によるコードベース整合性チェック

**ツール:** Gemini Flash（既定 `gemini-2.5-flash`・Google AI Studio 無料枠）

差分を Gemini に渡し、コードベース全体との設計上の矛盾・セキュリティ上の懸念を確認する。Semgrep が既知パターンを検出するのに対して、Gemini は文脈・設計レベルの問題を補う。

実体は [`scripts/gemini_review.py`](scripts/gemini_review.py)（setup.sh が各リポジトリにコピー設置する）。ドリフト防止のためここにはコード全文を載せず、振る舞いだけ記す。

**振る舞い:**

1. `get_diff()` で push 対象の差分を取得する。`@{push}` → `@{upstream}` → `origin/HEAD` の順に merge-base 起点で diff を取り、複数コミットをまとめて push しても全体をレビューできるようにする（いずれも失敗したら最後の手段として `HEAD~1..HEAD`）。
2. `GEMINI_API_KEY`（必須）、`GEMINI_MODEL`（任意・既定 `gemini-2.5-flash`）、`GEMINI_TIMEOUT_MS`（任意・既定 10000）を env から読む。
3. 新 SDK（`from google import genai` → `genai.Client(...)`）で差分をレビューさせ、結果を標準出力に表示する。
4. **常に exit 0**（情報表示のみ・push はブロックしない）。

**制約・前提:**

- 使用パッケージは **`google-genai`（新 SDK）**。旧 `google-generativeai` ではない（import 形が異なる）。これは `.pre-commit-config.yaml` の `additional_dependencies` で **pre-commit が隔離環境に導入**するため、グローバル python への手動インストールは不要。
- `GEMINI_API_KEY` 未設定・SDK 未インストール・API エラー（レート超過等）・**タイムアウト超過**は、いずれもメッセージを出して**スキップ通過**する。CI では Gemini 無しで動く。
- **pre-push なので必ずタイムアウトを設ける**（`GEMINI_TIMEOUT_MS`・既定 10 秒）。API が応答しないときに `git push` がハングするのを防ぐ。超過時はスキップして push を通す。開発テンポを優先し短め（5 秒では正常応答を切る懸念があり 10 秒に設定）。
- **lock ファイル・自動生成物・ビルド成果物は差分から除外**してからレビューする（`package-lock.json` / `*.lock` / `*.min.js` / `*.map` 等）。これらが混ざると 100k 制限で実コードが切り捨てられ、ノイズと AI 送信面積も増える。除外後に実コード差分が無ければ API を叩かずスキップする。
- **指摘があるときだけ枠付きで強調表示**し、無いときは stderr に 1 行（`問題なし`）。push のログに埋もれて見落とす／オオカミ少年化するのを防ぐ。
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
permissions:
  contents: read
  security-events: write   # SARIF を Security タブへ上げるのに必要
jobs:
  semgrep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install 'semgrep~=1.166'   # v1系のパッチ/ルール追従を許容しつつ再現性を確保
      - name: Semgrep scan
        run: semgrep scan --config=auto --sarif --output=semgrep.sarif --error
      - name: Upload SARIF to GitHub Security
        if: always()
        continue-on-error: true
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: semgrep.sarif
      - name: Upload SARIF artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: semgrep-sarif
          path: semgrep.sarif
```

`--config=auto` は言語を自動判別して公開ルールセットを適用する。Community 版のルールセットのみで pro ルールは含まれない。

**ブロック方針と SARIF:**

- `--error` で**検出があれば job を落とし push/PR をブロック**する（「気づければいい」ではなく「止める」方針）。
- 結果は SARIF で出力し、`if: always()` で**ブロック時も必ず後段に渡す**。
- **Security タブへの掲載は public リポジトリ（または GitHub Advanced Security 契約済み）でのみ可能。** private で未契約だと upload は失敗するが、`continue-on-error: true` のため job は落とさない（ブロックは scan 側が担保）。
- private でも確認できるよう **SARIF を成果物（artifact）としても保存**する。Actions の実行結果からダウンロードできる。
- コンテナ（`semgrep/semgrep` image）はやめて `pip install semgrep` に変更。理由は、`upload-sarif` アクション（Node 製）がコンテナ内だと Node 不在で動かないことがあるため。

**誤検知の抑制（`.semgrepignore`）:**

スキャン対象から外したいパスは `.semgrepignore` に記述する（setup.sh が雛形を生成）。個別の検出だけ黙らせたい場合は、該当行の直前に `# nosemgrep` コメントを置く。「止まるが、正規の手順で黙らせられる」状態を保ち、`--error` の場当たり的な無効化を防ぐ。

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
        .github/workflows/semgrep.yml .semgrepignore
# Dependabot を設定した場合は追加
git add .github/dependabot.yml
git commit -m "add pj-vauban security setup"
git push
```

### 更新とバージョン（ドリフト対策）

`setup.sh` は配布ファイルを各リポジトリに**コピー**するため、本体を直しても既存 repo には自動反映されない。どの repo がどの版かを確認できるようバージョンを埋め込んでいる。

```bash
bash ~/pj-vauban/setup.sh --version            # 本体の版
python3 scripts/gemini_review.py --version      # 各 repo に入っている版
```

**更新方法は setup.sh の再実行のみ**（既存の `.secrets.baseline` / `.semgrepignore` は維持され、上書きされない）。版がズレている repo を見つけたら流し直す。7 repo 程度の個人運用ではこの「再実行で更新」で十分なため、サブモジュール等の重い共有はしていない。

**既存ファイルの保護（マーカー方式）:** setup.sh が生成するファイル（`.pre-commit-config.yaml` / `semgrep.yml` / `dependabot.yml` / `gemini_review.py`）の先頭には `# pj-vauban managed` マーカーを埋め込む。再実行時、対象ファイルが既に存在し**マーカーを含まない場合**（＝ユーザーが自前で用意した設定）は、黙って消さず **`*.bak` に退避してから**上書きする。マーカーを含む（＝前回 pj-vauban が生成した）場合はそのまま上書き＝更新。これにより「更新の上書き」と「既存資産の破壊防止」を両立する。導入先で独自の pre-commit フックや Actions を使っていた場合は `*.bak` から手動でマージする。

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
| Gemini（既定 2.5 Flash） | AI Studio の無料枠（レートリミット・モデル別quotaあり。`GEMINI_MODEL` で差替可） |
| detect-secrets | 完全無料（OSS） |
| Semgrep Community | 完全無料（プライベートリポジトリ含む）。ただし SARIF の Security タブ掲載は public / GHAS のみ |
| Dependabot | GitHub 標準機能（無料） |
| Qodo Merge | 個人プラン無料（制限あり・要確認） |

---

## データフロー（外部送信）

セキュリティ構成である以上、**自分のコードがどこへ送られるか**を把握しておく。本構成では、private なソースコードが以下の外部サービスへ送信される。承知の上で使う前提。

| 層 | サービス | 送信される内容 | 送信先 | 備考 |
| :--- | :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | 会話・ファイル内容 | Anthropic | Claude Pro |
| pre-commit / detect-secrets | （なし） | — | **ローカル完結** | 外部送信なし |
| pre-push / Gemini | Google AI Studio (Gemini API) | **git の差分**（diff） | Google | ⚠️ **無料枠は入力（コード差分）が Google の学習・人間レビューに利用されうる**（有料の Vertex AI とは規約が異なる）。lock/生成物は除外済みだが、実コードは送られる |
| CI / Semgrep | Semgrep（公開ルール取得） | コードは原則ローカルでスキャン。`--config=auto` は**ルールセットを取得**する | semgrep.dev | ログイン無しなら所見のアップロードはしない。SARIF は GitHub 内に留まる |
| CI / Dependabot | GitHub | 依存関係のメタデータ | GitHub | リポジトリと同じ管理下 |
| PR / Qodo Merge | Qodo | **PR の差分・コード** | Qodo (third-party) | GitHub App として権限付与。機微なコードを扱う repo では導入可否を個別判断 |

**判断材料:**

- ⚠️ **Gemini 無料枠の学習利用リスクは重い。** 商用化・特許を狙うコアロジック、契約上社外秘のコード、あるいは誤ってステージした認証情報などが含まれると、規約上 Google 側に学習・閲覧されうる。**秘匿すべきリポジトリでは `GEMINI_API_KEY` を設定せず Gemini 層を無効化する**こと（未設定なら自動スキップ）。どうしても AI レビューが要るなら、学習に使われない有料経路（Vertex AI 等）への切り替えを検討する。
- 同様に Qodo Merge も第三者にコードが渡る。機微な repo では App を入れない。
- ローカル完結なのは detect-secrets のみ。「最低限ここだけは効かせる」というミニマム構成も可能。

---

## 注意・限界

**ツールの限界:**

- detect-secrets は新しいシークレットパターンを自動学習しない。ルールは定期的に更新する必要がある。
- Semgrep は既知の脆弱性パターンの検出が主で、アプリ固有のビジネスロジックのバグは検出できない。
- Gemini はコンテキストウィンドウが大きいが、実際に「理解」して矛盾を検出できるかは差分の性質による。false positive・false negative の両方が起きる。
- Dependabot のバージョンアップが安全かどうかの判断は人間が行う。

**人間確認の形骸化リスク（Zaion check）:**

最終の人間確認を **Zaion check** と呼ぶ。すべての防壁を通り抜けたものを最後に止める守護神（最終ライン）であり、ここが形骸化すると多層構成全体が意味を失う。複数の AI ツールが「問題なし」と判断した後の人間確認は、注意力が下がりやすい。このシステムはバグや脆弱性を発見する機会を増やすものであり、人間がコードを読む習慣の代替ではない。AI の指摘がない状態でのマージは「問題がないこと」を意味しない。Zaion check だけは自動化・省略しない。

---

## 変更履歴

バージョンは `setup.sh --version` / `python3 scripts/gemini_review.py --version` で確認できる。

### v1.4.0（2026-06-16）— 第三者レビュー（Gemini 3.5 Flash）反映
- 🔴 **既存ファイルの破壊的上書きを防止（マーカー方式）**: 生成ファイルに `# pj-vauban managed` を埋め、再実行時マーカー無しの既存ファイルは `*.bak` に退避してから上書き。導入先の既存 pre-commit/Actions 設定の消失を防ぐ。
- Gemini レビューの差分から **lock/自動生成/ビルド成果物を除外**（100k 制限による実コード切り捨て・ノイズ・送信面積を抑制）。実コード差分が無ければ API を叩かずスキップ。
- **指摘があるときだけ枠付きで強調表示**、無いときは 1 行。見落とし／オオカミ少年化を防止。
- 無料枠 Gemini の **学習利用リスクの警告を格上げ**（秘匿 repo は `GEMINI_API_KEY` 未設定で無効化）。
- タイムアウト既定を **20 秒 → 10 秒**に短縮（開発テンポ優先）。
- Semgrep ピン留めを `==1.166.0` → `~=1.166`（v1 系のルール追従を許容）。
- detect-secrets フックに `exclude` 追加（#7）／依存導入を導入済みならスキップ（#8）。

### v1.3.0（2026-06-16）
- Semgrep を `semgrep==1.166.0` にバージョンピン留め（再現性・サプライチェーン対策）。
- MIT `LICENSE` を追加（公開準備）。

### v1.2.0（2026-06-16）— 堅牢性
- Gemini API に HTTP タイムアウト（`GEMINI_TIMEOUT_MS`・既定 20 秒）を追加。pre-push で API が応答しないと `git push` がハングする問題を解消。
- `setup.sh` の依存導入を PEP 668（externally-managed）対応で 3 段フォールバック化。失敗時は中途半端に止まらず pipx 案内を出して終了。
- Gemini フックを `language: python` + `additional_dependencies: [google-genai]` に変更し、依存を pre-commit の隔離環境で管理（グローバル python 依存による無言スキップを根絶）。

### v1.1.0（2026-06-16）— 改名＋Layer 2 修正＋運用整備
- **改名**: pj-cerberus → **pj-vauban**。最終人間レビュー層を **Zaion check** と命名。
- **Layer 2（Gemini）の致命的不具合を修正**: 旧 SDK `google-generativeai` → 新 `google-genai`／pre-commit フックに `verbose: true`（exit 0 でも結果表示）／`get_diff` を `@{push}→@{upstream}→origin/HEAD` の merge-base 起点に／モデルを `GEMINI_MODEL` 化・既定 `gemini-2.5-flash`（`gemini-2.0-flash` は無料枠 `limit:0` で離脱）／100k 超の差分切り捨てを警告化。
- **Semgrep**: SARIF 出力＋Security タブ連携（`if: always` / `continue-on-error`）＋ artifact 保存。`--error` のブロックは維持。`.semgrepignore` 雛形を生成。
- **運用**: `.secrets.baseline` の棚卸し手順（`detect-secrets audit`）を明記／外部送信のデータフロー表を追加／`VAUBAN_VERSION`・`--version` でコピー配布のドリフトを可視化。

---

## 外部レビュー履歴

設計・コードの死角を減らすため、開発に関与していない別ベンダーの LLM に二次レビューを依頼している（ベンダーを散らして指摘の重複・共倒れを避ける方針）。

### 2026-06-16 — Google AI Studio / Gemini 3.5 Flash

- **依頼方法:** `pj-vauban-review-bundle.md`（対象ファイル全文＋対応済み論点＋焦点プロンプト）を貼り、優先度別（🔴/🟡/🟢）で批判的にレビューさせた。
- **主な指摘と対応（→ v1.4.0 で反映）:**
  - 🔴 **既存 `.pre-commit-config.yaml` / workflow の無条件上書き** → マーカー方式（`# pj-vauban managed`）で既存資産を `*.bak` 退避してから上書き。
  - 🟡 lock/生成ファイルで実コードが 100k 制限で切り捨て → `get_diff` で除外。
  - 🟡 exit 0 の見落とし・オオカミ少年化 → 指摘時のみ強調表示／コード差分ゼロなら API 不発。
  - 🟡 無料枠 Gemini の学習利用リスク → 警告を格上げ。
  - 🟡 timeout 20s が長い → 10s。/ 🟡 semgrep ピン留めのドリフト → `~=1.166` に緩和。
  - 🟢 detect-secrets フックの `exclude`／導入済みなら install skip。
- **議論の上で見送り:** `semgrep-action` への全面移行（コンテナ/Node 依存が戻るため）、SCA 即時実行（npm/pip audit）、全 repo 一括同期ラッパー。いずれも「個人向けの軽量・引き締まったスコープ」を維持するための判断。
- 詳細な差分は [変更履歴 v1.4.0](#変更履歴) を参照。
