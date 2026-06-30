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

import argparse
import json
import os
import re
import sys
from typing import Any

# ── Defaults ──────────────────────────────────────────────────────────────────
from core.agent import Agent
from core.headroom import CompressionManager
from core.mcp_orchestrator import init_orchestrator
from core.parser import minify_markdown

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

ARCHITECT_MODEL = "ornith:35b"
GEMINI_MODEL = "gemini-2.5-flash"

# MCP integration
_mcp_orchestrator = None


ARCHITECT_SYSTEM_PROMPT = (
    "You are a Staff Backend Architect designing a DRF feature implementation plan. "
    "When the prompt includes '=== MCP-AUGMENTED CONTEXT ===', use git history and memory to understand project evolution. "
    "Cross-reference all file paths against the provided codebase scan — never invent paths.\n"
    "Produce a markdown report with these sections:\n"
    "## 1. Codebase Target Map\n"
    "List exact files, classes, URL routes to create/modify. All paths from the scan only. When asked to update 'all' of a category, include EVERY matching file from the scan.\n"
    "## 2. Architecture & Design\n"
    "Describe approach with syntactically correct Python code snippets for key patterns.\n"
    "## 3. Risk Assessment & Mitigations\n"
    "Identify backwards compat, migration, performance, security risks and mitigations.\n"
    "## 4. Implementation Pipeline\n"
    "Provide a step-by-step execution plan as a JSON code block with this structure:\n"
    '{"feature_name": "...", "target_workspace": "/path/to/repo", "pipeline": [{"step": 1, "name": "...", "target_file": "relative/path.py", "task": "exact instruction — class names, methods, imports, what to change", "assigned_agent": "Engineer", "auditor_agent": "QA_Tester", "allowed_skills": ["write_file", "run_formatter", "validate_syntax", "run_mypy"], "max_attempts": 4}]}\n'
    "Pipeline rules:\n"
    "- Every target_file, function, class, decorator in instructions MUST match the codebase scan exactly.\n"
    "- For Celery tasks: modify the @shared_task function, not a helper. Verify against the CELERY TASK DECORATORS section.\n"
    "- Order steps so earlier steps create dependencies later steps rely on.\n"
    "- Instructions must be a single coherent paragraph about exactly what to add/change. No vague directives.\n"
    "- Include both new files and modifications. Match existing decorator/class/import style.\n"
    "- When updating 'all' of a kind, include EVERY matching file from the scan.\n"
    "- Use 'target_workspace' as placeholder (replaced at runtime).\n"
    "- CRITICAL — Trust AST-parsed topology over raw file previews: The DJANGO TOPOLOGY section is the definitive source for views, serializers, models, and their relationships. The CROSS-REFERENCE USAGE section shows how entities are actually imported/used. RAW FILE PREVIEWS at the bottom is only a fallback for context not present in the topology or cross-references.\n"
    "### Retry Safety — OVERRIDES ALL OTHER RULES\n"
    "If adding autoretry_for to a Celery task, check the task's error handler. AUTORETRY_FOR IS UNSAFE if the except block: (1) sets status=ERROR on a model, (2) calls .save(), (3) re-raises. On retry the task re-executes from the top, hits `if status != PENDING: raise ValueError`, and permanently fails.\n"
    "ACTION: Either (A) remove state-modifying code from the error handler — only set status=ERROR after all retries exhausted; or (B) use tenacity retry on just the API call in the task body instead of Celery-level autoretry_for.\n"
    "Explain your choice in the instructions and risk section.\n"
    "Output ONLY the report with pipeline JSON embedded in section 4."
)


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
    context = orch.build_mcp_context_block(
        tags=["architectural_rule", "active", "campaign_complete"]
    )
    orch.stop()
    return context


# ── Codebase Scanner ──────────────────────────────────────────────────────────


