#!/usr/bin/env python3
"""List secrets and variables used by every repository in a GitHub organization and produce a report.

Collects, per repository:
  - Actions repository secrets and variables
  - Actions organization secrets and variables shared with the repository
  - Actions environment secrets and variables (per environment)
  - Dependabot repository secrets
  - secrets.* / vars.* referenced in workflow files (optional, --scan-workflows)

Only secret NAMES are retrieved; GitHub never exposes secret values via the API.
Variable values are included unless --hide-variable-values is set.

Auth: GITHUB_TOKEN / GH_TOKEN env var, or falls back to `gh auth token`.
Token needs `repo` + `read:org` scopes (classic) or "Secrets: read", "Variables: read",
"Environments: read", "Dependabot secrets: read", "Contents: read" (fine-grained).

Usage:
  python3 list_org_secrets.py my-org
  python3 list_org_secrets.py my-org --scan-workflows --skip-archived --output-dir reports
"""

import argparse
import base64
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REF_RES = {
    "secret": re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)"),
    "variable": re.compile(r"vars\.([A-Za-z_][A-Za-z0-9_]*)"),
}


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def get_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("No token found. Set GITHUB_TOKEN or log in with `gh auth login`.")


class GitHub:
    """Minimal GitHub REST client with primary and secondary rate-limit handling."""

    def __init__(self, token, min_interval=0.1, reserve=50, max_retries=6):
        self.token = token
        self.min_interval = min_interval  # pause between requests to avoid secondary limits
        self.reserve = reserve  # stop and wait when remaining quota drops below this
        self.max_retries = max_retries
        self._last_request = 0.0
        self.requests_made = 0

    def _throttle(self):
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _sleep_until(reset_epoch, reason):
        wait = max(0, int(reset_epoch) - int(time.time())) + 5
        log(f"{reason}: sleeping {wait}s until {dt.datetime.fromtimestamp(int(reset_epoch)):%H:%M:%S}")
        time.sleep(wait)

    def request(self, url):
        """Return (status, json_body, headers). 404/403-permission errors are returned, not raised."""
        if not url.startswith("http"):
            url = API + url
        backoff = 60
        for attempt in range(self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "org-secrets-report",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.requests_made += 1
                    headers = resp.headers
                    body = json.loads(resp.read() or b"null")
                    self._check_quota(headers)
                    return resp.status, body, headers
            except urllib.error.HTTPError as e:
                self.requests_made += 1
                headers = e.headers
                raw = e.read()
                try:
                    body = json.loads(raw or b"null")
                except ValueError:
                    body = {"message": raw.decode(errors="replace")}
                message = (body or {}).get("message", "") if isinstance(body, dict) else ""

                if e.code in (403, 429):
                    retry_after = headers.get("Retry-After")
                    remaining = headers.get("X-RateLimit-Remaining")
                    if retry_after:
                        log(f"Secondary rate limit hit, retrying after {retry_after}s")
                        time.sleep(int(retry_after) + 1)
                        continue
                    if remaining == "0":
                        self._sleep_until(headers.get("X-RateLimit-Reset", time.time() + 60), "Primary rate limit exhausted")
                        continue
                    if "rate limit" in message.lower():
                        log(f"Secondary rate limit hit, backing off {backoff}s")
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 900)
                        continue
                    return e.code, body, headers  # permission problem
                if e.code >= 500 and attempt < self.max_retries:
                    log(f"HTTP {e.code} on {url}, retrying in {2 ** attempt}s")
                    time.sleep(2 ** attempt)
                    continue
                return e.code, body, headers
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < self.max_retries:
                    log(f"Network error ({e}), retrying in {2 ** attempt}s")
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError(f"Giving up on {url} after {self.max_retries} retries")

    def _check_quota(self, headers):
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None and reset is not None and int(remaining) < self.reserve:
            self._sleep_until(reset, f"Only {remaining} requests left")

    def paginate(self, path, key=None):
        """Yield items across all pages. Returns early with error dict on non-200."""
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}per_page=100"
        while url:
            status, body, headers = self.request(url)
            if status != 200:
                raise ApiError(status, (body or {}).get("message", "") if isinstance(body, dict) else "")
            items = body.get(key, []) if key else body
            yield from items
            url = _next_link(headers.get("Link"))


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _next_link(link_header):
    if not link_header:
        return None
    for part in link_header.split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip()[1:-1]
    return None


