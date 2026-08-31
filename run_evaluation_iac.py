import argparse
import json
import os
import sys
import time

import requests

from core import harness
from core.config import get_config
from core.forge_proxy import ForgeProxy
from core.terragrunt_parser import TerragruntTopographer, format_topology_for_prompt


def _abort_on_conn_error(e: Exception) -> None:
    """Surface an actionable message for LLM connection failures, then exit."""
    print(
        f"\n❌ LLM request failed ({type(e).__name__}). "
        "If local, Ollama may be down or the model OOM'd during generation. "
        "Check `ollama ps` / GPU memory and retry."
    )
    sys.exit(1)


MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG", "mcp_config.iac.json")


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


def main():
    args = parse_arguments()
    cfg = get_config()

    target_repo = args.repo or os.environ.get("TARGET_IAC_PROJECT")
    mcp_config_path = args.mcp_config or os.environ.get("MCP_CONFIG", MCP_CONFIG_PATH)

    if cfg.is_cloud and not cfg.api_key:
        print("Error: USE_GEMINI=true requires GEMINI_API_KEY to be set.")
        print("Please run: export GEMINI_API_KEY='your_key_here'")
        return

    if not target_repo:
        print("Error: No target repository specified.")
        print("Set TARGET_IAC_PROJECT env var or pass --repo /path/to/iac/project")
        return

    harness.banner(
        "Launching IaC Architecture Review Engine (Hybrid Mode)",
        cfg,
        target_project=target_repo,
        local_judge=cfg.resolve_judge(),
    )

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

    with harness.mcp_context(mcp_config_path, target_repo) as orch:
        mcp_block = (
            orch.build_mcp_context_block(
                tags=["iac_rule", "architectural_rule"],
                exclude_tools_from=["gortex"],
            )
            if orch
            else ""
        )
        if orch:
            print("   [Done] MCP workbench active (tools + git + memory)\n")
        else:
            print(
                "   [Skipped] No MCP config found. "
                "Use MCP_CONFIG env var or mcp_config.iac.json\n"
            )

        # 3. Build shared context (injected into Pass 1 only; threaded onward)
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

        shared_context = (
            f"{parser_limitations}\n\n## Project Topography (formatted)\n```\n"
            f"{topology_text}\n```\n\n## Project Topography (raw JSON)\n```json\n"
            f"{project_map_json}\n```"
        )

        pass1 = """[Pass 1: Repository Observation]
Analyze the Terragrunt/OpenTofu repository topography provided above.

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
Confidence:"""

        pass2 = """[Pass 2: Evidence Validation]
Review all observations from Pass 1.

Categorize each observation as:
- Confirmed
- Plausible
- Speculative

Cross-check each observation against the actual topography provided above:
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
Likely Impact:"""

        pass3 = """[Pass 3: Staff Prioritization]
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
- Estimated implementation effort."""

        pass4 = """[Pass 4: Executive Reporting]
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

Focus on pragmatic infrastructure evolution."""

        # 4. Execute multi-pass architecture review
        print(f"Step 3: Processing architecture review via [{cfg.reasoning_model}]...")
        pass_start = time.time()

        try:
            draft_report, model_used, _hist = harness.run_multipass(
                system_prompt=system_agent_prompt,
                passes=[pass1, pass2, pass3, pass4],
                shared_context=shared_context,
                role="reasoning",
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _abort_on_conn_error(e)
        print(
            f"   [Done] Architecture review via {model_used} "
            f"in {time.time() - pass_start:.2f}s"
        )

        # 5. Adversarial review (separate model family)
        print(f"Step 4: Running adversarial review via [{cfg.heavy_reviewer}]...")
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
        adversary = harness.build_agent(
            "Adversary",
            system_prompt="",
            model_override=cfg.heavy_reviewer,
            num_ctx=65536,
        )
        try:
            critique = adversary.execute(adversarial_prompt)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _abort_on_conn_error(e)
        print(
            f"   [Done] Adversarial review completed in {time.time() - adv_start:.2f}s"
        )

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
        architect = harness.build_agent("Systems_Architect", system_agent_prompt)
        try:
            final_report = architect.execute(revision_prompt)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _abort_on_conn_error(e)
        print(f"   [Done] Revision completed in {time.time() - rev_start:.2f}s")

        # 7. Evaluate final report quality + archive
        print(f"Step 6: Evaluating final report via Judge [{cfg.resolve_judge()}]...")
        judge_start = time.time()
        judge_context = f"Project Topography:\n{project_map_json[:8000]}"
        scores = harness.grade_and_archive(
            output=final_report,
            rubric_path="rubrics/iac_architecture_rubric.json",
            agent_role="Staff IaC Architecture Review",
            model_used=model_used,
            config=cfg,
            judge_context=judge_context,
            report_path="reports/staff_iac_architecture_review.md",
        )
        print(f"   [Done] Judging in {time.time() - judge_start:.2f}s")
        print(f"IaC Architecture Review Reliability Scores: {scores}")

        if orch:
            orch.remember(
                "eval:iac_architecture_review:complete",
                "IaC architecture review completed. Report: reports/staff_iac_architecture_review.md",
                tags=["evaluation", "iac", "architecture", "complete"],
            )

    total_duration = time.time() - start_time
    print("\nReport saved to: reports/staff_iac_architecture_review.md")
    print(f"Total Time: {total_duration:.2f}s  Model: {model_used}")


if __name__ == "__main__":
    with ForgeProxy(get_config()):
        main()