def build_codebase_context(args: argparse.Namespace) -> str:
    """Build a codebase context block for the LLM prompt from the target repo.

    Order matters: structured AST data first (authoritative), raw file
    previews last (fallback). This prevents the LLM from hallucinating
    based on noisy raw content.
    """
    from core.parser import (
        DjangoTopographer,
        grep_context,
        scan_celery_tasks,
        scan_file_tree,
        scan_files_by_keyword,
        scan_files_by_pattern,
    )

    file_tree = scan_file_tree(args.target_repo)
    celery_tasks = scan_celery_tasks(args.target_repo)

    parts = ["=== PROJECT FILE TREE ==="]
    parts.append("\n".join(file_tree) if file_tree else "(empty)")

    # ── AST-parsed topology (views, serializers, models) ────────────
    topographer = DjangoTopographer(args.target_repo)
    topology = topographer.scan_project()

    if topology.get("views") or topology.get("serializers") or topology.get("models"):
        parts.append("\n\n=== DJANGO TOPOLOGY (AST-parsed, authoritative) ===")
        if topology["views"]:
            parts.append("\nViews:")
            for v in topology["views"]:
                parts.append(
                    f"  {v['relative_path']} :: {v['class']} "
                    f"({', '.join(m['name'] for m in v['methods'])})"
                )
        if topology["serializers"]:
            parts.append("\nSerializers:")
            for s in topology["serializers"]:
                meta_fields = s.get("meta", {})
                if isinstance(meta_fields.get("fields"), str):
                    fd = f'fields="{meta_fields["fields"]}"'
                elif isinstance(meta_fields.get("fields"), list):
                    fd = f"fields={meta_fields['fields']}"
                else:
                    fd = f"declared_fields={[f['name'] for f in s['fields']]}"
                parts.append(f"  {s['relative_path']} :: {s['class']} {fd}")
        if topology["models"]:
            parts.append("\nModels:")
            for m in topology["models"]:
                fnames = [f["name"] for f in m.get("fields", []) if isinstance(f, dict)]
                parts.append(f"  {m['relative_path']} :: {m['class']} fields={fnames}")

    # ── Content grep for cross-references ──────────────────────────
    prompt_lower = args.prompt.lower()
    entity_names = sorted(
        {
            *(m["class"] for m in topology.get("models", [])),
            *(s["class"] for s in topology.get("serializers", [])),
            *(v["class"] for v in topology.get("views", [])),
        }
    )
    relevant = [e for e in entity_names if e.lower() in prompt_lower]
    if relevant:
        parts.append("\n\n=== CROSS-REFERENCE USAGE (content grep) ===")
        for entity in relevant[:6]:
            matches = grep_context(
                args.target_repo, rf"\b{re.escape(entity)}\b", max_matches=12
            )
            if matches:
                parts.append(f"\n{entity}:")
                for m in matches:
                    parts.append(
                        f"  {m['file']}:{m['line_number']}  {m['matched_line']}"
                    )

    # ── Celery tasks ────────────────────────────────────────────────
    if celery_tasks:
        parts.append("\n\n=== CELERY TASK DECORATORS ===")
        for t in celery_tasks:
            parts.append(f"  {t['file']}:")
            parts.append(f"    {t['decorator']}")
            parts.append(f"    def {t['function']}(")

    # ── Project context file ───────────────────────────────────────
    if getattr(args, "project_context", None):
        ctx_path = args.project_context
        if os.path.exists(ctx_path):
            try:
                with open(ctx_path, encoding="utf-8") as fh:
                    ctx_content = minify_markdown(fh.read())
                if ctx_content:
                    parts.append(
                        f"\n\n=== PROJECT CONVENTIONS ({os.path.basename(ctx_path)}) ==="
                    )
                    parts.append(ctx_content)
            except Exception:
                pass

    # ── Raw file previews (fallback — only for context missing above) ──
    keywords = re.findall(r"[\w_]+", args.prompt)
    keywords = sorted({k for k in keywords if len(k) > 3}, key=lambda k: -len(k))[:5]
    keyword_matches = []
    for kw in keywords:
        keyword_matches.extend(scan_files_by_keyword(args.target_repo, kw))
    pattern_matches = scan_files_by_pattern(args.target_repo, keywords)

    task_files = sorted({t["file"] for t in celery_tasks})
    for tf in task_files:
        if not any(tf in m["file"] for m in keyword_matches + pattern_matches):
            task_content = scan_files_by_keyword(
                args.target_repo, tf.replace(".py", "").split("/")[-1]
            )
            pattern_matches.extend(task_content)

    if keyword_matches or pattern_matches:
        parts.append(
            "\n\n=== RAW FILE PREVIEWS (first 80 lines — fallback for context not in topology above) ==="
        )
        for matches in [keyword_matches, pattern_matches]:
            if matches:
                for m in matches:
                    parts.append(f"--- {m['file']} ---")
                    parts.append(m["content"].rstrip())

    return "\n".join(parts)


def get_gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")


def build_agent(
    engine: str, model: str, system_prompt: str, num_ctx: int = 65536
) -> Agent:
    is_gemini = engine == "gemini"
    api_key = get_gemini_api_key() if is_gemini else None
    base_url = (
        "https://generativelanguage.googleapis.com/v1beta/openai"
        if is_gemini
        else "http://localhost:11434"
    )
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
    sections = re.split(
        r"^##\s+4\.?\s*Implementation Pipeline\s*$", markdown_text, flags=re.MULTILINE
    )
    if len(sections) < 2:
        sections = re.split(
            r"^##\s+Implementation Pipeline\s*$", markdown_text, flags=re.MULTILINE
        )
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

    print(
        "Warning: Found JSON block(s) in pipeline section but none parsed successfully."
    )
    return None


