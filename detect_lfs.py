#!/usr/bin/env python3
"""
detect_lfs.py - Detect LFS files in organization repositories

Scans every repository in a GitHub organization for Git LFS usage.
Processes repositories in parallel with configurable concurrency.
Reports findings in JSON, CSV, and Markdown formats.

Usage:
    python3 detect_lfs.py <organization> [--max-parallel N] [--skip-archived] [--skip-forks] [--output-dir DIR] [--limit N]

Requirements:
    - GitHub CLI (gh) with authentication
    - Git with LFS support installed
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
import shutil
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

# Shared utilities (imported from list_org_secrets.py pattern)
try:
    # Try to import shared utilities if they exist
    from list_org_secrets import get_github_token, api_call, format_number
except ImportError:
    # Fallback implementations
    def get_github_token() -> str:
        """Get GitHub token from environment or gh CLI."""
        token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
        if not token:
            try:
                result = subprocess.run(['gh', 'auth', 'token'], 
                                      capture_output=True, text=True, check=True)
                token = result.stdout.strip()
            except subprocess.CalledProcessError:
                print("Error: Cannot get GitHub token. Set GITHUB_TOKEN/GH_TOKEN or run 'gh auth login'")
                sys.exit(1)
        return token

    def api_call(method: str, url: str, token: str, **kwargs) -> dict:
        """Make GitHub API call."""
        headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json',
            **kwargs.get('headers', {})
        }
        
        import urllib.request
        import urllib.error
        
        req = urllib.request.Request(
            f'https://api.github.com{url}',
            method=method,
            headers=headers
        )
        
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            raise Exception(f"API error {e.code}: {e.reason}")

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
    lfs_files: int
    lfs_size_bytes: int
    repo_size_bytes: int  # Total repository size
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
        self.token = get_github_token()
        self.temp_dir = tempfile.mkdtemp(prefix=f"lfs-scan-{int(time.time())}-")
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
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            for line in result.stdout.strip().split('\n'):
                if line:
                    repo_data = json.loads(line)
                    repos.append(repo_data)
            
            if self.limit:
                repos = repos[:self.limit]
            
            print(f"[SUCCESS] Found {len(repos)} repositories")
            return repos
            
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to fetch repositories: {e.stderr}")
            sys.exit(1)
    
    def analyze_repo(self, repo_data: dict) -> LFSAnalysisResult:
        """Analyze single repository for LFS usage."""
        repo_name = repo_data['nameWithOwner']
        repo_url = repo_data['url']
        is_archived = repo_data['isArchived']
        is_fork = repo_data['isFork']
        
        print(f"[INFO] Analyzing: {repo_name}")
        
        repo_work_dir = Path(self.temp_dir) / repo_name.replace('/', '-')
        repo_work_dir.mkdir(parents=True, exist_ok=True)
        
        status = "no-lfs"
        lfs_files = 0
        lfs_size = 0
        repo_size = 0
        repo_size_formatted = "0 B"
        error_msg = None
        
        try:
            # Clone mirror
            mirror_path = repo_work_dir / "repo.git"
            cmd = ['git', 'clone', '--mirror', repo_url, str(mirror_path)]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_work_dir))
            
            if result.returncode != 0:
                error_msg = f"Clone failed: {result.stderr[:100]}"
                print(f"[ERROR] {repo_name}: {error_msg}")
            else:
                # Fetch LFS objects
                os.chdir(str(mirror_path))
                fetch_cmd = ['git', 'lfs', 'fetch', '--all', 'origin']
                subprocess.run(fetch_cmd, capture_output=True, text=True)
                
                # Get repository size using git sizer
                repo_size = self._get_repo_size(str(mirror_path))
                repo_size_formatted = self._format_size(repo_size)
                
                # Get LFS info
                info_cmd = ['git', 'lfs', 'migrate', 'info', '--everything']
                result = subprocess.run(info_cmd, capture_output=True, text=True)
                
                if result.returncode == 0 and result.stdout:
                    output = result.stdout
                    if 'numObjects' in output:
                        status = "has-lfs"
                        # Parse output for LFS statistics
                        for line in output.split('\n'):
                            if 'numObjects:' in line:
                                lfs_files = int(line.split(':')[1].strip() or 0)
                            elif 'totalSize:' in line:
                                lfs_size = int(line.split(':')[1].strip() or 0)
                        
                        size_str = self._format_size(lfs_size)
                        print(f"[SUCCESS] {repo_name}: Found {lfs_files} LFS files ({size_str}) | Repo size: {repo_size_formatted}")
                    else:
                        print(f"[INFO] {repo_name}: No LFS files found | Repo size: {repo_size_formatted}")
                
        except Exception as e:
            error_msg = str(e)[:100]
            print(f"[ERROR] {repo_name}: {error_msg}")
        finally:
            # Cleanup
            try:
                shutil.rmtree(repo_work_dir)
            except Exception:
                pass
        
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
    
    def run(self):
        """Run the LFS detection analysis."""
        print(f"\n[INFO] Starting LFS detection for organization: {self.org}")
        print(f"[INFO] Max parallel jobs: {self.max_parallel}")
        print(f"[INFO] Output directory: {self.output_dir}\n")
        
        repos = self.get_repositories()
        if not repos:
            print("[WARN] No repositories found")
            return
        
        # Process in parallel
        with ThreadPoolExecutor(max_workers=self.max_parallel) as executor:
            futures = {executor.submit(self.analyze_repo, repo): repo for repo in repos}
            
            completed = 0
            for future in as_completed(futures):
                try:
                    result = future.result()
                    self.results.append(result)
                    completed += 1
                    if completed % 10 == 0:
                        print(f"[INFO] Progress: {completed}/{len(repos)} repositories analyzed")
                except Exception as e:
                    print(f"[ERROR] Unexpected error: {e}")
        
        print(f"\n[SUCCESS] Analyzed {len(self.results)} repositories")
        
        # Generate reports
        self.generate_reports()
        
        # Cleanup
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        print(f"\n[SUCCESS] Analysis complete!")
    
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
                    'LFS Files': result.lfs_files,
                    'LFS Size (bytes)': result.lfs_size_bytes,
                    'Repo Size (bytes)': result.repo_size_bytes,
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
        total_size = sum(r.lfs_size_bytes for r in has_lfs_repos)
        total_repo_size = sum(r.repo_size_bytes for r in self.results if r.repo_size_bytes > 0)
        
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
| Total LFS Size | {self._format_size(total_size)} |
| Total Repository Size | {self._format_size(total_repo_size)} |

"""
        
        if has_lfs_repos:
            md += """## Repositories with LFS

"""
            for result in sorted(has_lfs_repos, key=lambda x: x.lfs_size_bytes, reverse=True):
                lfs_percentage = (result.lfs_size_bytes / result.repo_size_bytes * 100) if result.repo_size_bytes > 0 else 0
                md += f"""### [{result.repository}]({result.url})

- **Status:** {result.status}
- **LFS Files:** {format_number(result.lfs_files)}
- **LFS Size:** {self._format_size(result.lfs_size_bytes)} ({lfs_percentage:.1f}% of repo)
- **Total Repository Size:** {result.repo_size_formatted}
- **Archived:** {result.archived}
- **Fork:** {result.fork}

"""
        else:
            md += "No repositories with LFS files detected.\n\n"
        
        return md
    
    @staticmethod
    def _format_size(bytes_size: int) -> str:
        """Format bytes to human-readable size."""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_size < 1024.0:
                return f"{bytes_size:.2f} {unit}"
            bytes_size /= 1024.0
        return f"{bytes_size:.2f} PB"
    
    def _get_repo_size(self, repo_path: str) -> int:
        """Get total repository size using git sizer or fallback method."""
        try:
            # Try using git sizer if available
            try:
                result = subprocess.run(
                    ['git', 'sizer', '--verbose'],
                    capture_output=True,
                    text=True,
                    cwd=repo_path,
                    timeout=60
                )
                
                if result.returncode == 0 and result.stdout:
                    # Parse git sizer output for total size
                    for line in result.stdout.split('\n'):
                        if 'total file size' in line.lower():
                            # Extract size from line like "total file size:   1234567890"
                            parts = line.split(':')
                            if len(parts) > 1:
                                try:
                                    size_str = parts[1].strip().split()[0]
                                    size = int(size_str)
                                    if size > 0:
                                        return size
                                except (ValueError, IndexError):
                                    pass
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            
            # Fallback 1: use du with platform-specific flags
            # macOS uses -s (no -b), Linux uses -sb for bytes
            try:
                import platform
                if platform.system() == "Darwin":  # macOS
                    # macOS du returns bytes by default with -s
                    result = subprocess.run(
                        ['du', '-s', repo_path],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                else:  # Linux and others
                    result = subprocess.run(
                        ['du', '-sb', repo_path],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                
                if result.returncode == 0 and result.stdout.strip():
                    size_bytes = int(result.stdout.split()[0])
                    if size_bytes > 0:
                        return size_bytes
            except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, IndexError):
                pass
            
            # Fallback 2: use Python's os.walk to calculate size
            try:
                import os
                total_size = 0
                for dirpath, dirnames, filenames in os.walk(repo_path):
                    for filename in filenames:
                        filepath = os.path.join(dirpath, filename)
                        try:
                            total_size += os.path.getsize(filepath)
                        except (OSError, FileNotFoundError):
                            pass
                if total_size > 0:
                    return total_size
            except Exception:
                pass
            
        except Exception as e:
            pass
        
        return 0


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
    
    detector = LFSDetector(
        org=args.organization,
        max_parallel=args.max_parallel,
        skip_archived=args.skip_archived,
        skip_forks=args.skip_forks,
        output_dir=args.output_dir,
        limit=args.limit
    )
    
    detector.run()


if __name__ == '__main__':
    main()
