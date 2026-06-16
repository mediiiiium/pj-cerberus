#!/bin/bash
# Usage: bash setup.sh <target-repo-path> [ecosystem: npm|pip|none]
#
# Examples:
#   bash setup.sh ~/now-on-tap npm
#   bash setup.sh ~/podcast pip
#   bash setup.sh ~/brewdrop none

set -e

# pj-vauban のバージョン。各リポジトリに配るファイルにこの値を埋め込み、
# どの repo が古い構成のままか分かるようにする。更新は setup.sh を再実行するだけ。
VAUBAN_VERSION="1.4.0"

TARGET="$1"
ECOSYSTEM="${2:-none}"
VAUBAN_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$TARGET" = "--version" ] || [ "$TARGET" = "-v" ]; then
  echo "pj-vauban $VAUBAN_VERSION"
  exit 0
fi

if [ -z "$TARGET" ]; then
  echo "Usage: bash setup.sh <target-repo-path> [npm|pip|none]"
  echo "       bash setup.sh --version"
  exit 1
fi

if [ ! -d "$TARGET/.git" ]; then
  echo "Error: $TARGET is not a git repository"
  exit 1
fi

TARGET="$(cd "$TARGET" && pwd)"
echo "Setting up pj-vauban in: $TARGET (ecosystem: $ECOSYSTEM)"
echo ""

echo "pj-vauban version: $VAUBAN_VERSION"
echo ""

# 生成ファイルにはマーカーを埋め込む。再実行時、マーカーの無い既存ファイル
# （＝ユーザーが自前で用意した設定）は黙って消さず、.bak に退避してから上書きする。
MARKER="pj-vauban managed"
backup_if_foreign() {
  local f="$1"
  if [ -f "$f" ] && ! grep -q "$MARKER" "$f" 2>/dev/null; then
    cp "$f" "$f.bak"
    echo "⚠️ 既存の $(basename "$f") は pj-vauban 管理外でした → ${f}.bak に退避して上書きします"
    echo "   必要な独自設定は .bak から手動でマージしてください。"
  fi
}

# 1. scripts/gemini_review.py
mkdir -p "$TARGET/scripts"
backup_if_foreign "$TARGET/scripts/gemini_review.py"
cp "$VAUBAN_DIR/scripts/gemini_review.py" "$TARGET/scripts/gemini_review.py"
echo "✓ scripts/gemini_review.py"

# 2. .pre-commit-config.yaml
backup_if_foreign "$TARGET/.pre-commit-config.yaml"
cat > "$TARGET/.pre-commit-config.yaml" << 'EOF'
# pj-vauban managed — このファイルは setup.sh が生成します。再実行で上書きされます。
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
        language: python
        additional_dependencies: [google-genai]
        stages: [pre-push]
        pass_filenames: false
        always_run: true
        verbose: true
EOF
echo "✓ .pre-commit-config.yaml"

# 3. .github/workflows/semgrep.yml
mkdir -p "$TARGET/.github/workflows"
backup_if_foreign "$TARGET/.github/workflows/semgrep.yml"
cat > "$TARGET/.github/workflows/semgrep.yml" << 'EOF'
# pj-vauban managed — このファイルは setup.sh が生成します。再実行で上書きされます。
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
      # 検出があれば --error で job を落とし push/PR をブロックする
      - name: Semgrep scan
        run: semgrep scan --config=auto --sarif --output=semgrep.sarif --error
      # ブロックされても結果は必ず残す。
      # Security タブへの上げは public/GHAS のみ可。private で未契約だと失敗するが
      # continue-on-error で job は落とさない（ブロックは scan 側が担う）。
      - name: Upload SARIF to GitHub Security
        if: always()
        continue-on-error: true
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: semgrep.sarif
      # private repo でも確認できるよう SARIF を成果物としても残す
      - name: Upload SARIF artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: semgrep-sarif
          path: semgrep.sarif
EOF
echo "✓ .github/workflows/semgrep.yml"

