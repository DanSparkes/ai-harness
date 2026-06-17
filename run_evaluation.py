import os

from core.parser import DjangoTopographer
from core.runner import StatefulHarnessRunner
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

REASONING_ARCHITECT = "qwen3.6:latest"
HEAVY_REVIEWER = "qwen2.5-coder:32b"

TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"


def main():
    print(
        f"Step 1: Ingesting Project Topography for {TARGET_DJANGO_PROJECT}..."
    )

    topographer = DjangoTopographer(TARGET_DJANGO_PROJECT)
    project_map = topographer.scan_project()

    with open("agents/architecture_review.md", "r") as f:
        system_agent_prompt = f.read()

    passes = [
        f"""
[Pass 1: Repository Observation]

Analyze this Django repository topography:

{project_map}

Your task is ONLY to identify observations.

For each observation:

- describe what exists,
- identify the relevant files,
- explain why it may matter operationally,
- assign a confidence score (High / Medium / Low).

Rules:

- Do NOT propose solutions.
- Do NOT infer missing structures.
- Do NOT speculate.
- Do NOT introduce architectural patterns.

Output format:

Observation:
Evidence:
Operational Significance:
Confidence:
""",
        """
[Pass 2: Evidence Validation]

Review all observations from Pass 1.

Categorize each observation as:

- Confirmed
- Plausible
- Speculative

Definitions:

Confirmed:
- supported directly by repository evidence.

Plausible:
- partially supported but requires additional inspection.

Speculative:
- insufficient evidence.

Rules:

- Discard speculative findings.
- Preserve only confirmed findings.
- Do NOT recommend fixes.

Output format:

Finding:
Category:
Evidence:
Reasoning Chain:
Likely Impact:
""",
        """
[Pass 3: Staff Prioritization]

Assume you are the Staff Engineer responsible for this system.

Constraints:

- Two engineers.
- One quarter.
- Existing feature commitments remain unchanged.

Using ONLY confirmed findings:

Select EXACTLY five initiatives.

Rank them by:

1. Operational impact,
2. Engineering effort,
3. Developer productivity impact,
4. Incident prevention potential.

For each initiative provide:

- Why it was selected,
- Why alternatives were deferred,
- Estimated implementation effort.
""",
        """
[Pass 4: Executive Reporting]

Generate the final report.

Requirements:

- Separate evidence from interpretation.
- Introduce NO new findings.
- Preserve prioritization rationale.
- Explicitly identify assumptions.

Avoid recommending:

- service layers,
- DTO layers,
- command buses,
- app decomposition,

unless repository evidence demonstrates
that the current approach is failing.

Focus on pragmatic Django evolution.
""",
    ]

    print(
        f"Step 2: Running Staff Review Harness via [{REASONING_ARCHITECT}]..."
    )

    runner = StatefulHarnessRunner(
        model_name=REASONING_ARCHITECT
    )

    history = runner.execute_sequence(
        system_agent_prompt,
        passes,
    )

    draft_report = history[-1]["output"]

    print(
        f"Step 3: Running Adversarial Review via [{HEAVY_REVIEWER}]..."
    )

    adversarial_prompt = f"""
Act as a skeptical Staff Django Engineer.

Review this report.

Your job is NOT to improve it.

Your job is to identify:

- unsupported claims,
- over-engineering,
- recommendations lacking evidence,
- Django anti-patterns introduced by the reviewer.

For each criticism provide:

- Severity,
- Confidence,
- Supporting rationale.

Report:

{draft_report}
"""

    reviewer = StatefulHarnessRunner(
        model_name=HEAVY_REVIEWER
    )

    critique = reviewer.execute_sequence(
        "",
        [adversarial_prompt],
    )[-1]["output"]

    print("Step 4: Final Revision Pass...")

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

    final_report = runner.execute_sequence(
        "",
        [revision_prompt],
    )[-1]["output"]

    print(
        f"Step 5: Running Automated Evaluation via [{HEAVY_REVIEWER}]..."
    )

    evaluator = AutomatedEvaluator(
        judge_model=HEAVY_REVIEWER
    )

    scores = evaluator.grade_run(
        final_report,
        "rubrics/architecture_rubric.json",
    )

    print(f"Scores Awarded: {scores}")

    print("Step 6: Logging Historical Record...")

    warehouse = HarnessWarehouse()

    warehouse.log_run(
        model_name=REASONING_ARCHITECT,
        agent_role="Staff Architecture Review",
        raw_output=final_report,
        scores=scores,
    )

    os.makedirs("reports", exist_ok=True)

    report_filename = (
        "reports/staff_architecture_review.md"
    )

    with open(
        report_filename,
        "w",
        encoding="utf-8",
    ) as f:
        f.write(final_report)

    print(
        f"📄 Final report written to: {report_filename}"
    )


if __name__ == "__main__":
    main()
