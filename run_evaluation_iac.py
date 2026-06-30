import argparse
import json
import os
import time

from core.agent import Agent
from core.judge import AutomatedEvaluator
from core.mcp_orchestrator import init_orchestrator
from core.terragrunt_parser import TerragruntTopographer, format_topology_for_prompt
from core.warehouse import HarnessWarehouse

USE_GEMINI = os.getenv("USE_GEMINI", "").lower() in ("1", "true", "yes")

CLOUD_MODEL = "gemini-2.5-flash"
LOCAL_MODEL = "ornith:35b"

REASONING_ARCHITECT = CLOUD_MODEL if USE_GEMINI else LOCAL_MODEL
ARCHITECT_API_BASE = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
    if USE_GEMINI
    else "http://localhost:11434"
)
ARCHITECT_API_KEY = os.getenv("GEMINI_API_KEY") if USE_GEMINI else None

FALLBACK_REVIEWER = "gemini-2.5-flash"
HEAVY_REVIEWER = "deepseek-r1:14b"
LOCAL_JUDGE = "qwen3-coder:latest"

MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.iac.json")

_mcp_orch = None


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="IaC Architecture Review Evaluation Engine"
    )
    parser.add_argument(
        "--repo",
        "-r",
        default=None,
        help="Path to the target IaC repository (overrides TARGET_IAC_PROJECT env var)",
    )
    parser.add_argument(
        "--project-context",
        "-c",
        default=None,
        help="Path to a project-specific context file (markdown) with domain knowledge",
    )
    parser.add_argument(
        "--mcp-config",
        "-m",
        default=None,
        help="Path to MCP server config file (overrides MCP_CONFIG env var)",
    )
    return parser.parse_args()


def init_mcp(repo_path: str | None = None, config_path: str | None = None):
    global _mcp_orch
    if _mcp_orch is not None:
        return _mcp_orch
    cfg_path: str = config_path or MCP_CONFIG_PATH
    path: str = repo_path or os.environ.get("TARGET_IAC_PROJECT") or os.getcwd()
    orch = init_orchestrator(cfg_path, path)
    if orch:
        _mcp_orch = orch
    return orch


def build_mcp_context_block() -> str:
    orch = _mcp_orch
    if not orch:
        return ""
    return orch.build_mcp_context_block(tags=["iac_rule", "architectural_rule"])


