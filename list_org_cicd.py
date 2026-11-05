#!/usr/bin/env python3
"""Detect which CI/CD tools each repository in a GitHub organization uses.

Detection is based on files in the default branch (one recursive git-tree call per repo):
  - GitHub Actions: .github/workflows/*.yml|*.yaml
  - Jenkins:        Jenkinsfile, Jenkinsfile.*, *.jenkinsfile
  - Harness:        anything under a .harness/ directory, or *harness*.yml|*.yaml files,
                    or a repository webhook pointing to *.harness.io (one extra call per repo)
Repositories with none of the above are reported as "Not detected".

Auth: same as list_org_secrets.py (needs `repo` scope / "Contents: read").
Webhook detection needs repo admin access (`admin:repo_hook` scope / "Webhooks: read");
without it the webhook check is skipped and noted in the report.

Usage:
  python3 list_org_cicd.py my-org
  python3 list_org_cicd.py my-org --skip-archived --skip-forks
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.parse

from list_org_secrets import ApiError, GitHub, get_token, log, md

TOOLS = ["GitHub Actions", "Jenkins", "Harness"]
NOT_DETECTED = "Not detected"

DETECTORS = {
    "GitHub Actions": lambda p: re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", p) is not None,
    "Jenkins": lambda p: re.fullmatch(r"(?i)(jenkinsfile(\..+)?|.+\.jenkinsfile)", p.rsplit("/", 1)[-1]) is not None,
    "Harness": lambda p: p.startswith(".harness/") or "/.harness/" in p
    or re.fullmatch(r"(?i).*harness.*\.ya?ml", p.rsplit("/", 1)[-1]) is not None,
}

# Fallback paths checked when the git tree is too large and GitHub truncates it
FALLBACK_PATHS = {
    "GitHub Actions": ".github/workflows",
    "Jenkins": "Jenkinsfile",
    "Harness": ".harness",
}


def harness_webhooks(gh, full):
    """Return (list of Harness webhook URLs without query string, error note)."""
    try:
        hooks = list(gh.paginate(f"/repos/{full}/hooks"))
    except ApiError as e:
        return [], f"webhooks: HTTP {e.status}"
    found = []
    for h in hooks:
        url = (h.get("config") or {}).get("url", "")
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if host == "harness.io" or host.endswith(".harness.io"):
            # query string can carry account ids / tokens, keep it out of the report
            state = "" if h.get("active", True) else " (inactive)"
            found.append(f"webhook: {parsed.scheme}://{parsed.hostname}{parsed.path}{state}")
    return found, ""


def detect_from_paths(paths):
    found = {t: [] for t in TOOLS}
    for p in paths:
        for tool, match in DETECTORS.items():
            if match(p):
                found[tool].append(p)
    return found


def fallback_detect(gh, full, branch):
    found = {t: [] for t in TOOLS}
    ref = urllib.parse.quote(branch, safe="")
    for tool, path in FALLBACK_PATHS.items():
        status, body, _ = gh.request(f"/repos/{full}/contents/{path}?ref={ref}")
        if status != 200:
            continue
        if isinstance(body, list):
            found[tool] = [e["path"] for e in body if e.get("type") == "file" and DETECTORS[tool](e["path"])]
        else:
            found[tool] = [body["path"]]
    return found


def scan_repo(gh, repo, check_webhooks):
    full = repo["full_name"]
    branch = repo.get("default_branch") or "main"
    info = {"repository": full, "default_branch": branch, "archived": repo.get("archived", False),
            "fork": repo.get("fork", False), "note": ""}

    status, body, _ = gh.request(
        f"/repos/{full}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1")
    if status == 409:
        found, info["note"] = {t: [] for t in TOOLS}, "empty repository"
    elif status != 200:
        found, info["note"] = {t: [] for t in TOOLS}, f"tree: HTTP {status}"
    else:
        found = detect_from_paths(e["path"] for e in body.get("tree", []) if e.get("type") == "blob")
        if body.get("truncated"):
            info["note"] = "tree truncated, used fallback checks"
            for tool, files in fallback_detect(gh, full, branch).items():
                found[tool] = sorted(set(found[tool]) | set(files))

    if check_webhooks:
        hooks, err = harness_webhooks(gh, full)
        found["Harness"] += hooks
        if err:
            info["note"] = "; ".join(filter(None, [info["note"], err]))

    detected = [t for t in TOOLS if found[t]]
    info["cicd"] = ", ".join(detected) if detected else NOT_DETECTED
    for t in TOOLS:
        info[t] = found[t]
    return info


def write_reports(org, results, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_dir, f"{org}-cicd-{stamp}")

    with open(f"{base}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["repository", "default_branch", "archived", "fork", "cicd"]
                   + [f"{t} files" for t in TOOLS] + ["note"])
        for r in results:
            w.writerow([r["repository"], r["default_branch"], r["archived"], r["fork"], r["cicd"]]
                       + ["; ".join(r[t]) for t in TOOLS] + [r["note"]])

    with open(f"{base}.json", "w") as fh:
        json.dump({"organization": org, "generated_at": dt.datetime.now().isoformat(),
                   "repositories": results}, fh, indent=2)

    def count(tool):
        return sum(1 for r in results if r[tool])

    with open(f"{base}.md", "w") as fh:
        fh.write(f"# CI/CD report for `{org}`\n\n")
        fh.write(f"Generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
        fh.write("| Tool | # Repos |\n|---|---|\n")
        fh.write(f"| Repositories scanned | {len(results)} |\n")
        for t in TOOLS:
            fh.write(f"| {t} | {count(t)} |\n")
        fh.write(f"| Multiple tools | {sum(1 for r in results if ',' in r['cicd'])} |\n")
        fh.write(f"| {NOT_DETECTED} | {sum(1 for r in results if r['cicd'] == NOT_DETECTED)} |\n\n")

        fh.write("## Per repository\n\n")
        fh.write("| Repository | CI/CD | " + " | ".join(TOOLS) + " | Note |\n")
        fh.write("|---|---|" + "---|" * len(TOOLS) + "---|\n")
        for r in sorted(results, key=lambda r: r["repository"].lower()):
            cells = [md("<br>".join(r[t])) if r[t] else "-" for t in TOOLS]
            archived = " (archived)" if r["archived"] else ""
            fh.write(f"| {r['repository']}{archived} | {r['cicd']} | " + " | ".join(cells)
                     + f" | {r['note']} |\n")

    return base


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("org", help="GitHub organization login")
    p.add_argument("--output-dir", default="reports")
    p.add_argument("--skip-archived", action="store_true")
    p.add_argument("--skip-forks", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="Process at most N repositories (0 = all)")
    p.add_argument("--no-webhooks", action="store_true",
                   help="Skip the Harness webhook check (saves one API call per repo)")
    p.add_argument("--min-interval", type=float, default=0.1,
                   help="Minimum seconds between API requests (default 0.1)")
    p.add_argument("--reserve", type=int, default=50,
                   help="Wait for rate-limit reset when remaining requests fall below this (default 50)")
    args = p.parse_args()

    gh = GitHub(get_token(), min_interval=args.min_interval, reserve=args.reserve)

    log(f"Listing repositories in {args.org}...")
    try:
        repos = list(gh.paginate(f"/orgs/{args.org}/repos?type=all"))
    except ApiError as e:
        sys.exit(f"Failed to list repositories: {e}")
    if args.skip_archived:
        repos = [r for r in repos if not r.get("archived")]
    if args.skip_forks:
        repos = [r for r in repos if not r.get("fork")]
    if args.limit:
        repos = repos[:args.limit]
    log(f"{len(repos)} repositories to scan")

    results = []
    try:
        for i, repo in enumerate(repos, 1):
            info = scan_repo(gh, repo, not args.no_webhooks)
            results.append(info)
            log(f"[{i}/{len(repos)}] {repo['full_name']}: {info['cicd']}"
                + (f" ({info['note']})" if info["note"] else ""))
    except KeyboardInterrupt:
        log("Interrupted, writing partial report...")

    base = write_reports(args.org, results, args.output_dir)
    log(f"Done. {gh.requests_made} API requests made.")
    log(f"Reports: {base}.md, {base}.csv, {base}.json")


if __name__ == "__main__":
    main()
