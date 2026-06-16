#!/usr/bin/env python3
# pj-vauban managed — setup.sh が各リポジトリに配布します。再実行で上書きされます。
import sys
import os
import subprocess

# pj-vauban から配布されたバージョン。`--version` でどの構成が入っているか確認できる。
# setup.sh の VAUBAN_VERSION と揃える。更新は setup.sh の再実行で行う。
__version__ = "1.4.0"

# lock ファイル・自動生成物・ビルド成果物は差分から除外する。
# これらが混ざると 100k 文字制限で実コードが切り捨てられ、レビューから漏れる。
# また AI へ送るノイズ・プライバシー面積も減らせる。
EXCLUDES = [
    "--",
    ".",
    ":(exclude)*package-lock.json",
    ":(exclude)*yarn.lock",
    ":(exclude)*pnpm-lock.yaml",
    ":(exclude)*poetry.lock",
    ":(exclude)*Pipfile.lock",
    ":(exclude)*composer.lock",
    ":(exclude)*Cargo.lock",
    ":(exclude)*go.sum",
    ":(exclude)*.lock",
    ":(exclude)*.min.js",
    ":(exclude)*.min.css",
    ":(exclude)*.map",
]


def get_diff() -> str:
    # push 対象（= ローカルにあって push 先にまだ無い変更）を優先的に拾う。
    # merge-base 起点なので、複数コミットをまとめて push しても全体をレビューできる。
    # @{push}     … push 先の追跡ブランチ（pre-push で最も正確）
    # @{upstream} … upstream 追跡ブランチ
    # origin/HEAD … リモートの既定ブランチ（新規ブランチで upstream 未設定のとき）
    candidates = [
        ["git", "diff", "--merge-base", "@{push}", "HEAD", *EXCLUDES],
        ["git", "diff", "--merge-base", "@{upstream}", "HEAD", *EXCLUDES],
        ["git", "diff", "--merge-base", "origin/HEAD", "HEAD", *EXCLUDES],
    ]
    for cmd in candidates:
        try:
            diff = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            if diff.strip():
                return diff
        except subprocess.CalledProcessError:
            continue

    # 最後の手段: 直近コミットのみ
    try:
        return subprocess.check_output(
            ["git", "diff", "HEAD~1", "HEAD", *EXCLUDES],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return ""


def review(diff: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Gemini] GEMINI_API_KEY が未設定のためスキップします。", file=sys.stderr)
        return ""

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[Gemini] google-genai 未インストールのためスキップします。", file=sys.stderr)
        return ""

    if len(diff) > 100_000:
        print(
            "[Gemini] 差分が大きいため先頭 100,000 文字のみレビューします（残りは未確認）。",
            file=sys.stderr,
        )
        diff = diff[:100_000] + "\n... (差分が大きいため省略)"

    prompt = f"""以下のコード差分をレビューしてください。

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

    # 無料枠で使えるモデルは変わるため env で差し替え可能にする。
    # 例: gemini-2.5-flash / gemini-flash-latest など（要・最新の無料枠確認）
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # pre-push フックなので、API が応答しないと push がハングする。
    # 開発テンポを優先し既定 10 秒。超過時は例外→スキップして push を通す。
    timeout_ms = int(os.environ.get("GEMINI_TIMEOUT_MS", "10000"))

    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text or ""
    except Exception as e:
        print(f"[Gemini] スキップします（API エラー / タイムアウト）: {e}", file=sys.stderr)
        return ""


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"pj-vauban gemini_review {__version__}")
        sys.exit(0)

    diff = get_diff()

    # lock/生成物を除いた実コード差分が無ければ、API を叩かずスキップ（ノイズ削減）。
    if not diff:
        print("[Gemini] レビュー対象のコード差分なし。スキップします。", file=sys.stderr)
        sys.exit(0)

    print("[Gemini] レビュー中...", file=sys.stderr)
    result = review(diff)

    if not result:
        sys.exit(0)

    # 指摘が無いときは静かに 1 行だけ（push ログに埋もれて問題ない）。
    cleaned = result.strip().rstrip("。")
    if cleaned == "問題なし":
        print("[Gemini] 問題なし", file=sys.stderr)
        sys.exit(0)

    # 指摘があるときだけ枠で囲んで目立たせる（端末なら色付き）。
    use_color = sys.stdout.isatty()
    yel = "\033[1;33m" if use_color else ""
    rst = "\033[0m" if use_color else ""
    bar = "═" * 56
    print(f"\n{yel}╔{bar}╗{rst}")
    print(f"{yel}║  ⚠  Gemini レビュー指摘あり — 確認してください{rst}")
    print(f"{yel}╚{bar}╝{rst}")
    print(result)
    print(f"{yel}{'─' * 58}{rst}\n")

    # 常に exit 0（情報表示のみ、プッシュはブロックしない）。
    # ブロックしたい場合は、ここで sys.exit(1) に変更する。
    sys.exit(0)