# 3b. .semgrepignore（誤検知の抑制ポイント）
if [ ! -f "$TARGET/.semgrepignore" ]; then
  cat > "$TARGET/.semgrepignore" << 'EOF'
# Semgrep のスキャン対象から除外するパス
# https://semgrep.dev/docs/ignoring-files-folders-code
#
# 個別の検出を黙らせたい場合は、該当行の直前に  # nosemgrep  コメントを置く。
node_modules/
.venv/
venv/
vendor/
dist/
build/
__pycache__/
*.min.js
EOF
  echo "✓ .semgrepignore（新規生成）"
else
  echo "✓ .semgrepignore（既存を維持）"
fi

# 4. .github/dependabot.yml（ecosystem 指定時のみ）
if [ "$ECOSYSTEM" != "none" ]; then
  backup_if_foreign "$TARGET/.github/dependabot.yml"
  cat > "$TARGET/.github/dependabot.yml" << EOF
# pj-vauban managed — このファイルは setup.sh が生成します。再実行で上書きされます。
version: 2
updates:
  - package-ecosystem: "$ECOSYSTEM"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      all-dependencies:
        patterns:
          - "*"
EOF
  echo "✓ .github/dependabot.yml (ecosystem: $ECOSYSTEM)"
fi

# 5. pre-commit フックのインストール
cd "$TARGET"

# pre-commit 本体と detect-secrets（baseline 生成に使う）を入れる。
# Gemini レビューの google-genai は .pre-commit-config.yaml 側の
# additional_dependencies で pre-commit が隔離環境に入れるため、ここでは入れない。
# PEP 668（externally-managed-environment）対策で段階的にフォールバックする。
install_deps() {
  python3 -m pip install pre-commit detect-secrets --quiet --user 2>/dev/null && return 0
  python3 -m pip install pre-commit detect-secrets --quiet --user --break-system-packages 2>/dev/null && return 0
  python3 -m pip install pre-commit detect-secrets --quiet --break-system-packages 2>/dev/null && return 0
  return 1
}
if python3 -c "import pre_commit, detect_secrets" 2>/dev/null; then
  echo "✓ pre-commit / detect-secrets 既に導入済み（インストールskip）"
elif ! install_deps; then
  echo "✗ pre-commit / detect-secrets のインストールに失敗しました。"
  echo "  pipx での導入を検討してください:"
  echo "    pipx install pre-commit && pipx install detect-secrets"
  echo "  （生成済みの設定ファイルはそのまま残っています）"
  exit 1
else
  echo "✓ pre-commit / detect-secrets インストール済み"
fi

if [ ! -f ".secrets.baseline" ]; then
  python3 -m detect_secrets scan \
    --exclude-files 'node_modules/.*' \
    --exclude-files '\.venv/.*' \
    --exclude-files 'vendor/.*' \
    --exclude-files '__pycache__/.*' \
    --exclude-files 'dist/.*' \
    --exclude-files 'build/.*' \
    > .secrets.baseline
  echo "✓ .secrets.baseline（新規生成）"
  echo ""
  echo "  ⚠️ baseline には『生成時点で見つかった秘密』が既知として登録され、"
  echo "     以後スキャンから除外される。既存の本物の鍵が紛れていないか必ず棚卸しを:"
  echo "       python3 -m detect_secrets audit .secrets.baseline"
  echo "     本物が見つかったら baseline で蓋をせず、鍵をローテーションすること。"
  echo ""
else
  echo "✓ .secrets.baseline（既存を維持）"
fi

python3 -m pre_commit install
python3 -m pre_commit install --hook-type pre-push
echo "✓ pre-commit フック（commit + push）インストール済み"

echo ""
echo "完了: $TARGET"
echo ""
echo "残りの作業:"
echo "  1. GEMINI_API_KEY を環境変数に設定する"
echo "  2. GitHub Marketplace から Qodo Merge を連携する"
echo "  3. 作成されたファイルをコミット・プッシュする"
