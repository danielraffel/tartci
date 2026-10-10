#!/usr/bin/env python3
"""Live TartCI rename probes. Read-only unless --create-issue is explicit."""
from __future__ import annotations
import argparse, json, os, subprocess
from dataclasses import asdict, dataclass

OLD = "danielraffel/tartci"
NEW = "Generous-Corp/tartci"
CLASSES = ("git_https", "git_ssh", "api", "raw", "issue")

@dataclass(frozen=True)
class Receipt:
    probe: str
    slug: str
    result: int
    follow_result: int | None = None
    detail: str = ""

def classify(row: Receipt) -> str:
    if row.slug == NEW and row.result == 404: return "missing"
    if row.slug == OLD and row.probe == "api" and row.result != 0: return "live_pin"
    if row.slug == OLD and row.result == 301: return "live_pin"
    if row.slug == NEW and row.result in (200, 201): return "new_ok"
    if row.slug == OLD and row.result in (0, 200, 201): return "old_ok"
    return "unexpected"

def command(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr).strip()[-500:]

def curl(url: str) -> tuple[int, int, str]:
    args = ["/usr/bin/curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}"]
    rc1, text1 = command([*args, url]); rc2, text2 = command([*args, "-L", url])
    def code(rc: int, text: str) -> int:
        try: return int(text[-3:])
        except ValueError: return rc
    return code(rc1, text1), code(rc2, text2), text1

def probe_one(name: str, slug: str, issue: "IssueTransport") -> Receipt:
    if name == "git_https":
        rc, detail = command(["git", "ls-remote", f"https://github.com/{slug}.git", "HEAD"]); return Receipt(name, slug, rc, detail=detail)
    if name == "git_ssh":
        rc, detail = command(["git", "ls-remote", f"git@github.com:{slug}.git", "HEAD"]); return Receipt(name, slug, rc, detail=detail)
    if name == "api":
        env = {**os.environ, "GH_REPO": slug, "SHIPYARD_GH_APP_REPO": slug}
        rc, detail = command(["ghapp", "api", f"repos/{slug}"], env); return Receipt(name, slug, 200 if rc == 0 else rc, detail=detail)
    endpoint = f"https://raw.githubusercontent.com/{slug}/main/README.md" if name == "raw" else f"https://api.github.com/repos/{slug}/issues"
    nofollow, followed, detail = curl(endpoint)
    return Receipt(name, slug, nofollow, follow_result=followed, detail=detail)

def probe(issue: "IssueTransport | None" = None, create_issue: bool = False, transport=None) -> list[Receipt]:
    issue = issue or IssueTransport()
    rows = [transport(name, slug) if transport else probe_one(name, slug, issue)
            for name in CLASSES for slug in (OLD, NEW)]
    if create_issue:
        number = issue.create(NEW); issue.close(NEW, number)
        rows.append(Receipt("issue-create-close", NEW, 201, detail=str(number)))
    return rows

class IssueTransport:
    def create(self, slug: str) -> int:
        rc, text = command(["ghapp", "api", f"repos/{slug}/issues", "--method", "POST", "-f", "title=tartci redirect probe (disposable)", "-f", "body=automated probe; close immediately"], {**os.environ, "GH_REPO": slug})
        if rc: raise RuntimeError(f"issue create failed: {text}")
        return int(json.loads(text)["number"])
    def close(self, slug: str, number: int) -> None:
        rc, text = command(["ghapp", "api", f"repos/{slug}/issues/{number}", "--method", "PATCH", "-f", "state=closed"], {**os.environ, "GH_REPO": slug})
        if rc: raise RuntimeError(f"issue close failed: {text}")

def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--live", action="store_true"); p.add_argument("--create-issue", action="store_true"); p.add_argument("--json", action="store_true"); args = p.parse_args()
    if not args.live: p.error("--live is required")
    rows = [{**asdict(r), "classification": classify(r)} for r in probe(create_issue=args.create_issue)]
    if args.json: print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print("probe\tslug\tresult\tfollow_result\tclassification\tdetail")
        for r in rows: print("\t".join(str(r[k]).replace("\n", " ") for k in ("probe","slug","result","follow_result","classification","detail")))
    return 0
if __name__ == "__main__": raise SystemExit(main())
