#!/usr/bin/env python3
"""
generate_feature_plan.py
Takes a brief feature prompt, expands it into a detailed .md architectural report
(with human review), and produces a structured .json pipeline consumable by
new_feature_harness.py. Optionally uses an MCP workbench for richer codebase
exploration (git history, persistent memory, filesystem search).

Usage:
  # Generate a new feature plan
  python3 generate_feature_plan.py \
    --prompt "Throttle Public Onboarding Endpoints..." \
    --name throttle-onboarding \
    --target-repo /path/to/repo \
    --mcp-config mcp_config.json

  # Re-extract pipeline after editing the .md report
  python3 generate_feature_plan.py --update reports/throttle_onboarding.md
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from typing import Any

# ── Defaults ──────────────────────────────────────────────────────────────────
from core.agent import Agent
from core.mcp_orchestrator import init_orchestrator

os.environ.setdefault("OLLAMA_MLX", "1")

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

ARCHITECT_MODEL = "qwen3.6:latest"
GEMINI_MODEL = "gemini-2.5-flash"

# MCP integration
_mcp_orchestrator = None


ARCHITECT_SYSTEM_PROMPT = """\
You are a Staff Backend Architect designing an implementation plan for a Django REST Framework feature. You have access to an MCP tool workbench with servers for filesystem exploration, git history, persistent memory, documentation lookup, web search, and structured reasoning.

When the prompt includes an "=== MCP-AUGMENTED CONTEXT ===" section, use the git history and memory context to understand the project's evolution and existing architectural rules before proposing changes. Cross-reference all file paths against the provided codebase scan — do not invent paths.

Given a brief feature prompt and a codebase context scan, produce a detailed implementation report in markdown with the following sections:

## 1. Codebase Target Map
List the exact files, classes, and URL routes that need to be created or modified. All file paths MUST come from the provided codebase scan — do not invent paths. When the prompt asks to update "all" tasks of a category, include EVERY matching file from the scan, not a subset.

## 2. Architecture & Design
Describe the approach in detail. Include code snippets for key patterns (e.g. a custom permission class, throttle configuration, serializer change). Explain how the pieces fit together.

CRITICAL: Code snippets must be syntactically correct Python.

## 3. Risk Assessment & Mitigations
Identify potential issues (backwards compatibility, migration risk, performance, security) and how to mitigate them.

## 4. Implementation Pipeline
Provide a step-by-step execution plan as a JSON code block. Each step represents one file to create or modify. The JSON must be an object with this structure:

{
  "feature_name": "short feature name",
  "target_workspace": "/path/to/repo",
  "pipeline": [
    {
      "step": 1,
      "name": "short step name",
      "target_file": "relative/path/to/file.py",
      "task": "detailed implementation instruction — class names, methods, imports, exactly what to change.",
      "assigned_agent": "Engineer",
      "auditor_agent": "QA_Tester",
      "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"],
      "max_attempts": 4
    }
  ]
}

Rules for the pipeline:
- Every target_file, function name, class name, and decorator in instructions MUST exactly match what appears in the codebase scan. Do not invent or rename them.
- For Celery task steps: the function name with @shared_task is the one to modify, not a plain helper function in the same file. Check the CELERY TASK DECORATORS section to verify.
- Steps must be ordered so earlier steps create dependencies that later steps rely on.
- Instructions must be precise and correct. Do not say "import it at the top" for a settings constant. Each instruction must be a single coherent paragraph about exactly what to add/change.
- Include both new files and modifications to existing files.
- Instructions must match existing codebase patterns (decorators, class hierarchies, import styles).
- When the prompt says to update "all" tasks of a kind, include EVERY matching file from the CELERY TASK DECORATORS section, not just the ones explicitly listed in the prompt.
- Use "target_workspace" as a placeholder — it will be replaced at runtime.

### Retry Safety Check — OVERRIDES ALL OTHER RULES
If the plan involves adding autoretry_for to a Celery task, you MUST check that task's code (shown in the file matches section) for error handlers that modify state before re-raising.

AUTORETRY_FOR IS NOT SAFE if the task's except block does this pattern:
1. Sets status = ERROR (or similar state field) on a model
2. Calls .save() on that model
3. Then re-raises the exception

When Celery retries this task, it re-executes from the top. The first thing the task does is check `if status != PENDING: raise ValueError` — which immediately fails because status is still ERROR from the previous attempt. Every retry hits this guard and permanently fails.

ACTION REQUIRED: For any task with this pattern, you MUST either:
- Option A: Remove the state-modifying code from the error handler entirely. Let the task fail cleanly before setting status=ERROR. Only set status=ERROR on the final failure (after all retries exhausted).
- Option B: Use tenacity retry on just the API call inside the task body instead of Celery-level autoretry_for. This keeps the existing error handler intact.

Whichever option you choose, explain the decision clearly in the instructions and risk section.

