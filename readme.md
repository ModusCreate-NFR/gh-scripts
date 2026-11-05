# gh-scripts

Python scripts that scan every repository in a GitHub organization and produce reports (`.md`, `.csv`, `.json`) in the `reports/` folder.

| Script | What it reports |
|---|---|
| `list_org_secrets.py` | Actions/Dependabot secrets and Actions variables per repository |
| `list_org_packages.py` | GitHub Packages (npm, NuGet, Maven, RubyGems, Docker, container) per repository |
| `list_org_cicd.py` | CI/CD tool per repository: GitHub Actions, Jenkins, Harness, or "Not detected" |

## Requirements

- Python 3.8+ (standard library only, no `pip install` needed)
- A GitHub token, either:
  - `GITHUB_TOKEN` or `GH_TOKEN` environment variable, or
  - the [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`); the scripts fall back to `gh auth token`

All scripts must stay in the same folder (`list_org_packages.py` and `list_org_cicd.py` import shared code from `list_org_secrets.py`).

## Authentication

```bash
gh auth login
# Add the scopes needed by the scripts
gh auth refresh -h github.com -s read:org,read:packages,admin:repo_hook
export GITHUB_TOKEN=$(gh auth token)
```

| Script | Classic token scopes | Fine-grained token permissions (read) |
|---|---|---|
| `list_org_secrets.py` | `repo`, `read:org` | Secrets, Variables, Environments, Dependabot secrets, Contents |
| `list_org_packages.py` | `read:packages` (+ `repo` for private repos) | Packages |
| `list_org_cicd.py` | `repo`; `admin:repo_hook` for webhook detection | Contents, Webhooks |

Repositories the token can't read are not skipped silently; the error is recorded in the report.

## Usage

### Secrets and variables

```bash
python3 list_org_secrets.py <org>
python3 list_org_secrets.py <org> --scan-workflows --skip-archived --skip-forks
```

| Option | Description |
|---|---|
| `--scan-workflows` | Also find `secrets.*` / `vars.*` referenced in `.github/workflows/*.yml` and flag the ones not defined |
| `--hide-variable-values` | Do not include Actions variable values in the report |
| `--skip-archived` / `--skip-forks` | Ignore archived / forked repositories |
| `--limit N` | Only process the first N repositories (useful for testing) |

Secret values are never available through the API; only names are reported.

### Packages

```bash
python3 list_org_packages.py <org>
python3 list_org_packages.py <org> --types npm nuget container --with-versions --include-empty-repos
```

| Option | Description |
|---|---|
| `--types` | Package types to list: `npm maven rubygems docker nuget container` (default: all) |
| `--with-versions` | Fetch the latest version/tags of each package (1 extra call per package) |
| `--include-empty-repos` | Also list repositories without packages |

### CI/CD

```bash
python3 list_org_cicd.py <org>
python3 list_org_cicd.py <org> --skip-archived --skip-forks
```

Detection rules (default branch):

| Tool | Detected by |
|---|---|
| GitHub Actions | `.github/workflows/*.yml` / `*.yaml` |
| Jenkins | `Jenkinsfile`, `Jenkinsfile.*`, `*.jenkinsfile` |
| Harness | `.harness/` directory, `*harness*.yml` files, or a repository webhook to `*.harness.io` |

| Option | Description |
|---|---|
| `--no-webhooks` | Skip the Harness webhook check (saves 1 call per repo) |
| `--skip-archived` / `--skip-forks` | Ignore archived / forked repositories |
| `--limit N` | Only process the first N repositories |

## Rate limiting

All scripts share the same client, built for large organizations:

- Waits `--min-interval` seconds between requests (default `0.1`) to avoid secondary rate limits.
- When remaining quota drops below `--reserve` (default `50`), sleeps until the rate limit resets.
- Retries on secondary rate limits (`Retry-After` or exponential backoff), 5xx errors, and network errors.
- `Ctrl+C` stops the scan and still writes a partial report.

## Output

Reports are written to `reports/` (change with `--output-dir`):

```
reports/<org>-secrets-<timestamp>.{md,csv,json}
reports/<org>-packages-<timestamp>.{md,csv,json}
reports/<org>-cicd-<timestamp>.{md,csv,json}
```
