#!/usr/bin/env python3
"""
vauban rescue agent

対象リポジトリの Semgrep CI 失敗を検出し、修正ブランチを作成して PR を開く。
ルールベースの修正:
  - tests/** の findings → .semgrepignore に追加
  - その他（対応言語のみ） → 該当行に nosemgrep コメントを付与
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

GITHUB_OWNER = "mediiiiium"

RESCUE_BRANCH = "vauban/rescue-semgrep"
RESCUE_LABEL  = "vauban-rescue"

# 言語別 nosemgrep コメント形式。未対応拡張子はインライン修正をスキップ。
_COMMENT_FMT = {
    ".py":   "  # nosemgrep: {rule}",
    ".js":   "  // nosemgrep: {rule}",
    ".ts":   "  // nosemgrep: {rule}",
    ".jsx":  "  // nosemgrep: {rule}",
    ".tsx":  "  // nosemgrep: {rule}",
    ".go":   "  // nosemgrep: {rule}",
    ".java": "  // nosemgrep: {rule}",
    ".rb":   "  # nosemgrep: {rule}",
    ".sh":   "  # nosemgrep: {rule}",
    ".yaml": "  # nosemgrep: {rule}",
    ".yml":  "  # nosemgrep: {rule}",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def run(cmd, cwd=None, env=None):
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, cwd=cwd,
        env=(env if env is not None else os.environ),
    )


def _gh_env():
    """GH_TOKEN を明示的にセットした env を返す（gh CLI 認証用）。"""
    return {**os.environ, "GH_TOKEN": os.environ["GH_PAT"]}


def gh(*args):
    return run(["gh"] + list(args), env=_gh_env())


def gh_json(*args):
    return json.loads(gh(*args).stdout)


# ── per-repo logic ────────────────────────────────────────────────────────────

def latest_semgrep_failure(repo):
    """最新の semgrep run が失敗していれば run_id を返す。なければ None。"""
    runs = gh_json(
        "run", "list",
        "--repo", f"{GITHUB_OWNER}/{repo}",
        "--workflow", "semgrep.yml",
        "--limit", "1",
        "--json", "databaseId,conclusion",
    )
    if not isinstance(runs, list) or not runs or runs[0]["conclusion"] != "failure":
        return None
    return runs[0]["databaseId"]


def rescue_pr_exists(repo):
    prs = gh_json(
        "pr", "list",
        "--repo", f"{GITHUB_OWNER}/{repo}",
        "--head", RESCUE_BRANCH,
        "--json", "number",
    )
    return isinstance(prs, list) and bool(prs)


def download_sarif(repo, run_id, dest):
    try:
        gh(
            "run", "download", str(run_id),
            "--repo", f"{GITHUB_OWNER}/{repo}",
            "--name", "semgrep-sarif",
            "--dir", dest,
        )
        sarif_path = Path(dest) / "semgrep.sarif"
        return sarif_path if sarif_path.exists() else None
    except subprocess.CalledProcessError as e:
        print(f"  SARIF download failed: {e.stderr.strip()}", file=sys.stderr)
        return None


def parse_findings(sarif_path):
    try:
        data = json.loads(sarif_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"  Failed to parse SARIF: {e}", file=sys.stderr)
        return []
    findings = []
    for r in data.get("runs", []):
        for result in r.get("results", []):
            rule_id = result.get("ruleId", "")
            msg     = (result.get("message") or {}).get("text", "")
            for loc in result.get("locations", []):
                phys = (loc.get("physicalLocation") or {})
                uri  = (phys.get("artifactLocation") or {}).get("uri", "")
                line = (phys.get("region") or {}).get("startLine", 0)
                findings.append({"rule": rule_id, "file": uri, "line": line, "msg": msg})
    return findings


def plan_fixes(findings):
    """
    Returns:
      semgrepignore_paths: set of paths to add to .semgrepignore
      inline_fixes: list of {file, line, rule}
      skip: list of findings we can't auto-fix
    """
    semgrepignore_paths = set()
    inline_fixes = []
    skip = []

    for f in findings:
        path = f["file"]
        if path.startswith("tests/") or path.startswith("test/"):
            top = path.split("/")[0] + "/"
            semgrepignore_paths.add(top)
        elif Path(path).suffix in _COMMENT_FMT:
            inline_fixes.append(f)
        else:
            skip.append(f)

    return semgrepignore_paths, inline_fixes, skip


def _in_repo(repo_path, target):
    """target が repo_path の配下にあるか確認する（パストラバーサル防止）。"""
    try:
        target.resolve().relative_to(repo_path.resolve())
        return True
    except ValueError:
        return False


def apply_semgrepignore(repo_path, paths):
    """paths を .semgrepignore に追記する（重複スキップ）。"""
    target = repo_path / ".semgrepignore"
    if not _in_repo(repo_path, target):
        return []
    content = target.read_text(encoding="utf-8") if target.exists() else ""
    existing_lines = set(content.splitlines())
    added = []
    for p in sorted(paths):
        if p not in existing_lines:
            content = content.rstrip("\r\n") + f"\n{p}"
            added.append(p)
    if added:
        # strip() は末尾のみにして先頭の空白行を保持する
        target.write_text(content.rstrip("\r\n") + "\n", encoding="utf-8")
    return added


def apply_nosemgrep(repo_path, fixes):
    """各行末に nosemgrep コメントを付与する（同一行複数ルールは結合）。"""
    by_location: dict = defaultdict(list)
    for fix in fixes:
        by_location[(fix["file"], fix["line"])].append(fix["rule"])

    changed_files = set()
    for (filepath, lineno), rules in by_location.items():
        target = repo_path / filepath
        if not _in_repo(repo_path, target):
            print(f"  SKIP (path traversal): {filepath}", file=sys.stderr)
            continue
        if not target.exists():
            continue

        fmt = _COMMENT_FMT.get(target.suffix)
        if not fmt:
            continue

        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        idx = lineno - 1
        if idx < 0 or idx >= len(lines):
            continue

        # ruleId に改行・制御文字が含まれていても安全にする（コード注入防止）
        safe_rules = ", ".join(
            re.sub(r"[\r\n\x00-\x1f]", "", r) for r in rules
        )

        existing = lines[idx]
        if "nosemgrep" in existing:
            # 既存コメントに追記
            lines[idx] = existing.rstrip("\r\n") + f", {safe_rules}\n"
        else:
            lines[idx] = existing.rstrip("\r\n") + fmt.format(rule=safe_rules) + "\n"

        target.write_text("".join(lines), encoding="utf-8")
        changed_files.add(filepath)
    return changed_files


def ensure_label(repo):
    """RESCUE_LABEL が無ければ作成する。"""
    try:
        gh(
            "label", "create", RESCUE_LABEL,
            "--repo", f"{GITHUB_OWNER}/{repo}",
            "--color", "e4e669",
            "--description", "Opened by vauban-rescue agent",
        )
    except subprocess.CalledProcessError as e:
        if "already exists" not in e.stderr and "already_exists" not in e.stderr:
            raise


def open_pr(repo, fixed_findings):
    lines = "\n".join(
        f"- `{f['file']}:{f['line']}` [{f['rule']}]" for f in fixed_findings
    )
    body = (
        "## Semgrep findings\n\n"
        f"{lines}\n\n"
        "---\n"
        "_Auto-generated by [vauban](https://github.com/mediiiiium/vauban) rescue agent_"
    )
    ensure_label(repo)
    gh(
        "pr", "create",
        "--repo", f"{GITHUB_OWNER}/{repo}",
        "--title", "fix: suppress semgrep findings (vauban-rescue)",
        "--body", body,
        "--label", RESCUE_LABEL,
        "--head", RESCUE_BRANCH,
    )


def rescue_repo(repo):
    print(f"\n=== {repo} ===")

    run_id = latest_semgrep_failure(repo)
    if not run_id:
        print("  semgrep: OK (no failures)")
        return

    if rescue_pr_exists(repo):
        print("  rescue PR already exists — skipping")
        return

    with tempfile.TemporaryDirectory() as sarif_dir:
        sarif_path = download_sarif(repo, run_id, sarif_dir)
        if not sarif_path:
            print("  SARIF not found (artifact may have expired)")
            return

        findings = parse_findings(sarif_path)
        if not findings:
            print("  no findings in SARIF")
            return

        print(f"  {len(findings)} finding(s) detected")
        semgrepignore_paths, inline_fixes, skipped = plan_fixes(findings)
        if skipped:
            print(f"  skipped (unsupported file type): {len(skipped)} finding(s)")

    with tempfile.TemporaryDirectory() as clone_dir:
        # PAT をコマンドライン引数に含めないため gh auth setup-git で認証を設定する
        run(["gh", "auth", "setup-git"], env=_gh_env())
        plain_url = f"https://github.com/{GITHUB_OWNER}/{repo}.git"
        try:
            run(["git", "clone", plain_url, clone_dir])
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git clone failed: {e.stderr.strip()}") from None

        # -B: ローカルブランチが既にあれば reset して再利用
        run(["git", "checkout", "-B", RESCUE_BRANCH], cwd=clone_dir)

        repo_path = Path(clone_dir)
        changed = False
        fixed_findings = []

        added_paths = apply_semgrepignore(repo_path, semgrepignore_paths)
        if added_paths:
            print(f"  .semgrepignore: added {added_paths}")
            changed = True
            added_set = set(added_paths)
            fixed_findings += [
                f for f in findings
                if (f["file"].split("/")[0] + "/") in added_set
            ]

        changed_files = apply_nosemgrep(repo_path, inline_fixes)
        if changed_files:
            print(f"  nosemgrep comments: {sorted(changed_files)}")
            changed = True
            fixed_findings += [f for f in inline_fixes if f["file"] in changed_files]

        if not changed:
            print("  nothing to fix automatically")
            return

        git_id = [
            "-c", "user.name=vauban-rescue",
            "-c", "user.email=vauban-rescue@users.noreply.github.com",
        ]
        run(["git"] + git_id + ["add", "-A"], cwd=clone_dir)
        run(["git"] + git_id + ["commit", "-m",
             "fix: suppress semgrep findings [vauban-rescue]"], cwd=clone_dir)

        try:
            run(["git", "push", "--force-with-lease", "origin",
                 f"HEAD:{RESCUE_BRANCH}"], cwd=clone_dir)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"git push failed: {e.stderr.strip()}") from None

        open_pr(repo, fixed_findings)
        print("  PR opened ✓")


# ── main ──────────────────────────────────────────────────────────────────────

def load_repos() -> list:
    """対象リポジトリの一覧は個人運用の内部情報のためソースに含めない。
    環境変数 VAUBAN_REPOS（JSON配列 [["repo","ecosystem"], ...]）から読む。"""
    raw = os.environ.get("VAUBAN_REPOS")
    if not raw:
        print("ERROR: VAUBAN_REPOS not set (JSON array of [repo, ecosystem] pairs)", file=sys.stderr)
        sys.exit(1)
    try:
        return [(repo, eco) for repo, eco in json.loads(raw)]
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"ERROR: invalid VAUBAN_REPOS: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    if "GH_PAT" not in os.environ:
        print("ERROR: GH_PAT not set", file=sys.stderr)
        sys.exit(1)

    for repo, _ in load_repos():
        try:
            rescue_repo(repo)
        except subprocess.CalledProcessError as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            if e.stderr:
                print(f"  stderr: {e.stderr.strip()}", file=sys.stderr)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)

    print("\ndone.")


if __name__ == "__main__":
    main()
