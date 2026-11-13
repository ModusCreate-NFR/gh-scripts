#!/usr/bin/env python3
"""
Terraform Module Dependency Detector

Scans all repositories in a GitHub organization for Terraform modules
and identifies cross-repository module dependencies.

Features:
- Parallel repository scanning (configurable workers)
- Terraform HCL parsing for module definitions
- Git source URL extraction and parsing
- Dependency relationship tracking
- Multiple report formats: JSON, CSV, Markdown with Mermaid diagrams
- Graceful error handling and logging

Usage:
    python3 detect_terraform_deps.py <organization> [--workers=5]

Example:
    python3 detect_terraform_deps.py ModusCreate-Perdigao-GHAS-Playground
"""

import os
import sys
import json
import csv
import re
import subprocess
import logging
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class TerraformModule:
    """Represents a Terraform module definition"""
    name: str
    source: str
    module_path: str
    line_number: int
    
    def is_git_source(self) -> bool:
        return self.source.startswith('git::')
    
    def is_github_source(self) -> bool:
        return 'github.com' in self.source


@dataclass
class ParsedGitSource:
    """Parsed Git source URL"""
    protocol: str
    owner: str
    repo: str
    subpath: str = ""
    ref: str = ""
    original: str = ""


@dataclass
class RepositoryTerraformDeps:
    """Terraform dependencies for a repository"""
    repo_name: str
    repo_url: str
    terraform_files: List[str] = field(default_factory=list)
    modules: List[TerraformModule] = field(default_factory=list)
    local_modules: List[TerraformModule] = field(default_factory=list)
    registry_modules: List[TerraformModule] = field(default_factory=list)
    github_dependencies: Dict[str, List[str]] = field(default_factory=dict)
    error: str = ""
    
    def has_terraform(self) -> bool:
        return len(self.terraform_files) > 0
    
    def external_module_count(self) -> int:
        return len(self.local_modules)


@dataclass
class ModuleDependency:
    """Cross-repository module dependency"""
    source_repo: str
    source_owner: str
    target_repo: str
    target_owner: str
    module_name: str
    module_source: str
    ref: str = ""