def safe_list(gh, path, key, errors, label):
    try:
        return list(gh.paginate(path, key))
    except ApiError as e:
        # 404 = feature disabled / no access; record it but keep going
        errors.append(f"{label}: {e}")
        return []


def scan_workflows(gh, full_name, errors):
    """Return {(type, name): [workflow files referencing it]}."""
    refs = {}
    status, body, _ = gh.request(f"/repos/{full_name}/contents/.github/workflows")
    if status == 404:
        return refs
    if status != 200 or not isinstance(body, list):
        errors.append(f"workflows: HTTP {status}")
        return refs
    for entry in body:
        if entry.get("type") != "file" or not entry["name"].endswith((".yml", ".yaml")):
            continue
        st, f, _ = gh.request(f"/repos/{full_name}/contents/{urllib.parse.quote(entry['path'])}")
        if st != 200 or "content" not in f:
            errors.append(f"workflow {entry['path']}: HTTP {st}")
            continue
        text = base64.b64decode(f["content"]).decode(errors="replace")
        for kind, regex in REF_RES.items():
            for name in set(regex.findall(text)):
                refs.setdefault((kind, name), []).append(entry["path"])
    return refs


def collect_repo(gh, repo, scan_wf, hide_values):
    full = repo["full_name"]
    errors = []
    rows = []

    def add(kind, scope, name, source="", extra="", value=""):
        rows.append({"repository": full, "type": kind, "scope": scope, "name": name,
                     "value": "" if hide_values else value, "source": source, "detail": extra})

    for s in safe_list(gh, f"/repos/{full}/actions/secrets", "secrets", errors, "actions secrets"):
        add("secret", "actions-repo", s["name"], "repository", f"updated {s.get('updated_at', '')}")

    for s in safe_list(gh, f"/repos/{full}/actions/organization-secrets", "secrets", errors, "org secrets"):
        add("secret", "actions-org", s["name"], "organization", f"visibility {s.get('visibility', '')}")

    for v in safe_list(gh, f"/repos/{full}/actions/variables", "variables", errors, "actions variables"):
        add("variable", "actions-repo", v["name"], "repository", f"updated {v.get('updated_at', '')}",
            v.get("value", ""))

    for v in safe_list(gh, f"/repos/{full}/actions/organization-variables", "variables", errors, "org variables"):
        add("variable", "actions-org", v["name"], "organization", f"visibility {v.get('visibility', '')}",
            v.get("value", ""))

    for env in safe_list(gh, f"/repos/{full}/environments", "environments", errors, "environments"):
        env_name = urllib.parse.quote(env["name"], safe="")
        source = f"environment:{env['name']}"
        for s in safe_list(gh, f"/repos/{full}/environments/{env_name}/secrets", "secrets",
                           errors, f"env:{env['name']} secrets"):
            add("secret", "environment", s["name"], source, f"updated {s.get('updated_at', '')}")
        for v in safe_list(gh, f"/repos/{full}/environments/{env_name}/variables", "variables",
                           errors, f"env:{env['name']} variables"):
            add("variable", "environment", v["name"], source, f"updated {v.get('updated_at', '')}",
                v.get("value", ""))

    for s in safe_list(gh, f"/repos/{full}/dependabot/secrets", "secrets", errors, "dependabot"):
        add("secret", "dependabot", s["name"], "repository", f"updated {s.get('updated_at', '')}")

    if scan_wf:
        defined = {(r["type"], r["name"]) for r in rows}
        for (kind, name), files in sorted(scan_workflows(gh, full, errors).items()):
            found = (kind, name) in defined or name == "GITHUB_TOKEN"
            add(kind, "workflow-reference", name, ", ".join(files), "defined" if found else "NOT FOUND")

    return rows, errors


