# core/git_provider.py
import subprocess
from pathlib import Path

class GitDiffProvider:
    """Extracts line-by-line diffs and modified file manifests, strictly excluding fixture noise."""
    def __init__(self, repo_dir: str):
        self.repo_dir = Path(repo_dir).resolve()

    def _resolve_ref(self, ref: str) -> str:
        """Resolve a branch name, falling back to origin/<ref> if the bare ref doesn't exist."""
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", ref],
                cwd=self.repo_dir, capture_output=True, text=True, check=True
            )
            return ref
        except subprocess.CalledProcessError:
            pass
        origin_ref = f"origin/{ref}"
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", origin_ref],
                cwd=self.repo_dir, capture_output=True, text=True, check=True
            )
            print(f"   [Git] '{ref}' not found locally, using '{origin_ref}'")
            return origin_ref
        except subprocess.CalledProcessError:
            return ref  # let it fail naturally with the original name

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
        target = self._resolve_ref(target_branch)
        source = self._resolve_ref(source_branch)
        return self._run_git([
            "diff",
            f"{target}...{source}",
            "--",
            ".",
            ":(exclude)**/fixtures/**",
            ":(exclude)fixtures/**",
            ":(exclude)uv.lock",
            ":(exclude)*.md"
        ])

    def get_changed_files(self, target_branch: str, source_branch: str) -> list[str]:
        """Returns a clean list of files modified or added, omitting fixtures and uv.lock."""
        target = self._resolve_ref(target_branch)
        source = self._resolve_ref(source_branch)
        output = self._run_git([
            "diff",
            "--name-only",
            f"{target}...{source}",
            "--",
            ".",
            ":(exclude)**/fixtures/**",
            ":(exclude)fixtures/**",
            ":(exclude)uv.lock",
            ":(exclude)*.md"
        ])
        return [line.strip() for line in output.splitlines() if line.strip()]
