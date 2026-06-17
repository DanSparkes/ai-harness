# run_evaluation.py (Synchronized to your exact local Ollama inventory)

import os
from core.parser import DjangoTopographer
from core.runner import StatefulHarnessRunner
from core.judge import AutomatedEvaluator
from core.warehouse import HarnessWarehouse

# Explicit Model Routing matching your local setup
REASONING_ARCHITECT = "qwen3.6:latest"       # 23 GB, 256k context, reasoning engine
DETERMINISTIC_CODER = "qwen3-coder:latest"   # 18 GB, 16k context, 0.2 temp lock
HEAVY_REVIEWER      = "qwen2.5-coder:32b"    # 19 GB, Excellent standalone evaluator/judge

# Target Path to evaluate (Point to your dummy folder first to test)
TARGET_DJANGO_PROJECT = "/Users/dansparkes/memores/memores-api"

def main():
    print(f"Step 1: Generating Codebase Topography Map...")
    topographer = DjangoTopographer(TARGET_DJANGO_PROJECT)
    project_map = topographer.scan_project()
    
    if not project_map["serializers"] and not project_map["views"]:
        print(f"⚠️ Warning: No Django views or serializers detected in {TARGET_DJANGO_PROJECT}. Check paths.")
    
    # Read System Prompt Persona
    with open("agents/security.md", "r") as f:
        system_agent_prompt = f.read()

    # Two-Pass Thinking sequence loops
    passes = [
        f"""[Pass 1: Discovery]
        Review this extracted codebase topography structure:
        {project_map}

        Identify potential mass-assignment or authorization vulnerabilities. Do not make recommendations yet.""",

        """[Pass 2: Strict Verification & Citation]
        Review your findings from Pass 1.

        CRITICAL MANDATE: For every single vulnerability or risk you retain in your final report, you MUST explicitly prepend the exact 'absolute_path' string provided in the topography map. If you cannot map a finding to an exact file path from the context, discard the finding entirely.

        Generate the final report using the markdown schema defined in your system prompt."""
    ]

    print(f"Step 2: Spawning Architect Session using [{REASONING_ARCHITECT}]...")
    runner = StatefulHarnessRunner(model_name=REASONING_ARCHITECT)
    history = runner.execute_sequence(system_agent_prompt, passes)
    
    final_analysis = history[-1]["output"]
    print("\n--- Final Analysis Generation Completed ---\n")

    print(f"Step 3: Executing Automated Evaluation with Judge [{HEAVY_REVIEWER}]...")
    evaluator = AutomatedEvaluator(judge_model=HEAVY_REVIEWER)
    scores = evaluator.grade_run(final_analysis, "rubrics/security_rubric.json")
    
    print(f"Scores Awarded: {scores}")

    print("Step 4: Writing Results to Warehouse...")
    warehouse = HarnessWarehouse()
    warehouse.log_run(
        model_name=REASONING_ARCHITECT,
        agent_role="Security Staff Engineer",
        raw_output=final_analysis,
        scores=scores
    )
    print("Evaluation Run Successfully Logged and Verifiable.")

if __name__ == "__main__":
    main()