class TerraformDepsAnalyzer:
    """Main analyzer for Terraform module dependencies"""
    
    def __init__(self, org: str, workers: int = 5):
        self.org = org
        self.workers = workers
        self.results: List[RepositoryTerraformDeps] = []
        self.dependencies: List[ModuleDependency] = []
        
    def run(self):
        """Main execution flow"""
        logger.info(f"Starting Terraform dependency analysis for {self.org}")
        
        repos = self._get_org_repos()
        logger.info(f"Found {len(repos)} repositories")
        
        self.results = self._analyze_repos_parallel(repos)
        
        logger.info(f"Analysis complete. Processing {len(self.results)} results")
        
        self._identify_dependencies()
        self.generate_reports()
        
    def _get_org_repos(self) -> List[Dict]:
        """Fetch all repositories in organization"""
        repos = []
        
        cmd = [
            'gh', 'repo', 'list', self.org,
            '--json', 'name,url,nameWithOwner',
            '--jq', '.[]'
        ]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to fetch repos: {result.stderr}")
                return repos
            
            for line in result.stdout.strip().split('\n'):
                if line:
                    repos.append(json.loads(line))
            
        except subprocess.TimeoutExpired:
            logger.error("Timeout fetching repositories")
        
        return repos
    
    def _analyze_repos_parallel(self, repos: List[Dict]) -> List[RepositoryTerraformDeps]:
        """Analyze repositories in parallel"""
        results = []
        
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self._analyze_repo, repo): repo
                for repo in repos
            }
            
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        if result.has_terraform():
                            logger.info(
                                f"[SUCCESS] {repo['nameWithOwner']}: "
                                f"{len(result.terraform_files)} .tf files, "
                                f"{len(result.modules)} modules, "
                                f"{len(result.local_modules)} external"
                            )
                        else:
                            logger.info(f"[INFO] {repo['nameWithOwner']}: No Terraform files")
                except Exception as e:
                    logger.error(f"[ERROR] {repo['nameWithOwner']}: {str(e)}")
        
        return results
    
    def _analyze_repo(self, repo: Dict) -> RepositoryTerraformDeps:
        """Analyze single repository for Terraform dependencies"""
        repo_name = repo['nameWithOwner']
        repo_url = repo['url']
        
        result = RepositoryTerraformDeps(
            repo_name=repo_name,
            repo_url=repo_url
        )
        
        temp_dir = f"/tmp/{repo['name']}-tf-{int(datetime.now().timestamp())}"
        
        try:
            # Clone shallow (just HEAD, fast!)
            subprocess.run(
                ['git', 'clone', '--depth=1', '--single-branch', repo_url, temp_dir],
                capture_output=True,
                timeout=45
            )
            
            # Find .tf files
            from pathlib import Path
            tf_files = list(Path(temp_dir).rglob('*.tf'))
            
            if not tf_files:
                return result
            
            result.terraform_files = [str(f.relative_to(temp_dir)) for f in tf_files]
            
            # Parse each .tf file
            for tf_file in tf_files[:20]:  # Limit to first 20
                try:
                    content = tf_file.read_text(errors='ignore')
                    if content:
                        modules = self._parse_terraform_content(content, repo_name, str(tf_file.relative_to(temp_dir)))
                        result.modules.extend(modules)
                        
                        # Categorize modules
                        for module in modules:
                            if module.is_git_source():
                                result.local_modules.append(module)
                            elif 'registry.terraform.io' in module.source or '/' not in module.source:
                                result.registry_modules.append(module)
                                
                                # Track GitHub dependencies
                                if module.is_github_source():
                                    parsed = self._parse_git_source(module.source)
                                    if parsed:
                                        dep_key = f"{parsed.owner}/{parsed.repo}"
                                        if dep_key not in result.github_dependencies:
                                            result.github_dependencies[dep_key] = []
                                        result.github_dependencies[dep_key].append(module.name)
                
                except Exception as e:
                    logger.debug(f"Error parsing {tf_file}: {str(e)}")
            
        except subprocess.TimeoutExpired:
            result.error = "Clone timeout (45s)"
        except Exception as e:
            result.error = str(e)
        finally:
            # Cleanup
            subprocess.run(['rm', '-rf', temp_dir], capture_output=True)
        
        return result
    
    def _parse_terraform_content(self, content: str, repo_name: str, file_path: str) -> List[TerraformModule]:
        """Parse Terraform content for module definitions"""
        modules = []
        
        try:
            # Regex to find module blocks
            pattern = r'module\s+"([^"]+)"\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}'
            
            for match in re.finditer(pattern, content, re.MULTILINE | re.DOTALL):
                module_name = match.group(1)
                module_body = match.group(2)
                
                # Extract source
                source_match = re.search(r'source\s*=\s*"([^"]+)"', module_body)
                if source_match:
                    source = source_match.group(1)
                    
                    # Get line number
                    line_number = content[:match.start()].count('\n') + 1
                    
                    module = TerraformModule(
                        name=module_name,
                        source=source,
                        module_path=file_path,
                        line_number=line_number
                    )
                    
                    modules.append(module)
        
        except Exception as e:
            logger.debug(f"Error parsing {file_path}: {str(e)}")
        
        return modules
    
    def _parse_git_source(self, source: str) -> ParsedGitSource:
        """Parse Git source URL to extract components"""
        try:
            # Remove git:: prefix
            if source.startswith('git::'):
                source = source[5:]
            
            # Extract ref if present
            ref = ""
            if '?ref=' in source:
                source, ref = source.split('?ref=')
            
            # Handle different URL formats
            if source.startswith('https://'):
                match = re.match(r'https://github\.com/([^/]+)/([^/]+)(?://(.+))?', source)
                if match:
                    return ParsedGitSource(
                        protocol='https',
                        owner=match.group(1),
                        repo=match.group(2).replace('.git', ''),
                        subpath=match.group(3) or "",
                        ref=ref,
                        original=source
                    )
            
            elif source.startswith('ssh://'):
                match = re.match(r'ssh://git@github\.com/([^/]+)/([^/]+)(?:\.git)?(?://(.+))?', source)
                if match:
                    return ParsedGitSource(
                        protocol='ssh',
                        owner=match.group(1),
                        repo=match.group(2).replace('.git', ''),
                        subpath=match.group(3) or "",
                        ref=ref,
                        original=source
                    )
            
            elif '@github.com:' in source:
                match = re.match(r'git@github\.com:([^/]+)/(.+?)(?:\.git)?(?://(.+))?$', source)
                if match:
                    return ParsedGitSource(
                        protocol='ssh',
                        owner=match.group(1),
                        repo=match.group(2).replace('.git', ''),
                        subpath=match.group(3) or "",
                        ref=ref,
                        original=source
                    )
        
        except Exception as e:
            logger.debug(f"Error parsing git source {source}: {str(e)}")
        
        return None
    
    def _identify_dependencies(self):
        """Identify cross-repository module dependencies"""
        for result in self.results:
            for module in result.local_modules:
                parsed = self._parse_git_source(module.source)
                if parsed:
                    dep = ModuleDependency(
                        source_repo=result.repo_name.split('/')[-1],
                        source_owner=result.repo_name.split('/')[0],
                        target_repo=parsed.repo,
                        target_owner=parsed.owner,
                        module_name=module.name,
                        module_source=module.source,
                        ref=parsed.ref
                    )
                    self.dependencies.append(dep)
    
    def generate_reports(self):
        """Generate JSON, CSV, and Markdown reports"""
        os.makedirs('reports', exist_ok=True)
        
        timestamp = int(datetime.now().timestamp())
        base_name = f"reports/{self.org}-terraform-deps-{timestamp}"
        
        # JSON report
        json_path = f"{base_name}.json"
        self._generate_json_report(json_path)
        logger.info(f"[SUCCESS] JSON report: {json_path}")
        
        # CSV report
        csv_path = f"{base_name}.csv"
        self._generate_csv_report(csv_path)
        logger.info(f"[SUCCESS] CSV report: {csv_path}")
        
        # Markdown report
        md_path = f"{base_name}.md"
        self._generate_markdown_report(md_path)
        logger.info(f"[SUCCESS] Markdown report: {md_path}")
    
    def _generate_json_report(self, path: str):
        """Generate JSON report"""
        data = {
            'organization': self.org,
            'generated': datetime.now().isoformat(),
            'repositories_analyzed': len(self.results),
            'repositories_with_terraform': sum(1 for r in self.results if r.has_terraform()),
            'total_terraform_files': sum(len(r.terraform_files) for r in self.results),
            'total_modules': sum(len(r.modules) for r in self.results),
            'external_module_references': len(self.dependencies),
            'dependencies': [asdict(d) for d in self.dependencies],
            'repositories': [
                {
                    'name': r.repo_name,
                    'url': r.repo_url,
                    'terraform_files': r.terraform_files,
                    'modules_count': len(r.modules),
                    'external_modules_count': len(r.local_modules),
                    'registry_modules_count': len(r.registry_modules),
                    'github_dependencies': r.github_dependencies,
                    'error': r.error
                }
                for r in self.results
            ]
        }
        
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    
    def _generate_csv_report(self, path: str):
        """Generate CSV report for module dependencies"""
        with open(path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Source Repo',
                'Source Owner',
                'Target Repo',
                'Target Owner',
                'Module Name',
                'Module Source',
                'Git Ref'
            ])
            
            for dep in self.dependencies:
                writer.writerow([
                    dep.source_repo,
                    dep.source_owner,
                    dep.target_repo,
                    dep.target_owner,
                    dep.module_name,
                    dep.module_source,
                    dep.ref
                ])
    
    def _generate_markdown_report(self, path: str):
        """Generate comprehensive Markdown report"""
        now = datetime.now().isoformat()
        
        with open(path, 'w') as f:
            # Header
            f.write("# Terraform Module Dependencies Analysis\n\n")
            f.write(f"**Organization:** {self.org}\n")
            f.write(f"**Generated:** {now} UTC\n")
            f.write(f"**Repositories Analyzed:** {len(self.results)}\n\n")
            
            # Summary statistics
            f.write("## 📊 Summary Statistics\n\n")
            repos_with_tf = sum(1 for r in self.results if r.has_terraform())
            total_tf_files = sum(len(r.terraform_files) for r in self.results)
            total_modules = sum(len(r.modules) for r in self.results)
            total_external = sum(len(r.local_modules) for r in self.results)
            total_registry = sum(len(r.registry_modules) for r in self.results)
            
            f.write(f"- **Total Repositories:** {len(self.results)}\n")
            f.write(f"- **Repositories with Terraform:** {repos_with_tf}\n")
            f.write(f"- **Total Terraform Files:** {total_tf_files}\n")
            f.write(f"- **Total Modules Defined:** {total_modules}\n")
            f.write(f"- **External Git Modules:** {total_external}\n")
            f.write(f"- **Registry Modules:** {total_registry}\n")
            f.write(f"- **Cross-Repo Dependencies:** {len(self.dependencies)}\n\n")
            
            # Top modules sources
            if self.dependencies:
                f.write("## 🔝 Most Reused External Modules\n\n")
                
                module_usage = {}
                for dep in self.dependencies:
                    key = f"{dep.target_owner}/{dep.target_repo}"
                    if key not in module_usage:
                        module_usage[key] = []
                    module_usage[key].append(dep.module_name)
                
                sorted_modules = sorted(
                    module_usage.items(),
                    key=lambda x: len(x[1]),
                    reverse=True
                )[:10]
                
                f.write("| External Module Source | Used By | Count |\n")
                f.write("|--------|---------|-------|\n")
                for source, uses in sorted_modules:
                    unique_repos = len(set(d.source_repo for d in self.dependencies 
                                         if f"{d.target_owner}/{d.target_repo}" == source))
                    f.write(f"| {source} | {unique_repos} repos | {len(uses)} |\n")
                f.write("\n")
            
            # Dependencies graph
            if self.dependencies:
                f.write("## 🔗 Terraform Module Dependencies\n\n")
                
                # Build repo maps for Mermaid
                repo_id_map = {}
                mod_id_map = {}
                counter = 0
                
                for dep in self.dependencies[:20]:  # Limit to top 20 for readability
                    source_key = dep.source_repo
                    if source_key not in repo_id_map:
                        repo_id_map[source_key] = f"repo_{counter}"
                        counter += 1
                    
                    target_key = f"{dep.target_owner}/{dep.target_repo}"
                    if target_key not in mod_id_map:
                        mod_id_map[target_key] = f"mod_{counter}"
                        counter += 1
                
                f.write("```mermaid\n")
                f.write("graph TD\n")
                
                # Write node definitions with full paths
                for repo, repo_id in repo_id_map.items():
                    f.write(f"    {repo_id}[\"{repo}\"]\n")
                
                for mod_source, mod_id in mod_id_map.items():
                    f.write(f"    {mod_id}[\"📦 {mod_source}\"]\n")
                
                # Write edges
                added_edges = set()
                for dep in self.dependencies[:20]:
                    source_id = repo_id_map.get(dep.source_repo)
                    target_id = mod_id_map.get(f"{dep.target_owner}/{dep.target_repo}")
                    
                    if source_id and target_id:
                        edge_key = (source_id, target_id)
                        if edge_key not in added_edges:
                            label = f"{dep.module_name}"
                            if dep.ref:
                                # Use colon separator to avoid Mermaid parse errors with parentheses
                                label += f": {dep.ref}"
                            # Escape pipe characters in label
                            label = label.replace("|", " - ")
                            f.write(f"    {source_id} -->|{label}| {target_id}\n")
                            added_edges.add(edge_key)
                
                f.write("```\n\n")
            
            # Repositories with external modules
            f.write("## 📁 Repositories Using External Modules\n\n")
            
            repos_with_deps = [r for r in self.results if r.external_module_count() > 0]
            
            if repos_with_deps:
                f.write("| Repository | .tf Files | Modules | External | Registry |\n")
                f.write("|------------|-----------|---------|----------|----------|\n")
                for r in sorted(repos_with_deps, 
                               key=lambda x: x.external_module_count(), 
                               reverse=True):
                    f.write(f"| {r.repo_name} | {len(r.terraform_files)} | "
                           f"{len(r.modules)} | {len(r.local_modules)} | "
                           f"{len(r.registry_modules)} |\n")
                f.write("\n")
            else:
                f.write("No repositories with external Terraform modules found.\n\n")
            
            # Detailed dependencies table
            if self.dependencies:
                f.write("## 🔗 All Module Dependencies\n\n")
                f.write("| Source Repo | Module Name | Target Module | Ref |\n")
                f.write("|-------------|------------|----------------|-----|\n")
                
                for dep in sorted(self.dependencies, 
                                 key=lambda x: (x.source_repo, x.module_name)):
                    target = f"{dep.target_owner}/{dep.target_repo}"
                    f.write(f"| {dep.source_repo} | {dep.module_name} | {target} | "
                           f"{dep.ref or 'N/A'} |\n")
                f.write("\n")
            
            # Footer
            f.write("---\n")
            f.write("*Generated by Terraform Dependency Analyzer*\n")


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <organization> [--workers=5]")
        print(f"Example: {sys.argv[0]} ModusCreate-Perdigao-GHAS-Playground")
        sys.exit(1)
    
    org = sys.argv[1]
    workers = 5
    
    # Parse optional workers argument
    for arg in sys.argv[2:]:
        if arg.startswith('--workers='):
            workers = int(arg.split('=')[1])
    
    try:
        analyzer = TerraformDepsAnalyzer(org, workers=workers)
        analyzer.run()
    except KeyboardInterrupt:
        logger.info("Analysis interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