def main():
    args = parse_arguments()

    target_repo = args.repo or os.environ.get("TARGET_IAC_PROJECT")
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    is_local_mode = not ARCHITECT_API_KEY
    if not is_local_mode and not ARCHITECT_API_KEY:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_IAC_PROJECT env var or pass --repo /path/to/iac/project")
        return

    print(f"{'=' * 60}")
    print("Launching IaC Architecture Review Engine (Hybrid Mode)")
    print(f"Target Project   : {target_repo}")
    print(f"Cloud Architect  : {REASONING_ARCHITECT}")
    print(f"Local Judge      : {LOCAL_JUDGE}")
    print(f"{'=' * 60}\n")

    start_time = time.time()

    # 1. Parse Project Topography
    print("Step 1: Parsing Terragrunt/OpenTofu topography...")
    step_start = time.time()
    topographer = TerragruntTopographer(target_repo)
    project_map = topographer.scan_project()
    topology_text = format_topology_for_prompt(project_map)
    project_map_json = json.dumps(project_map, default=str, separators=(",", ":"))
    print(f"   [Done] Topography scan in {time.time() - step_start:.2f}s")

    accounts = project_map.get("accounts", {})
    modules = project_map.get("modules", {})
    deps = project_map.get("dependencies", [])
    total_units = sum(len(a.get("units", [])) for a in accounts.values())
    print(
        f"   [Summary] {len(accounts)} accounts, {len(modules)} modules, "
        f"{len(deps)} dependencies ({total_units} units)"
    )

    # Fail fast: don't feed null topology through expensive LLM passes.
    if not accounts and not modules:
        print("\n   [Abort] Parser found nothing. Skipping architecture review.")
        print(f"   Resolved target directory: {topographer.target_dir}")
        if not topographer.target_dir.exists():
            print(
                "   ERROR: Directory does not exist. "
                "Set the TARGET_IAC_PROJECT env var or pass --repo."
            )

        else:
            print(
                f"   NOTE: {topographer.target_dir} exists but contains no recognized "
                f"Terragrunt/OpenTofu structure (no dev/, prod/, shared-services/ dirs). "
                f"Fix the target path or add valid HCL files."
            )
        return

    # 2. Load Architecture Review Persona
    print("Step 2: Loading IaC architecture review persona...")
    persona_path = "agents/iac_architecture_review.md"
    if not os.path.exists(persona_path):
        print(f"Error: System prompt missing at {persona_path}")
        return
    with open(persona_path, encoding="utf-8") as f:
        system_agent_prompt = f.read()
    print("   [Done] Persona loaded")

    # 2b. Initialize MCP workbench for richer context
    print("Step 2b: Initializing MCP workbench...")
    orch = init_mcp(repo_path=target_repo, config_path=mcp_config_path)
    mcp_block = build_mcp_context_block() if orch else ""
    if orch:
        print("   [Done] MCP workbench active (tools + git + memory)\n")
    else:
        print(
            "   [Skipped] No MCP config found. Use MCP_CONFIG env var or mcp_config.iac.json\n"
        )

    # 3. Build prompt context
    parser_limitations = f"""### Parser Capabilities & Limitations

The topography is built by static HCL/Terraform file parsing. Here's what it CAN and CANNOT resolve:

**CAN resolve:**
- Terragrunt dependency blocks (config_path, mock_outputs, skip)
- Module structure (variables, outputs, resources, provider aliases)
- Account layout (dev, prod, shared-services, shared-services-ca)
- Network topology modules (VPC, TGW, VPN, peering)
- Security modules (IAM, WAF, KMS, ACM, Secrets)
- CI/CD configuration (pre-commit, tflint, checkov, trivy, GitHub Actions)
- Provider version constraints
- Remote state backend configuration

**CANNOT resolve:**
- HCL expression evaluation or variable interpolation
- Terragrunt `run_cmd` or `get_terragrunt_dir` function outputs
- Dynamic dependency resolution at plan time
- Actual AWS resource state (requires live credentials)
- Cross-account IAM trust evaluation
- CIDR calculations or overlap detection
- WAF rule effectiveness or coverage

### Infrastructure Inventory
Based on parsing, this project contains:
- {len(accounts)} AWS accounts
- {sum(len(a.get('units', [])) for a in accounts.values())} Terragrunt units
- {len(modules)} reusable modules
- {len(deps)} cross-unit dependencies
- {len(project_map.get('network_topology', {}))} network modules
- {len(project_map.get('security', {}).get('security_modules', []))} security modules

### Anti-Hallucination Rules
1. **NEVER attribute a resource to a module unless it appears in that module's resource list.**
2. **`dependency` block != actual deploy order**: A dependency declares ordering but Terragrunt enforces it at runtime. Static analysis cannot confirm execution order.
3. **Each account is independent**: Every account has its own state backend. Do not mix state references between accounts.
4. **Only reference files and modules that appear in the topography map.** Do not invent configurations not visible in the parsed structure.
5. **Large files alone are insufficient evidence** for complexity concerns — check the actual module boundaries.
6. **Do not infer security posture from module names alone** — verify the actual resources and configurations.

### MCP-Augmented Context (Live Project State)
{mcp_block}"""

    # Build passes
    pass_templates = [
        f"""[Pass 1: Repository Observation]
Analyze this Terragrunt/OpenTofu repository topography:

{parser_limitations}

## Project Topography (formatted)
```
{topology_text}
```

## Project Topography (raw JSON)
```json
{project_map_json}
```

Your task is ONLY to identify observations.

For each observation:
- describe what exists,
- identify the relevant files/modules,
- explain why it may matter operationally,
- assign a confidence score (High / Medium / Low).

Rules:
- Do NOT propose solutions.
- Do NOT infer missing structures.
- Do NOT speculate.
- Do NOT introduce architectural patterns not already present.

Output format:

Observation:
Evidence:
Operational Significance:
Confidence:""",
        f"""[Pass 2: Evidence Validation]
Review all observations from Pass 1.

{parser_limitations}

Categorize each observation as:
- Confirmed
- Plausible
- Speculative

Cross-check each observation against the actual topography:
- **Module exists?** Confirm every referenced module appears in the `modules` section.
- **Dependency exists?** Confirm every referenced dependency appears in the `dependencies` section.
- **Account exists?** Confirm every referenced account appears in the `accounts` section.
- **Resource exists?** Only reference resources listed in module `resources`.

Definitions:
Confirmed: supported directly by repository evidence.
Plausible: partially supported but requires additional inspection.
Speculative: insufficient evidence.

Rules:
- Discard speculative findings.
- Preserve only confirmed findings.
- Do NOT recommend fixes.

Output format:

Finding:
Category:
Evidence:
Reasoning Chain:
Likely Impact:""",
        """[Pass 3: Staff Prioritization]
Assume you are the Staff Infrastructure Engineer responsible for this system.

Constraints:
- Two engineers.
- One quarter.
- Existing feature commitments remain unchanged.

Using ONLY confirmed findings:
Select EXACTLY five initiatives.

Rank them by:
1. Operational impact (blast radius, failure modes),
2. Security risk reduction,
3. Cost optimization potential,
4. Developer/ops productivity impact.

For each initiative provide:
- Why it was selected,
- Why alternatives were deferred,
- Estimated implementation effort.""",
        """[Pass 4: Executive Reporting]
Generate the final report.

Requirements:
- Separate evidence from interpretation.
- Introduce NO new findings.
- Preserve prioritization rationale.
- Explicitly identify assumptions.

Avoid recommending:
- replacing Terragrunt with custom tooling,
- adding unnecessary abstraction layers,
- introducing new IaC tools without evidence of failure,
- large-scale module restructuring unless evidence demands it.

Focus on pragmatic infrastructure evolution.""",
    ]

    passes = pass_templates

    # 4. Execute multi-pass architecture review via Agent
    print(f"Step 3: Processing architecture review via [{REASONING_ARCHITECT}]...")
    pass_start = time.time()

    architect = Agent(
        name="Systems_Architect",
        system_prompt=system_agent_prompt,
        model_name=REASONING_ARCHITECT,
        base_url=ARCHITECT_API_BASE,
        api_key=ARCHITECT_API_KEY,
        num_ctx=65536,
    )

    context_parts: list[str] = []
    for i, pass_prompt in enumerate(passes):
        combined = (
            "\n\n".join([*context_parts, pass_prompt]) if context_parts else pass_prompt
        )
        t0 = time.time()
        output = architect.execute(combined)
        print(f"   [Done] Pass {i + 1} / {len(passes)} in {time.time() - t0:.1f}s")
        context_parts.append(f"[Pass {i + 1} Output]:\n{output}")

    draft_report = output
    model_used = architect.model_name
    print(
        f"   [Done] Architecture review via {model_used} in {time.time() - pass_start:.2f}s"
    )

    # 5. Adversarial review
    print(f"Step 4: Running adversarial review via [{HEAVY_REVIEWER}]...")
    adv_start = time.time()

    adversarial_prompt = f"""
Act as a skeptical Staff Infrastructure Engineer.

Review this report.

Your job is NOT to improve it.

Your job is to identify:
- unsupported claims,
- over-engineering,
- recommendations lacking evidence,
- IaC anti-patterns introduced by the reviewer.

For each criticism provide:
- Severity,
- Confidence,
- Supporting rationale.

{parser_limitations}

Report:

{draft_report}
"""

    adversary = Agent(
        name="Adversary", system_prompt="", model_name=HEAVY_REVIEWER, num_ctx=32768
    )
    critique = adversary.execute(adversarial_prompt)
    print(f"   [Done] Adversarial review completed in {time.time() - adv_start:.2f}s")

    # 6. Revision pass
    print("Step 5: Final revision pass...")
    rev_start = time.time()

    revision_prompt = f"""
Revise the report using the critique below.

Critique:
{critique}

Rules:
- Remove unsupported findings.
- Reduce unnecessary complexity.
- Preserve evidence-backed recommendations.
- Preserve prioritization rationale.
- Explicitly state uncertainty.

Return the revised report only.

Original Report:
{draft_report}
"""

    final_report = architect.execute(revision_prompt)
    print(f"   [Done] Revision completed in {time.time() - rev_start:.2f}s")

    # 7. Evaluate final report quality
    print(f"Step 6: Evaluating final report via Local Judge [{LOCAL_JUDGE}]...")
    judge_start = time.time()

    judge_context = f"Project Topography:\n{project_map_json[:5000]}"
    evaluator = AutomatedEvaluator(judge_model=LOCAL_JUDGE)
    scores = evaluator.grade_run(
        final_report, "rubrics/iac_architecture_rubric.json", context=judge_context
    )

    print(f"   [Done] Judging completed in {time.time() - judge_start:.2f}s")
    print(f"IaC Architecture Review Reliability Scores: {scores}")

    # 8. Log and Export Artifacts
    print("Step 7: Archiving run data...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=model_used,
        agent_role="Staff IaC Architecture Review",
        raw_output=final_report,
        scores=scores,
    )

    report_filename = "reports/staff_iac_architecture_review.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_report)

    if _mcp_orch:
        _mcp_orch.remember(
            "eval:iac_architecture_review:complete",
            f"IaC architecture review completed. Report: {report_filename}",
            tags=["evaluation", "iac", "architecture", "complete"],
        )
        _mcp_orch.stop()

    total_duration = time.time() - start_time
    print(f"\nReport saved to: {report_filename}")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")


if __name__ == "__main__":
    main()