Output ONLY the report with the pipeline JSON embedded in a code block at the end of section 4.
"""


# ── MCP Integration ───────────────────────────────────────────────────────────

def init_mcp_orchestrator(config_path: str, target_repo: str | None = None):
    global _mcp_orchestrator
    if _mcp_orchestrator is not None:
        return _mcp_orchestrator
    if not target_repo:
        return None
    orch = init_orchestrator(config_path, target_repo)
    if orch:
        _mcp_orchestrator = orch
    return orch


def get_mcp_context(args: argparse.Namespace) -> str:
    if not args.mcp_config:
        return ""
    target = getattr(args, "target_repo", None)
    orch = init_mcp_orchestrator(args.mcp_config, target)
    if not orch:
        return ""
    parts = ["=== MCP-AUGMENTED CONTEXT ==="]
    git_block = orch.build_git_context(max_count=15)
    if git_block:
        parts.append(git_block)
    memory = orch.recall_tagged(tags=["architectural_rule"])
    if memory:
        parts.append(f"Architectural Rules:\n{memory}")
    memory = orch.recall_tagged(tags=["active", "campaign_complete"])
    if memory:
        parts.append(f"Active Campaign Context:\n{memory}")
    orch.stop()
    return "\n".join(parts)


# ── Codebase Scanner ──────────────────────────────────────────────────────────

def build_codebase_context(args: argparse.Namespace) -> str:
    """Build a codebase context block for the LLM prompt from the target repo."""
    from core.parser import (
        scan_file_tree, scan_celery_tasks,
        scan_files_by_keyword, scan_files_by_pattern, format_scan_results,
        DjangoTopographer,
    )

    file_tree = scan_file_tree(args.target_repo)
    celery_tasks = scan_celery_tasks(args.target_repo)

    # Extract meaningful keywords from the prompt
    keywords = re.findall(r"[\w_]+", args.prompt)
    keywords = sorted(set(k for k in keywords if len(k) > 3), key=lambda k: -len(k))[:5]
    keyword_matches = []
    for kw in keywords:
        keyword_matches.extend(scan_files_by_keyword(args.target_repo, kw))

    # Also scan for common target patterns mentioned in the prompt
    pattern_matches = scan_files_by_pattern(args.target_repo, keywords)

    # Always include content for Celery task files (so the LLM can see error handlers)
    task_files = sorted(set(t["file"] for t in celery_tasks))
    for tf in task_files:
        if not any(tf in m["file"] for m in keyword_matches + pattern_matches):
            task_content = scan_files_by_keyword(args.target_repo, tf.replace(".py", "").split("/")[-1])
            pattern_matches.extend(task_content)

    # Add structured Django topology for views/serializers
    topographer = DjangoTopographer(args.target_repo)
    topology = topographer.scan_project()

    base = format_scan_results(file_tree, celery_tasks, keyword_matches, pattern_matches)

    if topology.get("views") or topology.get("serializers"):
        base += "\n\n=== DJANGO VIEWS & SERIALIZERS ==="
        if topology["views"]:
            base += "\nViews:\n"
            for v in topology["views"]:
                base += f"  {v['relative_path']} :: {v['class']} ({', '.join(v['methods'])})\n"
        if topology["serializers"]:
            base += "\nSerializers:\n"
            for s in topology["serializers"]:
                base += f"  {s['relative_path']} :: {s['class']} fields={s['fields']}\n"

    return base


def get_gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")


def build_agent(engine: str, model: str, system_prompt: str, num_ctx: int = 65536) -> Agent:
    is_gemini = engine == "gemini"
    api_key = get_gemini_api_key() if is_gemini else None
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai" if is_gemini else "http://localhost:11434"
    return Agent(
        name="Architect",
        system_prompt=system_prompt,
        model_name=model,
        base_url=base_url,
        api_key=api_key,
        num_ctx=num_ctx,
    )


def extract_pipeline_json(markdown_text: str) -> dict[str, Any] | None:
    """
    Find the first JSON code block inside the ## Implementation Pipeline section
    and parse it. Returns the parsed dict or None.
    """
    # Split on the pipeline section header
    sections = re.split(r"^##\s+4\.?\s*Implementation Pipeline\s*$", markdown_text, flags=re.MULTILINE)
    if len(sections) < 2:
        sections = re.split(r"^##\s+Implementation Pipeline\s*$", markdown_text, flags=re.MULTILINE)
    if len(sections) < 2:
        print("Warning: Could not find '## Implementation Pipeline' section in report.")
        return None

    pipeline_section = sections[1]

    json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", pipeline_section, re.DOTALL)
    if not json_blocks:
        print("Warning: No JSON code block found in Implementation Pipeline section.")
        return None

    for block in json_blocks:
        block = block.strip()
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue

    print("Warning: Found JSON block(s) in pipeline section but none parsed successfully.")
    return None


def build_feature_prompt(feature_prompt: str, codebase_context: str, target_repo: str, mcp_context: str = "") -> str:
    extra = f"\n{mcp_context}\n" if mcp_context else ""
    return f"""\
Design an implementation plan for the following feature request:

{feature_prompt}

Target repository: {target_repo}

=== CODEBASE CONTEXT ===
All file paths in your plan MUST come from this context. Do not invent paths.
{codebase_context}
{extra}

