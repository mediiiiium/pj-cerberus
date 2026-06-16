#!/usr/bin/env python3
import sys
import os
import subprocess


def get_diff() -> str:
    # push 対象（= ローカルにあって push 先にまだ無い変更）を優先的に拾う。
    # merge-base 起点なので、複数コミットをまとめて push しても全体をレビューできる。
    # @{push}     … push 先の追跡ブランチ（pre-push で最も正確）
    # @{upstream} … upstream 追跡ブランチ
    # origin/HEAD … リモートの既定ブランチ（新規ブランチで upstream 未設定のとき）
    candidates = [
        ["git", "diff", "--merge-base", "@{push}", "HEAD"],
        ["git", "diff", "--merge-base", "@{upstream}", "HEAD"],
        ["git", "diff", "--merge-base", "origin/HEAD", "HEAD"],
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
            ["git", "diff", "HEAD~1", "HEAD"], text=True, stderr=subprocess.DEVNULL
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

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        return response.text
    except Exception as e:
        print(f"[Gemini] APIエラーのためスキップします: {e}", file=sys.stderr)
        return ""


if __name__ == "__main__":
    diff = get_diff()

    if not diff:
        print("[Gemini] 差分なし。スキップします。")
        sys.exit(0)

    print("[Gemini] レビュー中...", file=sys.stderr)
    result = review(diff)

    if not result:
        sys.exit(0)

    print("\n=== Gemini レビュー結果 ===")
    print(result)
    print("===========================\n")

    # 常に exit 0（情報表示のみ、プッシュはブロックしない）
    # ブロックしたい場合は "問題なし" を含まないときに exit 1 に変更する
    sys.exit(0)
