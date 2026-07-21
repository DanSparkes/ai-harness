#!/usr/bin/env python3
"""
run_adr_review.py
Multi-pass ADR review harness. Splits the ADR into focused review dimensions,
runs each pass through a local or cloud model, then merges into a unified report.

Usage:
  # Single-pass review (quick, one model)
  python3 run_adr_review.py /path/to/adr.md -r /path/to/repo

  # Multi-pass review (local models, each dimension separately)
  python3 run_adr_review.py /path/to/adr.md --multi-pass -r /path/to/repo

  # Full context: repo + project conventions + MCP tools
  python3 run_adr_review.py /path/to/adr.md \
    -r /Users/dansparkes/memores/memores-api \
    -c project_contexts/memores-api.md \
    -m mcp_config.python.json

  # Use Gemini for review
  python3 run_adr_review.py /path/to/adr.md --engine gemini
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from core.agent import Agent
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import init_orchestrator
from core.parser import minify_markdown

AGENTS_DIR = Path(__file__).parent / "agents"
RUBRICS_DIR = Path(__file__).parent / "rubrics"
REPORTS_DIR = Path(__file__).parent / "reports"

# ── Model Configuration ───────────────────────────────────────────────────────
USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL = "gemini-2.5-flash"
LOCAL_MODEL = "ornith:35b"
LOCAL_JUDGE = os.getenv(
    "LOCAL_JUDGE", "hf.co/yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF:Q8_0"
)

REVIEW_MODEL = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
REVIEW_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
    if USE_GEMINI
    else "http://localhost:11434"
)
REVIEW_API_KEY = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

JUDGE_BASE_URL = "http://localhost:11434"
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.json")

_mcp_orch = None

# ── Dimension Prompts (for multi-pass mode) ───────────────────────────────────

DIMENSIONS = {
    "assumption_validity": {
        "name": "Assumption Validity",
        "prompt": (
            "Review ONLY the assumptions in this ADR. For each assumption:\n"
            "1. Is it testable before the decision point?\n"
            "2. Are there missing assumptions that would invalidate the approach if false?\n"
            "3. Do the 'Impact if False' entries accurately reflect real consequences?\n"
            "4. Are there hidden assumptions not explicitly listed?\n\n"
            "Output a numbered list of findings. Each finding must reference the "
            "exact assumption text from the ADR."
        ),
    },
    "alternative_completeness": {
        "name": "Alternative Completeness",
        "prompt": (
            "Review ONLY the alternatives considered section of this ADR.\n"
            "1. Were all reasonable alternatives evaluated?\n"
            "2. Are the 'Why not' dismissals justified with concrete evidence?\n"
            "3. Is there a hybrid alternative that combines strengths of rejected options?\n"
            "4. Was the chosen alternative compared against ALL decision criteria?\n\n"
            "Output a numbered list of findings. Reference specific alternative "
            "labels (A, B, C, etc.) from the ADR."
        ),
    },
    "decision_coherence": {
        "name": "Decision Coherence",
        "prompt": (
            "Review the coherence between the stated decision criteria and the chosen "
            "decision in this ADR.\n"
            "1. Does the chosen decision actually satisfy all 6 stated criteria?\n"
            "2. Are there contradictions between stated goals and implementation details?\n"
            "3. Is the decision truly reversible as claimed, or are there hidden lock-in effects?\n"
            "4. Does the migration plan have a realistic rollback that doesn't cut corners?\n\n"
            "Output a numbered list of findings. Quote specific sections of the ADR "
            "to support each finding."
        ),
    },
    "implementation_readiness": {
        "name": "Implementation Readiness",
        "prompt": (
            "Review the implementation details of this ADR for readiness.\n"
            "1. Are code snippets syntactically correct?\n"
            "2. Are file paths, class names, and method signatures plausible?\n"
            "3. Are there missing files or modules not listed?\n"
            "4. Is the migration plan ordered correctly (dependencies respected)?\n"
            "5. Are validation gates specific enough to be pass/fail?\n\n"
            "Output a numbered list of findings. Reference specific code snippets, "
            "file paths, or step numbers from the ADR."
        ),
    },
    "risk_coverage": {
        "name": "Risk Coverage",
        "prompt": (
            "Review the risk analysis in this ADR.\n"
            "1. Are all identified risks mitigated, or are some left unmitigated?\n"
            "2. Are there missing risk categories (data loss, rollback failure, coordination)?\n"
            "3. Do mitigation strategies reference specific tools, commands, or configs?\n"
            "4. Is the 'experiment failed' rollback trigger defined with measurable criteria?\n\n"
            "Output a numbered list of findings. Reference specific risk entries "
            "from the ADR's risk table."
        ),
    },
    "testability": {
        "name": "Testability",
        "prompt": (
            "Review the testing strategy in this ADR.\n"
            "1. Are the 4 test files listed sufficient to cover the behavioral change?\n"
            "2. Are there missing integration tests (end-to-end provider routing)?\n"
            "3. Is the validation gate for self-hosted infrastructure specific enough to automate?\n"
            "4. Can the data migration be tested in staging without production data?\n\n"
            "Output a numbered list of findings. Reference specific test file "
            "names and line counts from the ADR."
        ),
    },
}


# ── MCP Integration ───────────────────────────────────────────────────────────


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    cfg_path = config_path or MCP_CONFIG_PATH
    path = repo_path or os.environ.get("TARGET_REPO") or os.getcwd()
    orch = init_orchestrator(cfg_path, path)
    if orch:
        _mcp_orch = orch
    return orch


def build_mcp_context_block() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(tags=["architectural_rule"])


# ── Core Functions ────────────────────────────────────────────────────────────


def load_adr(adr_path: str) -> str:
    with open(adr_path, encoding="utf-8") as f:
        return f.read()


def extract_title(adr_text: str) -> str:
    match = re.search(r"^#\s+(ADR\s+\d+:?\s*.+)$", adr_text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return "ADR Review"


def load_project_context(project_context_path: str | None) -> str:
    if not project_context_path:
        return ""
    if not os.path.exists(project_context_path):
        print(f"  Warning: project context file not found: {project_context_path}")
        return ""
    with open(project_context_path, encoding="utf-8") as f:
        content = minify_markdown(f.read())
    print(f"  Loaded project context: {project_context_path}")
    return content


def build_repo_context(target_repo: str) -> str:
    """Scan the target repo for files and structure relevant to the ADR."""
    from core.parser import scan_file_tree, scan_files_by_keyword

    file_tree = scan_file_tree(target_repo)
    parts = ["=== PROJECT FILE TREE ==="]
    parts.append("\n".join(file_tree) if file_tree else "(empty)")

    # Scan for key files mentioned in ADR patterns
    keywords = [
        "ClaudeAPI",
        "PromptTemplate",
        "perform_claude_ai_job",
        "llm_provider",
        "anthropic_provider",
        "self_hosted_provider",
        "usage_tracker",
    ]
    for kw in keywords:
        matches = scan_files_by_keyword(target_repo, kw)
        if matches:
            for m in matches[:2]:
                parts.append(f"\n--- {m['file']} ---")
                parts.append(m["content"].rstrip()[:2000])

    return "\n".join(parts)


def build_full_context(
    adr_text: str, repo_context: str, project_context: str, mcp_context: str
) -> str:
    """Assemble all context blocks into a single prompt suffix."""
    parts = []
    if repo_context:
        parts.append(f"=== TARGET REPOSITORY CONTEXT ===\n{repo_context}")
    if project_context:
        parts.append(f"=== PROJECT CONVENTIONS ===\n{project_context}")
    if mcp_context:
        parts.append(
            f"=== MCP TOOL CONTEXT (git, memory, filesystem) ===\n{mcp_context}"
        )
    return "\n\n".join(parts)


def run_single_pass(
    adr_text: str, context_block: str = "", model: str | None = None
) -> str:
    """Run a single comprehensive review pass."""
    reviewer = Agent(
        name="ADR Reviewer",
        system_prompt=(AGENTS_DIR / "adr_reviewer.md").read_text(),
        model_name=model or REVIEW_MODEL,
        base_url=REVIEW_BASE_URL,
        api_key=REVIEW_API_KEY,
    )

    user_prompt = f"Review this ADR:\n\n{adr_text}"
    if context_block:
        user_prompt += f"\n\n{context_block}"

    return reviewer.execute(user_prompt)


def run_multi_pass(adr_text: str, context_block: str = "") -> str:
    """Run each dimension as a separate pass, then merge."""
    reviewer = Agent(
        name="ADR Reviewer",
        system_prompt=(AGENTS_DIR / "adr_reviewer.md").read_text(),
        model_name=REVIEW_MODEL,
        base_url=REVIEW_BASE_URL,
        api_key=REVIEW_API_KEY,
    )

    dimension_results = {}
    for dim_key, dim in DIMENSIONS.items():
        print(f"  Running dimension: {dim['name']}...")
        user_prompt = f"{dim['prompt']}\n\nADR TEXT:\n\n{adr_text}"
        if context_block:
            user_prompt += f"\n\n{context_block}"

        result = reviewer.execute(user_prompt, temperature=0.2)
        dimension_results[dim_key] = result

    # Merge all dimensions into unified report
    merged = "# ADR Review (Multi-Pass)\n\n"
    for dim_key, dim in DIMENSIONS.items():
        merged += f"## {dim['name']}\n\n{dimension_results[dim_key]}\n\n"

    return merged


def score_review(review_text: str, adr_text: str) -> dict:
    """Score the review against the ADR review rubric."""
    evaluator = AutomatedEvaluator(judge_model=LOCAL_JUDGE, base_url=JUDGE_BASE_URL)
    rubric_path = str(RUBRICS_DIR / "adr_review_rubric.json")
    return evaluator.grade_run(review_text, rubric_path, context=adr_text[:3000])


def save_report(adr_title: str, review_text: str, scores: dict, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", adr_title.lower()).strip("_")[:60]
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"adr_review_{slug}_{ts}.md"
    path = os.path.join(output_dir, filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write("# ADR Review Report\n\n")
        f.write(f"**Source:** {adr_title}\n")
        f.write(f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"**Model:** {REVIEW_MODEL}\n\n")

        if scores:
            f.write("## Rubric Scores\n\n")
            f.write("| Metric | Score (1-5) | Weight |\n")
            f.write("|--------|-------------|--------|\n")
            total = 0.0
            weight_sum = 0.0
            for metric, score in scores.get("scores", {}).items():
                weight = scores.get("weights", {}).get(metric, "?")
                f.write(f"| {metric} | {score} | {weight} |\n")
                if isinstance(score, (int, float)) and isinstance(weight, (int, float)):
                    total += score * weight / 100
                    weight_sum += weight / 100
            if weight_sum > 0:
                f.write(f"| **Weighted Total** | **{total / weight_sum:.1f}** | |\n\n")
            else:
                f.write("\n")

        f.write("## Review\n\n")
        f.write(review_text)

    return path


def main():
    parser = argparse.ArgumentParser(description="ADR Review Harness")
    parser.add_argument("adr", help="Path to the ADR markdown file")
    parser.add_argument(
        "--multi-pass",
        action="store_true",
        help="Run each review dimension as a separate pass (better for local models)",
    )
    parser.add_argument(
        "--repo",
        "-r",
        default=None,
        help="Path to target repository (overrides TARGET_REPO env var)",
    )
    parser.add_argument(
        "--project-context",
        "-c",
        default=None,
        help="Path to project context file (markdown) with domain conventions",
    )
    parser.add_argument(
        "--mcp-config",
        "-m",
        default=None,
        help="Path to MCP server config JSON (overrides MCP_CONFIG env var)",
    )
    parser.add_argument(
        "--engine",
        choices=["ollama", "gemini"],
        default="ollama" if not USE_GEMINI else "gemini",
        help="LLM backend (default: from USE_GEMINI env var)",
    )
    parser.add_argument(
        "--model", default=None, help="Override review model (default: from env/config)"
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=str(REPORTS_DIR),
        help="Output directory for the review report",
    )
    parser.add_argument(
        "--no-score",
        action="store_true",
        help="Skip rubric scoring (faster, useful for local-only review)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.adr):
        print(f"Error: ADR file not found: {args.adr}")
        sys.exit(1)

    print(f"Loading ADR: {args.adr}")
    adr_text = load_adr(args.adr)
    adr_title = extract_title(adr_text)
    print(f"  ADR Title: {adr_title}")
    print(f"  ADR Length: {len(adr_text)} chars")

    # Resolve model
    global REVIEW_MODEL, REVIEW_BASE_URL, REVIEW_API_KEY
    if args.model:
        REVIEW_MODEL = args.model
    elif args.engine == "gemini":
        REVIEW_MODEL = CLOUD_MODEL
        REVIEW_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
        REVIEW_API_KEY = os.getenv("GEMINI_API_KEY")
    else:
        REVIEW_MODEL = LOCAL_MODEL
        REVIEW_BASE_URL = "http://localhost:11434"
        REVIEW_API_KEY = None

    print(f"  Review Model: {REVIEW_MODEL}")
    print(f"  Mode: {'Multi-pass' if args.multi_pass else 'Single-pass'}")

    # Resolve target repo
    target_repo = args.repo or os.environ.get("TARGET_REPO")

    # Initialize MCP
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)
    mcp_context = ""
    if target_repo:
        orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
        if orch:
            mcp_context = build_mcp_context_block()
            print("  MCP Workbench: active (git + memory + filesystem)")
        else:
            print("  MCP Workbench: not configured")
    else:
        print("  MCP Workbench: skipped (no --repo specified)")

    # Load project context
    project_context = load_project_context(args.project_context)

    # Build repo context
    repo_context = ""
    if target_repo:
        if os.path.exists(target_repo):
            print(f"  Scanning repo: {target_repo}")
            repo_context = build_repo_context(target_repo)
        else:
            print(f"  Warning: target repo not found: {target_repo}")

    # Assemble full context
    context_block = build_full_context(
        adr_text, repo_context, project_context, mcp_context
    )

    # Run review
    print("\nRunning ADR review...")
    t0 = time.time()
    if args.multi_pass:
        review_text = run_multi_pass(adr_text, context_block)
    else:
        review_text = run_single_pass(adr_text, context_block)
    elapsed = time.time() - t0
    print(f"  Review completed in {elapsed:.1f}s")

    # Score
    scores = {}
    if not args.no_score:
        print("  Scoring review against rubric...")
        try:
            scores = score_review(review_text, adr_text)
            print(f"  Scores: {json.dumps(scores.get('scores', {}), indent=2)}")
        except Exception as e:
            print(f"  Warning: Scoring failed: {e}")

    # Save
    path = save_report(adr_title, review_text, scores, args.output_dir)
    print(f"\nReview saved: {path}")
    print("\nDone. Review the report and address critical gaps before proceeding.")


if __name__ == "__main__":
    main()