Follow the structure specified in the system prompt. Ensure the pipeline JSON uses "{target_repo}" as the target_workspace value.
"""


def save_report(report_text: str, report_path: str) -> None:
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"  Report saved: {report_path}")


def save_pipeline(pipeline: dict[str, Any], pipeline_path: str) -> None:
    os.makedirs(os.path.dirname(pipeline_path), exist_ok=True)
    with open(pipeline_path, "w", encoding="utf-8") as f:
        json.dump(pipeline, f, indent=2)
    print(f"  Pipeline saved: {pipeline_path}")


def cmd_generate(args: argparse.Namespace) -> None:
    name = args.name or args.prompt.split(":")[0].strip().lower().replace(" ", "_")[:40]
    report_filename = f"{name}.md"
    pipeline_filename = f"{name}.json"
    report_path = os.path.join(args.reports_dir, report_filename)
    pipeline_path = os.path.join(args.reports_dir, pipeline_filename)

    if os.path.exists(report_path) and not args.force:
        print(f"Report already exists at {report_path}. Use --force to overwrite.")
        sys.exit(1)

    print(f"Generating feature plan for: {name}")
    print(f"  Architect: [{'OLLAMA' if args.engine == 'ollama' else 'GEMINI'}] via {args.model}")
    print(f"  Scanning codebase: {args.target_repo}")
    if args.mcp_config:
        print(f"  MCP Config: {args.mcp_config}")
    print()

    codebase_context = build_codebase_context(args)
    mcp_context = get_mcp_context(args)
    user_prompt = build_feature_prompt(args.prompt, codebase_context, args.target_repo, mcp_context)

    architect = build_agent(args.engine, args.model, ARCHITECT_SYSTEM_PROMPT, getattr(args, 'num_ctx', 65536))
    raw_output = architect.execute(user_prompt)

    pipeline = extract_pipeline_json(raw_output)
    if pipeline is None:
        print("Could not extract a valid pipeline from the LLM response.")
        print("The raw response has been saved for review.")
        fallback_path = report_path.replace(".md", "_raw.md")
        save_report(raw_output, fallback_path)
        sys.exit(1)

    pipeline["target_workspace"] = args.target_repo
    pipeline["feature_name"] = name

    save_report(raw_output, report_path)
    save_pipeline(pipeline, pipeline_path)

    print()
    print(f"Done. Review the report at: {report_path}")
    print(f"Then run: python3 new_feature_harness.py {pipeline_path}")
    print("(Or edit the report and run --update to refresh the pipeline.)")


def cmd_update(args: argparse.Namespace) -> None:
    md_path = args.update
    if not os.path.exists(md_path):
        print(f"Report file not found: {md_path}")
        sys.exit(1)

    with open(md_path, "r", encoding="utf-8") as f:
        report_text = f.read()

    pipeline = extract_pipeline_json(report_text)
    if pipeline is None:
        print("Could not extract a valid pipeline from the report.")
        print("Ensure the report has a '## Implementation Pipeline' section")
        print("with a valid JSON code block.")
        sys.exit(1)

    pipeline_path = md_path.replace(".md", ".json")
    save_pipeline(pipeline, pipeline_path)
    print(f"Pipeline extracted and saved to {pipeline_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a feature plan (.md) and pipeline (.json)")
    sub = parser.add_subparsers(dest="mode", required=True)

    gen = sub.add_parser("generate", help="Generate a new feature plan from a prompt")
    gen.add_argument("--prompt", required=True, help="Brief feature prompt (e.g. 'Throttle Public Onboarding Endpoints...')")
    gen.add_argument("--name", help="Feature name (used for filenames). Derived from prompt if omitted.")
    gen.add_argument("--target-repo", default=os.environ.get("TARGET_REPO", ""), help="Absolute path to the target repository")
    gen.add_argument("--reports-dir", default=REPORTS_DIR, help="Directory for output files (default: reports/)")
    gen.add_argument("--agents-dir", default=AGENTS_DIR, help="Directory for agent persona files (default: agents/)")
    gen.add_argument("--engine", default="ollama", choices=["ollama", "gemini"], help="LLM backend")
    gen.add_argument("--model", default=ARCHITECT_MODEL, help="Model name (e.g. qwen3.6:latest)")
    gen.add_argument("--num-ctx", type=int, default=65536, help="Context window size for Ollama")
    gen.add_argument("--mcp-config", help="Path to MCP server configuration JSON")
    gen.add_argument("--force", action="store_true", help="Overwrite existing report")

    upd = sub.add_parser("update", help="Re-extract pipeline JSON from an edited .md report")
    upd.add_argument("update", help="Path to the .md report file")

    args = parser.parse_args()

    if args.mode == "generate":
        if not args.target_repo:
            print("Error: --target-repo is required (or set TARGET_REPO env var).")
            sys.exit(1)
        if not os.path.exists(args.target_repo):
            print(f"Error: target repo does not exist: {args.target_repo}")
            sys.exit(1)
        cmd_generate(args)
    elif args.mode == "update":
        cmd_update(args)


if __name__ == "__main__":
    main()
