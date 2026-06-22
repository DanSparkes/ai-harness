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
AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
_mlx_cache = {}

ARCHITECT_MODEL = "mlx-community/Qwen3.6-27B-4bit"
ARCHITECT_ENGINE = "mlx-lm"
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
      "instruction": "detailed, specific implementation instruction for this file. Include class names, methods, imports, and exactly what to change."
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
    if not os.path.exists(config_path):
        return None
    from core.mcp_orchestrator import MCPOrchestrator
    orch = MCPOrchestrator(config_path, target_repo=target_repo)
    started = orch.start()
    if started:
        _mcp_orchestrator = orch
        if target_repo:
            try:
                orch.call_tool("git", "git_set_repo", {"path": target_repo})
            except Exception:
                pass
        return orch
    return None


def get_mcp_context(args: argparse.Namespace) -> str:
    """Gather context from MCP servers for richer architectural planning."""
    if not args.mcp_config:
        return ""
    target = getattr(args, "target_repo", None)
    orch = init_mcp_orchestrator(args.mcp_config, target)
    if not orch:
        return ""
    parts = ["=== MCP-AUGMENTED CONTEXT ==="]
    try:
        status = orch.git_status()
        if status and status != "(no output)":
            parts.append(f"Git Status:\n{status}")
    except Exception:
        pass
    try:
        recent = orch.git_log(max_count=15)
        if recent and not recent.startswith("("):
            parts.append(f"Recent Commits:\n{recent}")
    except Exception:
        pass
    try:
        memory = orch.recall(tags=["architectural_rule"])
        if memory and memory != "(no memories)":
            parts.append(f"Architectural Rules:\n{memory}")
    except Exception:
        pass
    try:
        memory = orch.recall(tags=["active", "campaign_complete"])
        if memory and memory != "(no memories)":
            parts.append(f"Active Campaign Context:\n{memory}")
    except Exception:
        pass
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


def call_gemini(system_prompt: str, user_prompt: str) -> str:
    api_key = get_gemini_api_key()
    if not api_key:
        print("Error: GEMINI_API_KEY not set.")
        sys.exit(1)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }

    delays = [1, 2, 4, 8, 16]
    for attempt, delay in enumerate(delays):
        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=60)
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            if attempt == len(delays) - 1:
                print(f"Gemini API exhausted: {e}")
                sys.exit(1)
            print(f"  Gemini retry {attempt+1}/{len(delays)}: {e}")
            time.sleep(delay)
    return ""


def call_mlx_lm(model: str, system_prompt: str, user_prompt: str) -> str:
    from mlx_lm import load, generate
    if model not in _mlx_cache:
        print(f"   Loading {model}...")
        _mlx_cache[model] = load(model)
    mlx_model, mlx_tokenizer = _mlx_cache[model]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    prompt = mlx_tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    t0 = time.time()
    result = generate(mlx_model, mlx_tokenizer, prompt=prompt, verbose=False, max_tokens=8192)
    print(f"   -> Generated {len(result)} chars in {time.time() - t0:.1f}s")
    return result


def execute_agent(engine: str, model: str, system_prompt: str, user_prompt: str) -> str:
    if engine == "gemini":
        return call_gemini(system_prompt, user_prompt)
    return call_mlx_lm(model, system_prompt, user_prompt)


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
    print(f"  Architect: [{args.engine.upper()}] via {args.model}")
    print(f"  Scanning codebase: {args.target_repo}")
    if args.mcp_config:
        print(f"  MCP Config: {args.mcp_config}")
    print()

    codebase_context = build_codebase_context(args)
    mcp_context = get_mcp_context(args)
    user_prompt = build_feature_prompt(args.prompt, codebase_context, args.target_repo, mcp_context)

    raw_output = execute_agent(args.engine, args.model, ARCHITECT_SYSTEM_PROMPT, user_prompt)

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
    gen.add_argument("--engine", default=ARCHITECT_ENGINE, choices=["mlx-lm", "gemini"], help="LLM backend")
    gen.add_argument("--model", default=ARCHITECT_MODEL, help="Model name")
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
