import os
import sys
import json
import argparse
import time
from core.parser import DjangoTopographer
from core.git_provider import GitDiffProvider
from core.runner import StatefulHarnessRunner
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

# ==============================================================================
# 100% FREE HARDWARE-OPTIMIZED ALLOCATION
# ==============================================================================
# Cloud Architect: Handles the heavy context lifting for $0 (1M token window)
REASONING_ARCHITECT   = "qwen3.6:latest"
ARCHITECT_API_BASE    = "https://generativelanguage.googleapis.com/v1beta/openai" if "gemini" in REASONING_ARCHITECT.lower() else "http://localhost:11434"
ARCHITECT_API_KEY     = os.getenv("GEMINI_API_KEY")

# Local Fallback: Used when cloud API is unavailable
# qwen3.6: 23GB, 256K context — large enough for the full diff + two-pass review
FALLBACK_REVIEWER     = "gemini-2.5-flash"
# Local Judge: Scoring a review against a rubric is simpler — 14B is sufficient
HEAVY_REVIEWER        = "deepseek-r1:14b" 
# ==============================================================================

TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"

def parse_arguments():
    parser = argparse.ArgumentParser(description="Local-Cloud Hybrid Code Review Engine")
    parser.add_argument(
        "--target", "-t", 
        default="main", 
        help="Target branch to merge into (default: main)"
    )
    parser.add_argument(
        "--source", "-s", 
        default="develop", 
        help="Source branch containing new changes (default: develop)"
    )
    return parser.parse_args()