def md(text):
    return str(text).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def write_reports(org, repos_info, rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(out_dir, f"{org}-secrets-{stamp}")

    with open(f"{base}.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["repository", "type", "scope", "name", "value", "source", "detail"])
        w.writeheader()
        w.writerows(rows)

    with open(f"{base}.json", "w") as fh:
        json.dump({"organization": org, "generated_at": dt.datetime.now().isoformat(),
                   "repositories": repos_info}, fh, indent=2)

    usage = {}
    for r in rows:
        if r["scope"] != "workflow-reference":
            usage.setdefault((r["type"], r["scope"], r["name"]), set()).add(r["repository"])

    def count_repos(kind):
        return sum(1 for i in repos_info if any(r["type"] == kind for r in i["items"]))

    with open(f"{base}.md", "w") as fh:
        fh.write(f"# Secrets and variables report for `{org}`\n\n")
        fh.write(f"Generated: {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
        fh.write(f"- Repositories scanned: {len(repos_info)}\n")
        fh.write(f"- Repositories with secrets: {count_repos('secret')}\n")
        fh.write(f"- Repositories with variables: {count_repos('variable')}\n")
        fh.write(f"- Distinct secrets: {sum(1 for k in usage if k[0] == 'secret')}\n")
        fh.write(f"- Distinct variables: {sum(1 for k in usage if k[0] == 'variable')}\n")
        fh.write(f"- Repositories with errors: {sum(1 for r in repos_info if r['errors'])}\n\n")

        for kind, title in (("secret", "Secrets"), ("variable", "Variables")):
            fh.write(f"## {title} by usage\n\n| Name | Scope | # Repos |\n|---|---|---|\n")
            items = [(k, v) for k, v in usage.items() if k[0] == kind]
            for (_, scope, name), repos in sorted(items, key=lambda kv: (-len(kv[1]), kv[0])):
                fh.write(f"| `{name}` | {scope} | {len(repos)} |\n")
            fh.write("\n")

        fh.write("## Per repository\n")
        for info in repos_info:
            if not info["items"] and not info["errors"]:
                continue
            fh.write(f"\n### {info['repository']}\n\n")
            if info["items"]:
                fh.write("| Type | Name | Scope | Value | Source | Detail |\n|---|---|---|---|---|---|\n")
                for s in info["items"]:
                    fh.write(f"| {s['type']} | `{s['name']}` | {s['scope']} | {md(s['value'])} "
                             f"| {md(s['source'])} | {s['detail']} |\n")
            if info["errors"]:
                fh.write("\nErrors:\n" + "".join(f"- {e}\n" for e in info["errors"]))

        empty = [r["repository"] for r in repos_info if not r["items"] and not r["errors"]]
        if empty:
            fh.write(f"\n## Repositories without secrets or variables ({len(empty)})\n\n")
            fh.write("".join(f"- {r}\n" for r in empty))

    return base


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("org", help="GitHub organization login")
    p.add_argument("--output-dir", default="reports")
    p.add_argument("--scan-workflows", action="store_true",
                   help="Also parse .github/workflows/*.yml for secrets.*/vars.* references (more API calls)")
    p.add_argument("--hide-variable-values", action="store_true",
                   help="Do not include Actions variable values in the report")
    p.add_argument("--skip-archived", action="store_true")
    p.add_argument("--skip-forks", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="Process at most N repositories (0 = all)")
    p.add_argument("--min-interval", type=float, default=0.1,
                   help="Minimum seconds between API requests (default 0.1)")
    p.add_argument("--reserve", type=int, default=50,
                   help="Wait for rate-limit reset when remaining requests fall below this (default 50)")
    args = p.parse_args()

    gh = GitHub(get_token(), min_interval=args.min_interval, reserve=args.reserve)

    status, rl, _ = gh.request("/rate_limit")
    if status == 200:
        core = rl["resources"]["core"]
        log(f"Rate limit: {core['remaining']}/{core['limit']} remaining")

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

    all_rows, repos_info = [], []
    try:
        for i, repo in enumerate(repos, 1):
            rows, errors = collect_repo(gh, repo, args.scan_workflows, args.hide_variable_values)
            all_rows.extend(rows)
            repos_info.append({"repository": repo["full_name"], "archived": repo.get("archived", False),
                               "items": rows, "errors": errors})
            n_secrets = sum(1 for r in rows if r["type"] == "secret")
            log(f"[{i}/{len(repos)}] {repo['full_name']}: {n_secrets} secrets, {len(rows) - n_secrets} variables"
                + (f", {len(errors)} errors" if errors else ""))
    except KeyboardInterrupt:
        log("Interrupted, writing partial report...")

    base = write_reports(args.org, repos_info, all_rows, args.output_dir)
    log(f"Done. {gh.requests_made} API requests made.")
    log(f"Reports: {base}.md, {base}.csv, {base}.json")


if __name__ == "__main__":
    main()
