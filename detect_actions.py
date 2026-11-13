#!/usr/bin/env python3
"""
GitHub Actions Analyzer
Scans all repositories in an organization for GitHub Actions usage,
collects workflow metadata, and generates dependency graphs.

Features:
- Parallel repository scanning (max 10 concurrent)
- YAML workflow parsing
- Action extraction and versioning
- Cross-repo workflow call detection
- Dependency graph generation
- Multi-format reporting (JSON, CSV, Markdown with diagrams)
"""

import json
import csv
import sys
import subprocess
import os
import shutil
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from collections import defaultdict
import re

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    print("[WARNING] PyYAML not found. Install with: pip3 install pyyaml")


@dataclass
class WorkflowAction:
    """Single action usage in a workflow."""
    action_name: str          # e.g., "actions/checkout"
    action_version: str       # e.g., "v4", "v1.2.3", or "@latest"
    workflow_file: str        # e.g., ".github/workflows/ci.yml"
    repository: str           # e.g., "org/repo"
    step_name: str = ""       # Step name in workflow
    
    @property
    def full_action_ref(self) -> str:
        """Return full action reference."""
        return f"{self.action_name}@{self.action_version}"


@dataclass
class WorkflowCall:
    """Workflow call to another workflow."""
    source_repo: str          # e.g., "org/repo"
    source_workflow: str      # e.g., ".github/workflows/ci.yml"
    target_repo: str          # e.g., "org/other-repo"
    target_workflow: str      # e.g., ".github/workflows/reusable.yml"
    target_ref: str = ""      # e.g., "v1", "main", or @commit


@dataclass
class RepositoryActions:
    """Actions analysis for a repository."""
    repository: str
    url: str
    archived: bool
    fork: bool
    analyzed_at: str
    workflow_count: int = 0
    action_count: int = 0
    unique_actions: int = 0
    workflows: List[Dict] = field(default_factory=list)
    actions: List[WorkflowAction] = field(default_factory=list)
    workflow_calls: List[WorkflowCall] = field(default_factory=list)
    error: str = ""
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = {
            'repository': self.repository,
            'url': self.url,
            'archived': self.archived,
            'fork': self.fork,
            'analyzed_at': self.analyzed_at,
            'workflow_count': self.workflow_count,
            'action_count': self.action_count,
            'unique_actions': self.unique_actions,
            'workflows': self.workflows,
            'actions': [
                {
                    'action_name': a.action_name,
                    'action_version': a.action_version,
                    'workflow_file': a.workflow_file,
                    'repository': a.repository,
                    'step_name': a.step_name,
                    'full_action_ref': a.full_action_ref
                }
                for a in self.actions
            ],
            'workflow_calls': [
                {
                    'source_repo': wc.source_repo,
                    'source_workflow': wc.source_workflow,
                    'target_repo': wc.target_repo,
                    'target_workflow': wc.target_workflow,
                    'target_ref': wc.target_ref
                }
                for wc in self.workflow_calls
            ],
            'error': self.error
        }
        return data


