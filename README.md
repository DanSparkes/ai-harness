# Generate a plan
```
python3 generate_feature_plan.py generate \
  --prompt "Throttle Public Onboarding Endpoints: Apply DRF throttle_classes to RegistrationWaitlistView and LoginView to prevent DB lock contention and brute-force vectors during peak traffic." \
  --name throttle-onboarding \
  --target-repo /Users/dansparkes/memores/memores-api
```

# After editing the .md, re-extract the pipeline
```
python3 generate_feature_plan.py update reports/throttle_onboarding.md
```
The LLM generates a report with sections Codebase Target Map, Architecture & Design, Risk Assessment, and an Implementation Pipeline containing a JSON pipeline block. The script extracts that JSON into a separate .json file.
```
python3 new_feature_harness.py <plan.json|plan.md>
```
Workflow
Prompt → generate_feature_plan.py → .md (you review/edit) ─┐
                                   → .json (generated) ─────┤
                                                            ↓
                                              new_feature_harness.py → code