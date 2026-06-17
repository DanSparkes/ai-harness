# core/git_provider.py
import subprocess
from pathlib import Path

class GitDiffProvider:
    """Extracts line-by-line diffs and modified file manifests, strictly excluding fixture noise."""
    def __init__(self, repo_dir: str):
        self.repo_dir = Path(repo_dir).resolve()

    def _run_git(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self.repo_dir,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            print(f"Git execution error: {e.stderr}")
            raise RuntimeError(f"Failed to execute git command: {' '.join(args)}")

    def get_diff(self, target_branch: str, source_branch: str) -> str:
        """Returns the raw patch diff, explicitly skipping any fixtures folders and uv.lock."""
        # Using Git pathspec exclusions ensures massive JSON/YAML data files are ignored natively
        return self._run_git([
            "diff",
            f"{target_branch}...{source_branch}",
            "--",
            ".",
            ":(exclude)**/fixtures/**",
            ":(exclude)fixtures/**",
            ":(exclude)uv.lock"
        ])

    def get_changed_files(self, target_branch: str, source_branch: str) -> list[str]:
        """Returns a clean list of files modified or added, omitting fixtures and uv.lock."""
        output = self._run_git([
            "diff",
            "--name-only",
            f"{target_branch}...{source_branch}",
            "--",
            ".",
            ":(exclude)**/fixtures/**",
            ":(exclude)fixtures/**",
            ":(exclude)uv.lock"
        ])
        return [line.strip() for line in output.splitlines() if line.strip()]
