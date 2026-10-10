#!/usr/bin/env python3
"""Probe TartCI rename redirects with real transports and emit receipt rows."""
from __future__ import annotations
import argparse, json, subprocess, sys
from dataclasses import asdict, dataclass

OLD = "danielraffel/tartci"
NEW = "Generous-Corp/tartci"
CLASSES = ("git_https", "git_ssh", "api", "raw", "issue")

@dataclass(frozen=True)
class Receipt:
    probe: str
    slug: str
    result: int
    detail: str = ""


def classify(row: Receipt) -> str:
    if row.slug == NEW and row.result == 404:
        return "missing"
    if row.slug == OLD and row.result == 301:
        return "live_pin"
    if row.slug == NEW and row.result in (200, 201):
        return "new_ok"
    if row.slug == OLD and row.result in (0, 200, 201):
        return "old_ok"
    return "unexpected"


def run(argv: list[str]) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()[-500:]


def http(url: str) -> tuple[int, str]:
    code, detail = run(["/usr/bin/curl", "-sS", "-o", "/dev/null", "-D", "-",
                        "-w", "%{http_code}", url])
    try:
        return int(detail[-3:]), detail
    except ValueError:
        return code, detail


def probe_one(name: str, slug: str, create_issue: bool = False) -> Receipt:
    if name == "git_https":
        result, detail = run(["git", "ls-remote", f"https://github.com/{slug}.git", "HEAD"])
        return Receipt(name, slug, result, detail)
    if name == "git_ssh":
        result, detail = run(["git", "ls-remote", f"git@github.com:{slug}.git", "HEAD"])
        return Receipt(name, slug, result, detail)
    if name == "api":
        result, detail = http(f"https://api.github.com/repos/{slug}")
        return Receipt(name, slug, result, detail)
    if name == "raw":
        result, detail = http(f"https://raw.githubusercontent.com/{slug}/main/README.md")
        return Receipt(name, slug, result, detail)
    if create_issue:
        result, detail = run(["ghapp", "api", f"repos/{slug}/issues", "--method", "POST",
                              "-f", "title=tartci redirect probe (disposable)",
                              "-f", "body=automated probe; close immediately"])
        return Receipt(name, slug, 201 if result == 0 else result, detail)
    result, detail = http(f"https://api.github.com/repos/{slug}/issues")
    return Receipt(name, slug, result, detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="perform real GitHub probes")
    parser.add_argument("--create-issue", action="store_true", help="create disposable issue probes")
    parser.add_argument("--json", action="store_true", help="emit JSON receipt rows")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this command never silently uses fixtures")
    rows = [probe_one(name, slug, args.create_issue) for name in CLASSES for slug in (OLD, NEW)]
    enriched = [{**asdict(row), "classification": classify(row)} for row in rows]
    if args.json:
        print(json.dumps(enriched, indent=2, sort_keys=True))
    else:
        print("probe\tslug\tresult\tclassification\tdetail")
        for row in enriched:
            print("\t".join(str(row[key]).replace("\n", " ") for key in
                              ("probe", "slug", "result", "classification", "detail")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