class ActionsAnalyzer:
    """Analyzes GitHub Actions usage across organization repositories."""
    
    def __init__(self, org: str, max_workers: int = 10):
        """Initialize analyzer."""
        self.org = org
        self.max_workers = max_workers
        self.temp_dir = Path(f"/tmp/gh-actions-{org}-{int(datetime.now(timezone.utc).timestamp())}")
        self.reports_dir = Path("reports")
        self.all_results: List[RepositoryActions] = []
        self.all_workflow_calls: List[WorkflowCall] = []
        
        # Create directories
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"[INFO] Temp dir: {self.temp_dir}")
        print(f"[INFO] Reports dir: {self.reports_dir}")
    
    def get_org_repositories(self) -> List[Dict]:
        """Fetch all repositories from organization."""
        print(f"[INFO] Fetching repositories from organization: {self.org}")
        
        try:
            cmd = [
                'gh', 'repo', 'list', self.org,
                '--limit', '10000',
                '--json', 'nameWithOwner,url,isArchived,isFork',
                '-q', '.[] | @json'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode != 0:
                print(f"[ERROR] {result.stderr}")
                sys.exit(1)
            
            repos = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    repos.append(json.loads(line))
            
            print(f"[SUCCESS] Found {len(repos)} repositories")
            return repos
            
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to fetch repositories: {e.stderr}")
            sys.exit(1)
    
    def extract_actions_from_workflow(self, workflow_path: Path, repo_name: str) -> Tuple[List[WorkflowAction], List[WorkflowCall]]:
        """Extract actions and workflow calls from a single workflow file."""
        actions = []
        workflow_calls = []
        
        if not HAS_YAML:
            return actions, workflow_calls
        
        try:
            with open(workflow_path, 'r') as f:
                try:
                    workflow_data = yaml.safe_load(f)
                except yaml.YAMLError:
                    return actions, workflow_calls
            
            if not workflow_data or not isinstance(workflow_data, dict):
                return actions, workflow_calls
            
            workflow_file_rel = str(workflow_path.relative_to(workflow_path.parent.parent.parent))
            
            # Extract actions from jobs
            jobs = workflow_data.get('jobs', {})
            if isinstance(jobs, dict):
                for job_name, job_data in jobs.items():
                    if not isinstance(job_data, dict):
                        continue
                    
                    # Check for workflow calls (reusable workflows)
                    uses = job_data.get('uses')
                    if uses and isinstance(uses, str):
                        # This is a workflow call
                        target_repo, target_workflow, target_ref = self._parse_workflow_call(uses)
                        if target_repo:
                            workflow_calls.append(WorkflowCall(
                                source_repo=repo_name,
                                source_workflow=workflow_file_rel,
                                target_repo=target_repo,
                                target_workflow=target_workflow,
                                target_ref=target_ref
                            ))
                    
                    # Extract actions from steps
                    steps = job_data.get('steps', [])
                    if isinstance(steps, list):
                        for step_idx, step in enumerate(steps):
                            if not isinstance(step, dict):
                                continue
                            
                            step_name = step.get('name', f'step-{step_idx}')
                            uses = step.get('uses')
                            
                            if uses and isinstance(uses, str):
                                action_name, action_version = self._parse_action(uses)
                                if action_name:
                                    actions.append(WorkflowAction(
                                        action_name=action_name,
                                        action_version=action_version,
                                        workflow_file=workflow_file_rel,
                                        repository=repo_name,
                                        step_name=step_name
                                    ))
        
        except Exception as e:
            print(f"[ERROR] Failed to parse {workflow_path}: {str(e)[:100]}")
        
        return actions, workflow_calls
    
    def _parse_action(self, uses_str: str) -> Tuple[str, str]:
        """Parse action reference. Examples:
        - actions/checkout@v4
        - docker/build-push-action@v5
        - owner/action@sha
        - ./path/to/action
        """
        uses_str = uses_str.strip()
        
        # Local actions
        if uses_str.startswith('./'):
            return None, None
        
        # Action with version
        if '@' in uses_str:
            action_name, version = uses_str.rsplit('@', 1)
            return action_name, version
        
        return None, None
    
    def _parse_workflow_call(self, uses_str: str) -> Tuple[str, str, str]:
        """Parse workflow call reference. Examples:
        - owner/repo/.github/workflows/workflow.yml@v1
        - owner/repo/.github/workflows/workflow.yml@main
        - org/repo/.github/workflows/reusable.yml@sha
        """
        uses_str = uses_str.strip()
        
        if '@' not in uses_str:
            return None, None, None
        
        parts = uses_str.rsplit('@', 1)
        ref_part = parts[0]
        version = parts[1] if len(parts) > 1 else ""
        
        # Expected format: owner/repo/.github/workflows/workflow.yml
        if '.github/workflows/' not in ref_part:
            return None, None, None
        
        path_parts = ref_part.split('/')
        if len(path_parts) < 4:
            return None, None, None
        
        owner = path_parts[0]
        repo = path_parts[1]
        workflow_idx = path_parts.index('.github') if '.github' in path_parts else -1
        
        if workflow_idx >= 0 and workflow_idx + 2 < len(path_parts):
            workflow_file = '/'.join(path_parts[workflow_idx:])
            return f"{owner}/{repo}", workflow_file, version
        
        return None, None, None
    
    def analyze_repo(self, repo_data: dict) -> RepositoryActions:
        """Analyze single repository for GitHub Actions."""
        repo_name = repo_data['nameWithOwner']
        repo_url = repo_data['url']
        is_archived = repo_data['isArchived']
        is_fork = repo_data['isFork']
        
        print(f"[INFO] Analyzing: {repo_name}")
        
        repo_work_dir = self.temp_dir / repo_name.replace('/', '-')
        repo_work_dir.mkdir(parents=True, exist_ok=True)
        
        result = RepositoryActions(
            repository=repo_name,
            url=repo_url,
            archived=is_archived,
            fork=is_fork,
            analyzed_at=datetime.now(timezone.utc).isoformat()
        )
        
        try:
            # Clone repository
            cmd = ['git', 'clone', '--depth', '1', repo_url, str(repo_work_dir / 'repo')]
            clone_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if clone_result.returncode != 0:
                result.error = f"Clone failed: {clone_result.stderr[:100]}"
                print(f"[ERROR] {repo_name}: {result.error}")
                return result
            
            # Find workflows
            workflows_dir = repo_work_dir / 'repo' / '.github' / 'workflows'
            if not workflows_dir.exists():
                print(f"[INFO] {repo_name}: No workflows found")
                return result
            
            # Process workflow files
            workflow_files = list(workflows_dir.glob('*.yml')) + list(workflows_dir.glob('*.yaml'))
            result.workflow_count = len(workflow_files)
            
            for workflow_file in workflow_files:
                try:
                    with open(workflow_file, 'r') as f:
                        workflow_content = f.read()
                    
                    result.workflows.append({
                        'file': str(workflow_file.relative_to(repo_work_dir / 'repo')),
                        'size': len(workflow_content),
                        'lines': len(workflow_content.split('\n'))
                    })
                    
                    # Extract actions
                    actions, workflow_calls = self.extract_actions_from_workflow(workflow_file, repo_name)
                    result.actions.extend(actions)
                    result.workflow_calls.extend(workflow_calls)
                
                except Exception as e:
                    print(f"[ERROR] {repo_name}/{workflow_file.name}: {str(e)[:100]}")
            
            # Calculate statistics
            result.action_count = len(result.actions)
            result.unique_actions = len(set(a.full_action_ref for a in result.actions))
            
            if result.workflow_count > 0:
                print(f"[SUCCESS] {repo_name}: {result.workflow_count} workflows, {result.action_count} actions, {len(result.workflow_calls)} workflow calls")
            
            self.all_workflow_calls.extend(result.workflow_calls)
        
        except Exception as e:
            result.error = str(e)[:100]
            print(f"[ERROR] {repo_name}: {result.error}")
        
        finally:
            # Cleanup
            try:
                shutil.rmtree(repo_work_dir)
            except Exception:
                pass
        
        return result
    
    def run(self):
        """Run analysis on all repositories."""
        repos = self.get_org_repositories()
        
        print(f"\n[INFO] Analyzing {len(repos)} repositories (max {self.max_workers} concurrent)")
        print("=" * 60)
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.analyze_repo, repo): repo['nameWithOwner'] for repo in repos}
            
            for future in as_completed(futures):
                result = future.result()
                self.all_results.append(result)
        
        print("\n" + "=" * 60)
        print(f"[INFO] Analysis complete. Processing {len(self.all_results)} results")
        
        self.generate_reports()
    
    def generate_reports(self):
        """Generate JSON, CSV, and Markdown reports."""
        timestamp = int(datetime.now(timezone.utc).timestamp())
        
        # JSON report
        json_path = self.reports_dir / f"{self.org}-actions-{timestamp}.json"
        with open(json_path, 'w') as f:
            json.dump(
                [r.to_dict() for r in self.all_results],
                f,
                indent=2
            )
        print(f"[SUCCESS] JSON report: {json_path}")
        
        # CSV report
        csv_path = self.reports_dir / f"{self.org}-actions-{timestamp}.csv"
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Repository', 'Workflow File', 'Action', 'Version', 'Step Name'])
            
            for result in self.all_results:
                for action in result.actions:
                    writer.writerow([
                        result.repository,
                        action.workflow_file,
                        action.action_name,
                        action.action_version,
                        action.step_name
                    ])
        print(f"[SUCCESS] CSV report: {csv_path}")
        
        # Markdown report with dependency graph
        md_path = self.reports_dir / f"{self.org}-actions-{timestamp}.md"
        self._generate_markdown_report(md_path)
        print(f"[SUCCESS] Markdown report: {md_path}")
    
    def _generate_markdown_report(self, path: Path):
        """Generate comprehensive Markdown report with dependency diagrams."""
        with open(path, 'w') as f:
            # Header
            f.write(f"# GitHub Actions Analysis Report\n\n")
            f.write(f"**Organization:** {self.org}\n")
            f.write(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
            f.write(f"**Repositories Analyzed:** {len(self.all_results)}\n\n")
            
            # Summary statistics
            f.write("## 📊 Summary Statistics\n\n")
            
            total_workflows = sum(r.workflow_count for r in self.all_results)
            total_actions = sum(r.action_count for r in self.all_results)
            unique_actions_set = set()
            
            for result in self.all_results:
                for action in result.actions:
                    unique_actions_set.add(action.full_action_ref)
            
            f.write(f"- **Total Repositories:** {len(self.all_results)}\n")
            f.write(f"- **Total Workflows:** {total_workflows}\n")
            f.write(f"- **Total Action Uses:** {total_actions}\n")
            f.write(f"- **Unique Actions:** {len(unique_actions_set)}\n")
            f.write(f"- **Workflow Calls (Reusable Workflows):** {len(self.all_workflow_calls)}\n\n")
            
            # Top actions
            f.write("## 🔝 Top 20 Most Used Actions\n\n")
            action_counts = defaultdict(int)
            for result in self.all_results:
                for action in result.actions:
                    action_counts[action.full_action_ref] += 1
            
            top_actions = sorted(action_counts.items(), key=lambda x: x[1], reverse=True)[:20]
            f.write("| Action | Uses |\n")
            f.write("|--------|------|\n")
            for action, count in top_actions:
                f.write(f"| {action} | {count} |\n")
            f.write("\n")
            
            # Workflow calls (dependency graph data)
            if self.all_workflow_calls:
                f.write("## 🔗 Workflow Dependencies (Reusable Workflows)\n\n")
                f.write(f"Total cross-repository workflow calls: **{len(self.all_workflow_calls)}**\n\n")
                
                # Group by target workflow
                calls_by_target = defaultdict(list)
                for call in self.all_workflow_calls:
                    target_key = f"{call.target_repo}/{call.target_workflow}"
                    calls_by_target[target_key].append(call)
                
                f.write("### Most Called Reusable Workflows\n\n")
                f.write("| Target Workflow | Called By | Count |\n")
                f.write("|-----------------|-----------|-------|\n")
                
                sorted_targets = sorted(calls_by_target.items(), key=lambda x: len(x[1]), reverse=True)
                for target, callers in sorted_targets[:15]:
                    unique_callers = len(set(c.source_repo for c in callers))
                    f.write(f"| {target} | {unique_callers} repos | {len(callers)} |\n")
                f.write("\n")
                
                # Dependency graph as mermaid diagram
                f.write("### Workflow Dependency Graph (Mermaid)\n\n")
                f.write("```mermaid\n")
                f.write("graph TD\n")
                
                # Limit to most significant dependencies to keep diagram readable
                significant_calls = []
                for target, callers in sorted_targets[:10]:
                    for caller in callers[:3]:
                        significant_calls.append(caller)
                
                # Build node maps to reuse IDs for same repos/workflows
                repo_id_map = {}  # Maps repo name to safe ID
                workflow_id_map = {}  # Maps workflow path to safe ID
                node_counter = 0
                added_edges = set()
                
                # First pass: create all unique nodes
                for call in significant_calls:
                    # Get or create ID for source repo
                    if call.source_repo not in repo_id_map:
                        repo_id_map[call.source_repo] = f"repo_{node_counter}"
                        node_counter += 1
                    
                    # Get or create ID for target workflow
                    workflow_key = f"{call.target_repo}/{call.target_workflow}"
                    if workflow_key not in workflow_id_map:
                        workflow_id_map[workflow_key] = f"wf_{node_counter}"
                        node_counter += 1
                
                # Write node definitions with full paths
                for repo, repo_id in repo_id_map.items():
                    f.write(f"    {repo_id}[\"{repo}\"]\n")
                
                for workflow, wf_id in workflow_id_map.items():
                    # Keep full path: owner/repo/.github/workflows/filename.yml
                    f.write(f"    {wf_id}[\"{workflow}\"]\n")
                
                # Write edges
                for call in significant_calls:
                    source_id = repo_id_map[call.source_repo]
                    workflow_key = f"{call.target_repo}/{call.target_workflow}"
                    target_id = workflow_id_map[workflow_key]
                    
                    edge_key = (source_id, target_id)
                    if edge_key not in added_edges:
                        f.write(f"    {source_id} -->|calls| {target_id}\n")
                        added_edges.add(edge_key)
                
                f.write("```\n\n")
            
            # Actions by repository
            f.write("## 📁 Actions by Repository\n\n")
            
            repos_with_actions = [r for r in self.all_results if r.action_count > 0]
            repos_with_actions.sort(key=lambda x: x.action_count, reverse=True)
            
            f.write("| Repository | Workflows | Actions | Unique Actions | Workflow Calls |\n")
            f.write("|------------|-----------|---------|-----------------|----------------|\n")
            
            for result in repos_with_actions[:30]:
                f.write(f"| {result.repository} | {result.workflow_count} | {result.action_count} | {result.unique_actions} | {len(result.workflow_calls)} |\n")
            
            if len(repos_with_actions) > 30:
                f.write(f"\n*... and {len(repos_with_actions) - 30} more repositories*\n\n")
            
            # Detailed action listings
            f.write("## 📋 All Actions Used\n\n")
            
            unique_actions_list = sorted(unique_actions_set)
            for action in unique_actions_list[:50]:
                repos_using = set()
                for result in self.all_results:
                    for act in result.actions:
                        if act.full_action_ref == action:
                            repos_using.add(result.repository)
                
                count = action_counts[action]
                f.write(f"### {action}\n")
                f.write(f"- **Uses:** {count}\n")
                f.write(f"- **Used by:** {len(repos_using)} repositories\n")
                f.write(f"- **Repositories:** {', '.join(sorted(repos_using)[:5])}")
                if len(repos_using) > 5:
                    f.write(f", ...")
                f.write(f"\n\n")
            
            if len(unique_actions_list) > 50:
                f.write(f"\n*... and {len(unique_actions_list) - 50} more actions*\n\n")
            
            # Errors
            errors = [r for r in self.all_results if r.error]
            if errors:
                f.write("## ⚠️ Analysis Errors\n\n")
                f.write("| Repository | Error |\n")
                f.write("|------------|-------|\n")
                for result in errors:
                    f.write(f"| {result.repository} | {result.error} |\n")
                f.write("\n")
            
            # Footer
            f.write("---\n")
            f.write("*Generated by GitHub Actions Analyzer*\n")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python3 detect_actions.py <organization> [--max-workers=10]")
        print("\nExamples:")
        print("  python3 detect_actions.py myorg")
        print("  python3 detect_actions.py myorg --max-workers=5")
        sys.exit(1)
    
    org = sys.argv[1]
    max_workers = 10
    
    # Parse options
    for arg in sys.argv[2:]:
        if arg.startswith('--max-workers='):
            max_workers = int(arg.split('=')[1])
    
    analyzer = ActionsAnalyzer(org, max_workers=max_workers)
    analyzer.run()


if __name__ == '__main__':
    main()
