#!/usr/bin/env python3
"""List GitHub Packages (npm, NuGet, Maven, RubyGems, Docker, container images) per repository in an org.

Packages are listed at the org level (one paginated call per package type) and grouped by the
repository they are linked to, which is far cheaper than querying each repository.

Auth: same as list_org_secrets.py. The token needs the `read:packages` scope:
  gh auth refresh -h github.com -s read:packages

Usage:
  python3 list_org_packages.py my-org
  python3 list_org_packages.py my-org --types npm container --with-versions
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.parse

from list_org_secrets import ApiError, GitHub, get_token, log, md

PACKAGE_TYPES = ["npm", "maven", "rubygems", "docker", "nuget", "container"]
NO_REPO = "(not linked to a repository)"


def latest_version(gh, org, pkg):
    name = urllib.parse.quote(pkg["name"], safe="")
    status, body, _ = gh.request(
        f"/orgs/{org}/packages/{pkg['package_type']}/{name}/versions?per_page=1")
    if status != 200 or not body:
        return "", "", f"versions: HTTP {status}"
    v = body[0]
    tags = ((v.get("metadata") or {}).get("container") or {}).get("tags") or []
    # container versions are digests; tags are the human-readable names
    version = ", ".join(tags) if tags else v.get("name", "")
    return version, v.get("updated_at", ""), ""


def write_reports(org, rows, errors, all_repos, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_dir, f"{org}-packages-{stamp}")
    fields = ["repository", "package_type", "name", "visibility", "version_count",
              "latest_version", "latest_updated_at", "created_at", "updated_at", "url"]

    with open(f"{base}.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    by_repo = {}
    for r in rows:
        by_repo.setdefault(r["repository"], []).append(r)

    with open(f"{base}.json", "w") as fh:
        json.dump({"organization": org, "generated_at": dt.datetime.now().isoformat(),
                   "errors": errors, "repositories": by_repo}, fh, indent=2)

    by_type = {}
    for r in rows:
        by_type[r["package_type"]] = by_type.get(r["package_type"], 0) + 1

    with open(f"{base}.md", "w") as fh:
        fh.write(f"# Packages report for `{org}`\n\n")
        fh.write(f"Generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
        fh.write(f"- Total packages: {len(rows)}\n")
        fh.write(f"- Repositories with packages: {len([k for k in by_repo if k != NO_REPO])}\n")
        fh.write(f"- Packages not linked to a repository: {len(by_repo.get(NO_REPO, []))}\n\n")

        fh.write("## Packages by type\n\n| Type | Count |\n|---|---|\n")
        for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
            fh.write(f"| {t} | {n} |\n")

        fh.write("\n## Per repository\n")
        for repo in sorted(by_repo, key=lambda k: (k == NO_REPO, k.lower())):
            fh.write(f"\n### {repo}\n\n")
            fh.write("| Type | Package | Visibility | Versions | Latest | Updated |\n"
                     "|---|---|---|---|---|---|\n")
            for p in sorted(by_repo[repo], key=lambda p: (p["package_type"], p["name"])):
                fh.write(f"| {p['package_type']} | [{md(p['name'])}]({p['url']}) | {p['visibility']} "
                         f"| {p['version_count']} | {md(p['latest_version'])} | {p['updated_at'][:10]} |\n")

        if all_repos is not None:
            empty = sorted(set(all_repos) - set(by_repo), key=str.lower)
            fh.write(f"\n## Repositories without packages ({len(empty)})\n\n")
            fh.write("".join(f"- {r}\n" for r in empty))

        if errors:
            fh.write("\n## Errors\n\n" + "".join(f"- {e}\n" for e in errors))

    return base


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("org", help="GitHub organization login")
    p.add_argument("--output-dir", default="reports")
    p.add_argument("--types", nargs="+", choices=PACKAGE_TYPES, default=PACKAGE_TYPES,
                   help="Package types to list (default: all)")
    p.add_argument("--with-versions", action="store_true",
                   help="Fetch the latest version/tags of each package (1 extra API call per package)")
    p.add_argument("--include-empty-repos", action="store_true",
                   help="Also list org repositories that have no packages")
    p.add_argument("--min-interval", type=float, default=0.1,
                   help="Minimum seconds between API requests (default 0.1)")
    p.add_argument("--reserve", type=int, default=50,
                   help="Wait for rate-limit reset when remaining requests fall below this (default 50)")
    args = p.parse_args()

    gh = GitHub(get_token(), min_interval=args.min_interval, reserve=args.reserve)
    rows, errors = [], []

    try:
        for ptype in args.types:
            try:
                packages = list(gh.paginate(f"/orgs/{args.org}/packages?package_type={ptype}"))
            except ApiError as e:
                if e.status in (401, 403):
                    sys.exit(f"Access denied listing packages ({e}). "
                             "Run: gh auth refresh -h github.com -s read:packages")
                errors.append(f"{ptype}: {e}")
                log(f"{ptype}: {e}")
                continue
            log(f"{ptype}: {len(packages)} packages")

            for i, pkg in enumerate(packages, 1):
                row = {
                    "repository": (pkg.get("repository") or {}).get("full_name") or NO_REPO,
                    "package_type": pkg["package_type"],
                    "name": pkg["name"],
                    "visibility": pkg.get("visibility", ""),
                    "version_count": pkg.get("version_count", ""),
                    "latest_version": "",
                    "latest_updated_at": "",
                    "created_at": pkg.get("created_at", ""),
                    "updated_at": pkg.get("updated_at", ""),
                    "url": pkg.get("html_url", ""),
                }
                if args.with_versions:
                    row["latest_version"], row["latest_updated_at"], err = latest_version(gh, args.org, pkg)
                    if err:
                        errors.append(f"{ptype}/{pkg['name']}: {err}")
                    if i % 50 == 0:
                        log(f"  {ptype}: {i}/{len(packages)} versions fetched")
                rows.append(row)
    except KeyboardInterrupt:
        log("Interrupted, writing partial report...")

    all_repos = None
    if args.include_empty_repos:
        all_repos = [r["full_name"] for r in gh.paginate(f"/orgs/{args.org}/repos?type=all")]

    base = write_reports(args.org, rows, errors, all_repos, args.output_dir)
    log(f"Done. {len(rows)} packages, {gh.requests_made} API requests made.")
    log(f"Reports: {base}.md, {base}.csv, {base}.json")


if __name__ == "__main__":
    main()