def main():
    args = parse_arguments()
    target_branch = args.target
    source_branch = args.source

    is_local_mode = "localhost" in ARCHITECT_API_BASE or not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("❌ Error: GEMINI_API_KEY environment variable is not set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    print(f"{'='*60}")
    print(f"🚀 Launching Local Code Review Engine (Hybrid Mode)")
    print(f"Target Project   : {TARGET_DJANGO_PROJECT}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local JSON Judge : {HEAVY_REVIEWER}")
    print(f"Review Delta     : {target_branch} <--- {source_branch}")
    print(f"{'='*60}\n")

    start_time = time.time()

    # 1. Gather Global Structural Picture
    print("📦 Step 1a: Parsing global project topography...")
    topographer = DjangoTopographer(TARGET_DJANGO_PROJECT)
    project_map = topographer.scan_project()

    # 2. Gather Local Line Changes Picture
    print("🔍 Step 1b: Extracting Git modifications...")
    git_layer = GitDiffProvider(TARGET_DJANGO_PROJECT)
    raw_diff = git_layer.get_diff(target_branch, source_branch)
    changed_files = git_layer.get_changed_files(target_branch, source_branch)

    if not raw_diff or not raw_diff.strip():
        print("❌ Error: No Git differences found between these branches. Exiting.")
        return

    print(f"   [Git] Detected changes across {len(changed_files)} files.")

    # 3. (Full file context omitted to keep prompt within context window. Project topography map below includes model fields for fact-checking.)

    # 4. Load Reviewer Persona
    persona_path = "agents/code_reviewer.md"
    if not os.path.exists(persona_path):
        print(f"❌ Error: System prompt missing at {persona_path}")
        return
        
    with open(persona_path, "r", encoding="utf-8") as f:
        system_agent_prompt = f.read()

    # 5. Build prompt context
    project_map_json = json.dumps(project_map, indent=2, default=str)
    changed_files_json = json.dumps(changed_files, indent=2)

    # 6. Build prompts for either cloud or local mode
    # Single-pass fallback: used for local-only mode and cloud API failures
    fallback_prompt = f"""Below is the project model map (field names for fact-checking) and the git diff.

## Project Model Map
```json
{project_map_json}
```

## Git Diff
```diff
{raw_diff}
```

## Review Instructions
Review the diff above. For each changed file, evaluate whether the changes are correct.

### Mandatory Rules (violations will be flagged):
1. **CITE DIFF LINES** — For every issue, quote the actual `+` or `-` lines from the diff. Example: `in profiles/models.py line 42: +   is_active = BooleanField(default=True)`
2. **NO FABRICATED EXAMPLES** — Never invent function names, permission codenames, method signatures, or field names. If you cannot see them in the diff or the project map, do not mention them.
3. **UNCERTAIN MEANS UNCERTAIN** — If you aren't sure whether a change is correct, say `UNCERTAIN: [what you're unsure about]`. Do not hedge with vague wording.
4. **CORRECT IS A FINDING** — If a change looks correct, say "Looks correct" explicitly. A review that finds no issues is valid.
5. **NO GENERIC ADVICE** — Do not give generic Django/python architecture lectures. Only comment on what the diff actually changes.
6. **FILE-BY-FILE** — Cover each changed file in order. For each one: (a) what changed, (b) is it correct, (c) any issues found.
7. **TEST COVERAGE & VALIDITY** — For every changed source file, check that corresponding test files exist and adequately cover the new/modified logic. Flag tests that use vague or tautological assertions (e.g., `assert response.status_code == 200` without checking response body, or `assert True`). Tests must assert meaningful outcomes — request that tests validate actual state changes, error messages, or data transformations.

Format as markdown with file paths as headings."""

    # Two-pass reasoning for cloud; single-pass with all context for local-only mode
    if is_local_mode:
        passes = [fallback_prompt]
    else:
        pass1 = f"""[Pass 1: Context Alignment & Blast Radius Mapping]
Here is the global system layout of the app (models with their fields, serializers, views):
{project_map_json}

Here are the files changed in this branch:
{changed_files_json}

Analyze the structural intersection. Which upstream modules, views, or serializers could break or be impacted by changes to these specific files?
Identify potential vulnerabilities or scaling defects introduced by the patch."""

        pass2 = f"""[Pass 2: Detailed Code Review]
Review the raw lines of code changed in this branch:
```diff
{raw_diff}
```

Generate your final review report. Evaluate line changes, ensure patterns are clean, verify things are getting better and not worse, and generate code corrections where needed.

Follow the markdown schema and headers defined in your system prompt."""

        passes = [pass1, pass2]

    # 7. Execute Reasoning Pass
    print(f"🤖 Step 2: Processing Review via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()
    
    runner = StatefulHarnessRunner(
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        fallback_model_name=FALLBACK_REVIEWER,
        num_ctx=65536  # qwen3.6 supports 256K; 64K is fast and fits diff + response
    )
    history = runner.execute_sequence(
        system_prompt=system_agent_prompt,
        passes=passes,
        fallback_prompt=fallback_prompt
    )
    final_review = history[-1]["output"]
    model_used = runner.model_name
    
    print(f"   [Done] Review generated via {model_used} in {time.time() - pass_start:.2f}s")

    # 8. Evaluate review quality
    print(f"⚖️ Step 3: Checking review quality via Local Judge [{HEAVY_REVIEWER}]...")
    judge_start = time.time()
    
    # Provide the diff + project map as ground truth so the judge can detect fabrication
    judge_context = f"Diff:\n```diff\n{raw_diff[:10000]}\n```\n\nProject Map:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=HEAVY_REVIEWER)
    scores = evaluator.grade_run(final_review, "rubrics/code_review_rubric.json", context=judge_context)
    
    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"📊 Review Reliability Scores: {scores}")

    # 9. Log and Export Artifacts
    print("🗄️ Step 4: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role=f"Staff Code Review ({source_branch})",
        raw_output=final_review,
        scores=scores
    )

    report_filename = "reports/automated_code_review.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_review)
        
    total_duration = time.time() - start_time
    print(f"\n✅ Report saved to: {report_filename}")
    print(f"⏱️ Total Time: {total_duration:.2f}s  Model: {model_used}")

if __name__ == "__main__":
    main()
