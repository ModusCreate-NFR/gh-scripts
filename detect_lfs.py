#!/usr/bin/env python3
"""
detect_lfs.py - Detect LFS files in organization repositories

Scans every repository in a GitHub organization for Git LFS usage.
Processes repositories in parallel with configurable concurrency.
Reports findings in JSON, CSV, and Markdown formats.
Scans all mirrored branch and tag history without downloading LFS payloads.
Counts unique LFS object IDs per repository and sums their pointer-declared sizes.

Usage:
    python3 detect_lfs.py <organization> [--max-parallel N] [--skip-archived] [--skip-forks] [--output-dir DIR] [--limit N]

Requirements:
    - GitHub CLI (gh) with authentication
    - Git with LFS support installed
    - git-sizer installed (brew install git-sizer on macOS)
    - Python 3.8+

Examples:
    python3 detect_lfs.py myorg
    python3 detect_lfs.py myorg --max-parallel 5 --skip-archived --output-dir ./reports
    python3 detect_lfs.py myorg --limit 10  # Test with first 10 repos
"""

import json
import csv
import subprocess
import sys
import os
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import re


def format_number(num: int) -> str:
    """Format number with thousand separators."""
    return f"{num:,}"


@dataclass
class LFSAnalysisResult:
    """Result of LFS analysis for a repository."""
    repository: str
    url: str
    archived: bool
    fork: bool
    status: str  # "has-lfs", "no-lfs", "error"
    lfs_files: Optional[int]
    lfs_size_bytes: Optional[int]
    repo_size_bytes: Optional[int]  # git-sizer unique_blob_size (uncompressed bytes)
    repo_size_formatted: str  # Human-readable total size
    error: Optional[str]
    timestamp: int


