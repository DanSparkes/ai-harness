import os
from core.parser import DjangoTopographer
from core.runner import StatefulHarnessRunner
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

# Active Local Inventory
REASONING_ARCHITECT = "qwen3.6:latest"
HEAVY_REVIEWER      = "qwen2.5-coder:32b"
TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"

def main():
    print(f"Step 1: Parsing project topography for {TARGET_DJANGO_PROJECT}...")
    topographer = DjangoTopographer(TARGET_DJANGO_PROJECT)
    project_map = topographer.scan_project()
    
    # 1. Load the new Staff Onboarding Persona
    with open("agents/staff_onboarding.md", "r") as f:
        system_agent_prompt = f.read()

    # 2. Re-wire the Two-Pass Thinking Sequence for Strategic Synthesis
    passes = [
        f"""[Pass 1: Architecture Synthesis]
        Review this parsed structural layout of your new codebase:
        {project_map}
        
        Brainstorm a raw ledger of structural bottlenecks, coupling issues, observability gaps, and potential quick wins. 
        Do not structure the 90-day roadmap or write the final sections yet.""",
        
        """[Pass 2: Timeline Filtering & Production Strategy]
        Review your synthesis from Pass 1. Group, trim, and refine those insights into a concrete, realistic 90-day onboarding strategy.
        
        Ensure you explicitly call out: Quick Wins, Major Risks, Organizational Improvements, Architecture Investments, and Observability/Testing gaps.
        
        Generate the final report exactly matching the Markdown schema defined in your system prompt."""
    ]

    print(f"Step 2: Spawning Staff Engineer Onboarding Run using [{REASONING_ARCHITECT}]...")
    runner = StatefulHarnessRunner(model_name=REASONING_ARCHITECT)
    history = runner.execute_sequence(system_agent_prompt, passes)
    final_analysis = history[-1]["output"]
    
    print("\n--- Onboarding Strategy Synthesis Completed ---\n")

    # 3. Grade using the Strategy Rubric
    print(f"Step 3: Evaluating strategy viability using [{HEAVY_REVIEWER}] as Strategic Judge...")
    evaluator = AutomatedEvaluator(judge_model=HEAVY_REVIEWER)
    scores = evaluator.grade_run(final_analysis, "rubrics/strategy_rubric.json")
    print(f"Strategy Scores Awarded: {scores}")

    # 4. Log to Database Warehouse
    print("Step 4: Writing Results to Warehouse...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=REASONING_ARCHITECT,
        agent_role="Incoming Staff Engineer (90-Day Strategy)",
        raw_output=final_analysis,
        scores=scores
    )

    # 5. Export clean markdown artifact
    report_filename = "reports/staff_90_day_onboarding_roadmap.md"
    os.makedirs("reports", exist_ok=True)
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(final_analysis)
    print(f"📄 Strategic Roadmap exported to: {report_filename}")

if __name__ == "__main__":
    main()