def build_feature_prompt(
    feature_prompt: str, codebase_context: str, target_repo: str, mcp_context: str = ""
) -> str:
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
    # Resolve prompt from --prompt or --prompt-file
    if args.prompt_file:
        if not os.path.exists(args.prompt_file):
            print(f"Error: prompt file not found: {args.prompt_file}")
            sys.exit(1)
        with open(args.prompt_file, encoding="utf-8") as f:
            args.prompt = minify_markdown(f.read())
    elif not args.prompt:
        print("Error: either --prompt or --prompt-file is required.")
        sys.exit(1)

    name = args.name
    if not name:
        if args.prompt_file:
            name = os.path.splitext(os.path.basename(args.prompt_file))[0]
        else:
            name = args.prompt.split(":")[0].strip().lower().replace(" ", "_")[:40]
    report_filename = f"{name}.md"
    pipeline_filename = f"{name}.json"
    report_path = os.path.join(args.reports_dir, report_filename)
    pipeline_path = os.path.join(args.reports_dir, pipeline_filename)

    print(f"Generating feature plan for: {name}")
    print(
        f"  Architect: [{'OLLAMA' if args.engine == 'ollama' else 'GEMINI'}] via {args.model}"
    )
    print(f"  Scanning codebase: {args.target_repo}")
    if args.mcp_config:
        print(f"  MCP Config: {args.mcp_config}")
    print()

    codebase_context = build_codebase_context(args)
    mcp_context = get_mcp_context(args)

    headroom = CompressionManager(
        target_ratio=0.4, compress_user_messages=True, protect_recent=0
    )
    if len(codebase_context) > 5000:
        codebase_context, cr = headroom.compress_context(codebase_context)
        print(
            f"  Compressed codebase context: {cr.tokens_before:,} -> {cr.tokens_after:,} tok ({cr.compression_ratio:.1%} saved)"
        )
    if len(mcp_context) > 5000:
        mcp_context, cr = headroom.compress_context(mcp_context)
        print(
            f"  Compressed MCP context: {cr.tokens_before:,} -> {cr.tokens_after:,} tok ({cr.compression_ratio:.1%} saved)"
        )

    user_prompt = build_feature_prompt(
        args.prompt, codebase_context, args.target_repo, mcp_context
    )

    architect = build_agent(
        args.engine,
        args.model,
        ARCHITECT_SYSTEM_PROMPT,
        getattr(args, "num_ctx", 65536),
    )
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
    print("Done. Next steps:")
    print()
    print("  1. Review the architectural report:")
    print(f"     cat {report_path}")
    print()
    print("  2. Execute the implementation pipeline:")
    print(f"     python3 new_feature_harness.py {pipeline_path}")
    print()
    print("  3. (Optional) Edit the report and re-extract the pipeline:")
    print(f"     vi {report_path}")
    print(f"     python3 generate_feature_plan.py update {report_path}")
    print(f"     python3 new_feature_harness.py {pipeline_path}")


def cmd_update(args: argparse.Namespace) -> None:
    md_path = args.update
    if not os.path.exists(md_path):
        print(f"Report file not found: {md_path}")
        sys.exit(1)

    with open(md_path, encoding="utf-8") as f:
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
    parser = argparse.ArgumentParser(
        description="Generate a feature plan (.md) and pipeline (.json)"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    gen = sub.add_parser("generate", help="Generate a new feature plan from a prompt")
    gen.add_argument(
        "--prompt",
        help="Brief feature prompt (e.g. 'Throttle Public Onboarding Endpoints...')",
    )
    gen.add_argument(
        "--prompt-file",
        help="Path to a .md file containing the feature prompt (alternative to --prompt)",
    )
    gen.add_argument(
        "--name",
        help="Feature name (used for filenames). Derived from prompt or filename if omitted.",
    )
    gen.add_argument(
        "--target-repo",
        default=os.environ.get("TARGET_REPO", ""),
        help="Absolute path to the target repository",
    )
    gen.add_argument(
        "--reports-dir",
        default=REPORTS_DIR,
        help="Directory for output files (default: reports/)",
    )
    gen.add_argument(
        "--agents-dir",
        default=AGENTS_DIR,
        help="Directory for agent persona files (default: agents/)",
    )
    gen.add_argument(
        "--engine", default="ollama", choices=["ollama", "gemini"], help="LLM backend"
    )
    gen.add_argument(
        "--model", default=ARCHITECT_MODEL, help="Model name (e.g. ornith:35b)"
    )
    gen.add_argument(
        "--num-ctx", type=int, default=65536, help="Context window size for Ollama"
    )
    gen.add_argument("--mcp-config", help="Path to MCP server configuration JSON")
    gen.add_argument(
        "--project-context",
        "-c",
        help="Path to project-specific context file (markdown) with conventions to inject",
    )

    upd = sub.add_parser(
        "update", help="Re-extract pipeline JSON from an edited .md report"
    )
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