class LFSDetector:
    """Detect LFS usage in GitHub organization repositories."""
    
    def __init__(self, org: str, max_parallel: int = 10, skip_archived: bool = False, 
                 skip_forks: bool = False, output_dir: str = "./reports", limit: Optional[int] = None):
        self.org = org
        self.max_parallel = max_parallel
        self.skip_archived = skip_archived
        self.skip_forks = skip_forks
        self.output_dir = Path(output_dir).resolve()
        self.limit = limit
        self.timestamp = int(time.time())
        self.results: List[LFSAnalysisResult] = []
        
        # Create output directory
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ERROR] Failed to create output directory {self.output_dir}: {e}")
            sys.exit(1)
    
    def get_repositories(self) -> List[dict]:
        """Fetch repositories from organization."""
        print(f"[INFO] Fetching repositories from organization: {self.org}")
        
        repos = []
        try:
            # Use gh CLI for consistent behavior with other scripts
            cmd = ['gh', 'repo', 'list', self.org, '--limit', '10000',
                   '--json', 'nameWithOwner,url,isArchived,isFork',
                   '-q', '.[] | @json']
            
            if self.skip_archived:
                cmd.insert(4, '--no-archived')
            
            if self.skip_forks:
                cmd.insert(4, '--source')
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
            
            for line in result.stdout.strip().split('\n'):
                if line:
                    repo_data = json.loads(line)
                    repos.append(repo_data)
            
            if self.limit:
                repos = repos[:self.limit]
            
            print(f"[SUCCESS] Found {len(repos)} repositories")
            return repos
            
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            print(f"[ERROR] Failed to fetch repositories: {e}")
            sys.exit(1)
    
    def analyze_repo(self, repo_data: dict) -> LFSAnalysisResult:
        """Analyze single repository for LFS usage."""
        repo_name = repo_data['nameWithOwner']
        repo_url = repo_data['url']
        is_archived = repo_data['isArchived']
        is_fork = repo_data['isFork']
        
        print(f"[INFO] Analyzing: {repo_name}")
        
        status = "error"
        lfs_files = None
        lfs_size = None
        repo_size = None
        repo_size_formatted = "Unknown"
        error_msg = None
        stage = "Reading repository metadata"
        
        try:
            env = dict(os.environ, GIT_LFS_SKIP_SMUDGE='1', GIT_TERMINAL_PROMPT='0')
            stage = "Creating temporary clone directory"
            with tempfile.TemporaryDirectory(prefix='lfs-scan-') as work_dir:
                mirror_path = Path(work_dir) / 'repo.git'
                stage = "Cloning Git history"
                print(f"[INFO] {repo_name}: Cloning Git history (no LFS payloads)")
                subprocess.run(
                    ['git', 'clone', '--mirror', '--', repo_url, str(mirror_path)],
                    capture_output=True, text=True, check=True, timeout=1800, env=env
                )
                stage = "Reading LFS pointers across history"
                print(f"[INFO] {repo_name}: Reading LFS pointers across history")
                result = subprocess.run(
                    ['git', 'lfs', 'ls-files', '--all', '--json', '--include=', '--exclude='],
                    cwd=mirror_path, capture_output=True, text=True, check=True,
                    timeout=1800, env=dict(env, GIT_DIR=str(mirror_path))
                )
                stage = "Parsing LFS pointer output"
                lfs_files, lfs_size = self._parse_lfs_objects(result.stdout)
                stage = "Measuring repository size with git-sizer"
                print(f"[INFO] {repo_name}: Measuring Git blob size with git-sizer")
                size_result = subprocess.run(
                    ['git', 'sizer', '--json', '--json-version=1'],
                    cwd=mirror_path, capture_output=True, text=True, check=True,
                    timeout=1800, env=dict(env, GIT_DIR=str(mirror_path))
                )
                stage = "Parsing git-sizer output"
                size_data = json.loads(size_result.stdout)
                if (not isinstance(size_data, dict)
                        or type(size_data.get('unique_blob_size')) is not int
                        or size_data['unique_blob_size'] < 0):
                    raise ValueError("Invalid git-sizer JSON: expected nonnegative unique_blob_size")
                repo_size = size_data['unique_blob_size']
                repo_size_formatted = self._format_size(repo_size)
                stage = "Cleaning up temporary clone"
            status = "has-lfs" if lfs_files else "no-lfs"
            print(f"[SUCCESS] {repo_name}: {lfs_files} unique LFS objects "
                  f"({self._format_size(lfs_size)})")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
            if isinstance(e, subprocess.CalledProcessError):
                reason = f"Command failed with exit code {e.returncode}"
            elif isinstance(e, subprocess.TimeoutExpired):
                reason = f"Command timed out after {e.timeout} seconds"
            else:
                reason = f"{type(e).__name__}: {e}"
            error_msg = f"{stage}: {reason}"
            if isinstance(e, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
                for label, output in (('stderr', e.stderr), ('stdout', e.stdout)):
                    if isinstance(output, bytes):
                        output = output.decode('utf-8', errors='replace')
                    if output and output.strip():
                        error_msg += f"\n{label}:\n{output.strip()}"
            lfs_files = None
            lfs_size = None
            print(f"[ERROR] {repo_name}: {error_msg}")
        
        result = LFSAnalysisResult(
            repository=repo_name,
            url=repo_url,
            archived=is_archived,
            fork=is_fork,
            status=status,
            lfs_files=lfs_files,
            lfs_size_bytes=lfs_size,
            repo_size_bytes=repo_size,
            repo_size_formatted=repo_size_formatted,
            error=error_msg,
            timestamp=int(time.time())
        )
        
        return result

    @staticmethod
    def _parse_lfs_objects(output: str) -> Tuple[int, int]:
        """Sum pointer sizes once per SHA-256 object, not once per path/version."""
        data = json.loads(output)
        if not isinstance(data, dict) or 'files' not in data:
            raise ValueError("Invalid Git LFS JSON: expected a files array")
        # Git LFS encodes an empty result as null rather than [].
        if data['files'] is None:
            return 0, 0
        if not isinstance(data['files'], list):
            raise ValueError("Invalid Git LFS JSON: expected a files array")
        objects: Dict[str, int] = {}
        for entry in data['files']:
            if not isinstance(entry, dict):
                raise ValueError("Invalid Git LFS object entry")
            oid = entry.get('oid')
            size = entry.get('size')
            if (entry.get('oid_type') != 'sha256' or not isinstance(oid, str)
                    or re.fullmatch(r'[0-9a-f]{64}', oid) is None
                    or type(size) is not int or size < 0):
                raise ValueError("Invalid Git LFS object ID or size")
            if oid in objects and objects[oid] != size:
                raise ValueError(f"Conflicting sizes for LFS object {oid}")
            objects[oid] = size
        return len(objects), sum(objects.values())
    
    def run(self):
        """Run the LFS detection analysis."""
        print(f"\n[INFO] Starting LFS detection for organization: {self.org}")
        print(f"[INFO] Max parallel jobs: {self.max_parallel}")
        print(f"[INFO] Output directory: {self.output_dir}\n")
        
        repos = self.get_repositories()
        if not repos:
            print("[WARN] No repositories found")
            return True
        
        # Process in parallel
        with ThreadPoolExecutor(max_workers=self.max_parallel) as executor:
            futures = {executor.submit(self.analyze_repo, repo): repo for repo in repos}
            
            completed = 0
            for future in as_completed(futures):
                result = future.result()
                self.results.append(result)
                completed += 1
                if completed % 10 == 0:
                    print(f"[INFO] Progress: {completed}/{len(repos)} repositories analyzed")
        
        print(f"\n[SUCCESS] Analyzed {len(self.results)} repositories")
        
        # Generate reports
        self.generate_reports()
        
        errors = sum(r.status == 'error' for r in self.results)
        if errors:
            print(f"\n[ERROR] Analysis incomplete: {errors} repositories failed; see reports")
            return False
        print(f"\n[SUCCESS] Analysis complete!")
        return True
    
    def generate_reports(self):
        """Generate JSON, CSV, and Markdown reports."""
        print(f"[INFO] Generating reports...")
        
        # JSON report
        json_path = self.output_dir / f"{self.org}-lfs-{self.timestamp}.json"
        with open(json_path, 'w') as f:
            json.dump([asdict(r) for r in self.results], f, indent=2)
        print(f"[SUCCESS] JSON report: {json_path}")
        
        # CSV report
        csv_path = self.output_dir / f"{self.org}-lfs-{self.timestamp}.csv"
        with open(csv_path, 'w', newline='') as f:
            fieldnames = ['Repository', 'URL', 'Archived', 'Fork', 'Status', 'LFS Files', 'LFS Size (bytes)', 'Repo Size (bytes)', 'Repo Size (formatted)', 'Error']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for result in self.results:
                writer.writerow({
                    'Repository': result.repository,
                    'URL': result.url,
                    'Archived': result.archived,
                    'Fork': result.fork,
                    'Status': result.status,
                    'LFS Files': result.lfs_files if result.lfs_files is not None else 'Unknown',
                    'LFS Size (bytes)': result.lfs_size_bytes if result.lfs_size_bytes is not None else 'Unknown',
                    'Repo Size (bytes)': result.repo_size_bytes if result.repo_size_bytes is not None else 'Unknown',
                    'Repo Size (formatted)': result.repo_size_formatted,
                    'Error': result.error or 'None'
                })
        print(f"[SUCCESS] CSV report: {csv_path}")
        
        # Markdown report
        md_path = self.output_dir / f"{self.org}-lfs-{self.timestamp}.md"
        with open(md_path, 'w') as f:
            f.write(self._generate_markdown())
        print(f"[SUCCESS] Markdown report: {md_path}")
    
    def _generate_markdown(self) -> str:
        """Generate Markdown report content."""
        has_lfs_repos = [r for r in self.results if r.status == 'has-lfs']
        total_size = sum(r.lfs_size_bytes for r in has_lfs_repos if r.lfs_size_bytes is not None)
        errors = [r for r in self.results if r.status == 'error']
        total_repo_size = sum(r.repo_size_bytes for r in self.results if r.repo_size_bytes is not None)
        
        # Use timezone-aware datetime to avoid deprecation warning
        from datetime import datetime, timezone
        timestamp_utc = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        
        md = f"""# LFS Detection Report

**Organization:** {self.org}

**Timestamp:** {timestamp_utc}

## Summary

| Metric | Value |
|--------|-------|
| Total Repositories | {format_number(len(self.results))} |
| Repositories with LFS | {format_number(len(has_lfs_repos))} |
| Failed Repositories | {format_number(len(errors))} |
| Total LFS Size | {self._format_size(total_size)} |
| Total Repository Size | {self._format_size(total_repo_size)} |

LFS counts are unique object IDs per repository across all mirrored branch/tag
history, including deleted files. Sizes come from pointers; payload availability
and GitHub billing usage are not verified. Objects shared across repositories
are counted once in each repository.
Repository size is git-sizer's unique_blob_size: total uncompressed bytes of
distinct reachable Git file blobs across history, including LFS pointer files
but excluding LFS payloads, commits, trees, and tags. This is not compressed
disk usage or an LFS percentage denominator. Repository size totals exclude
unknown measurements.

"""
        if errors:
            md += "**INCOMPLETE:** LFS totals exclude failed repositories; their sizes are unknown.\n\n"
            md += "## Failed repositories\n\n"
            for result in errors:
                message = result.error or 'Unknown error'
                md += f"### [{result.repository}]({result.url})\n\n"
                md += "- **Status:** error\n- **Reason:**\n\n"
                md += "\n".join(f"    {line}" for line in message.splitlines()) + "\n\n"
        
        if has_lfs_repos:
            md += """## Repositories with LFS

"""
            for result in sorted(has_lfs_repos, key=lambda x: x.lfs_size_bytes or 0, reverse=True):
                assert result.lfs_files is not None and result.lfs_size_bytes is not None
                md += f"""### [{result.repository}]({result.url})

- **Status:** {result.status}
- **LFS Files (unique objects):** {format_number(result.lfs_files)}
- **LFS Size:** {self._format_size(result.lfs_size_bytes)}
- **Total Repository Size:** {result.repo_size_formatted}
- **Archived:** {result.archived}
- **Fork:** {result.fork}

"""
        else:
            md += "No LFS objects found in successfully scanned repositories.\n\n"
        
        return md
    
    @staticmethod
    def _format_size(bytes_size: int) -> str:
        """Format decimal sizes with the same rounding as Git LFS."""
        if bytes_size < 1000:
            return f"{bytes_size} B"
        divisor = 1000
        for unit in ['KB', 'MB', 'GB', 'TB', 'PB', 'EB']:
            if bytes_size < divisor * 1000 or unit == 'EB':
                # Git LFS rounds to tenths before choosing display precision.
                size = ((bytes_size * 10 + divisor // 2) // divisor) / 10
                return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"
            divisor *= 1000
        raise ValueError("Size exceeds supported units")


def check_lfs_installed():
    """Check if Git LFS is installed."""
    try:
        result = subprocess.run(['git', 'lfs', 'version'], 
                              capture_output=True, text=True, check=True)
        print(f"[SUCCESS] Git LFS is installed: {result.stdout.strip()}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[ERROR] Git LFS is not installed or not available in PATH")
        print("[ERROR] Please install Git LFS:")
        print("[ERROR]   macOS:  brew install git-lfs")
        print("[ERROR]   Linux:  apt-get install git-lfs (Debian/Ubuntu) or yum install git-lfs (RHEL/CentOS)")
        print("[ERROR]   Other:  https://git-lfs.github.com/")
        print("[ERROR] After installation, run: git lfs install")
        sys.exit(1)


def check_sizer_installed():
    """Fail before scanning if git-sizer cannot run."""
    try:
        result = subprocess.run(
            ['git', 'sizer', '--version'],
            capture_output=True, text=True, check=True, timeout=30
        )
        print(f"[SUCCESS] git-sizer is installed: {result.stdout.strip()}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[ERROR] git-sizer is not installed or cannot run: {e}")
        print("[ERROR] macOS: brew install git-sizer")
        print("[ERROR] Other platforms: https://github.com/github/git-sizer/releases")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='Detect LFS files in GitHub organization repositories',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument('organization', help='GitHub organization name')
    parser.add_argument('--max-parallel', type=int, default=10, 
                       help='Maximum parallel analysis jobs (default: 10)')
    parser.add_argument('--skip-archived', action='store_true',
                       help='Skip archived repositories')
    parser.add_argument('--skip-forks', action='store_true',
                       help='Skip forked repositories')
    parser.add_argument('--output-dir', default='./reports',
                       help='Output directory for reports (default: ./reports)')
    parser.add_argument('--limit', type=int, default=None,
                       help='Only process first N repositories (useful for testing)')
    
    args = parser.parse_args()
    
    check_lfs_installed()
    check_sizer_installed()
    
    detector = LFSDetector(
        org=args.organization,
        max_parallel=args.max_parallel,
        skip_archived=args.skip_archived,
        skip_forks=args.skip_forks,
        output_dir=args.output_dir,
        limit=args.limit
    )
    
    if not detector.run():
        sys.exit(1)


if __name__ == '__main__':
    main()
