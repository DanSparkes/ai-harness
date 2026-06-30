# Staff IaC Architecture Review Report

## 1. Executive Summary

The infrastructure demonstrates a mature multi-account Terragrunt + OpenTofu foundation with explicit dependency declarations, regional data residency intent, and established CI/CD security scanning. However, three structural gaps threaten deploy safety and module maintainability: unresolved dependency graph inconsistencies masked by static metrics, high coupling in `app-wrapper` that amplifies configuration drift risk, and unvalidated security scanning baselines that may not reflect active compliance rules.

**Major Strengths:**
* Explicit `dependency` blocks and per-unit provider alias generation avoiding `root.hcl` conflicts
* Established CI/CD parity with Checkov and Trivy scanning configured in pre-commit and GitHub Actions
* Clear regional segmentation intent (`ca-central-1` vs `us-east-1`) with documented CIDR boundaries

**Major Risks:**
* Mock output configuration patterns that may not gracefully handle cross-account state access transitions
* High blast radius from `app-wrapper` coupling across networking, compute, DNS, and CDN layers
* Security baseline and suppression drift due to lack of active validation against current HCL

**Confidence Level:** Medium-High (findings are Confirmed based on direct HCL structure, dependency declarations, module metrics, and CI configuration. Runtime state access behavior, live network topology, and actual module consumer dependencies require verification.)

**Explicit Uncertainty:**
1. Cross-account S3 state access behavior during policy updates or initial deploys cannot be verified without live IAM/S3 evaluation.
2. Actual Transit Gateway peering status and route propagation depend on runtime AWS API state not visible in static analysis.
3. Module output consumption is inferred from dependency blocks; direct consumer mapping requires repository-wide grep or `terragrunt graph-dependencies` execution.

---

## 2. Top 5 Prioritized Improvements

### 1. Validate & Document Checkov Baseline & Trivy Suppressions
* **Rank:** 1
* **Confidence Level:** Confirmed
* **Focus Category:** CI-CD Quality / Security
* **Target Location:** `.checkov.yaml` or baseline file, `.github/workflows/`, pre-commit config
* **Evidence:** Repository configures pre-commit hooks, TFLint, Checkov (baseline present), Trivy (2 suppressions). Static analysis confirms baseline exists but shows no active rule violations.
* **Interpretation:** A present baseline requires verification to ensure it reflects actual compliance rather than disabled checks or empty results. Suppressions must be validated against current infrastructure to prevent masking active risks.
* **Risk Statement:** False sense of security if baseline is incomplete; suppressed rules may mask critical vulnerabilities if not periodically reviewed.
* **Estimated Effort:** S
* **Expected Impact:** Ensures security scanning posture matches reality; prevents unvalidated suppression drift.

### 2. Incrementally Decouple `app-wrapper` Module & Streamline Variable Contracts
* **Rank:** 2
* **Confidence Level:** Confirmed
* **Focus Category:** Module Design Quality
* **Target Location:** `app-wrapper` module, consuming units
* **Evidence:** Module spans ALB, ECS (api, celery_worker, celery_beat), CloudFront, ACM, Route53. Declares 44 variables and 32 outputs. Consuming units use `generate "provider_hub"` blocks alongside dependencies.
* **Interpretation:** High coupling across networking, compute, DNS, and CDN layers increases configuration surface area and drift risk. Streamlining variables/outputs improves deploy safety and developer velocity without requiring a full rewrite. Incremental extraction reduces blast radius.
* **Risk Statement:** Increased configuration surface area amplifies drift risk; extensive output passing requires strict versioning to prevent cascading failures.
* **Estimated Effort:** L
* **Expected Impact:** Reduces deployment blast radius; improves module reusability and developer onboarding velocity.

### 3. Enforce Dependency Graph Consistency & Provider Alias Validation
* **Rank:** 3
* **Confidence Level:** Confirmed
* **Focus Category:** Orchestration / Operational Correctness
* **Target Location:** `dev/app-wrapper`, `dev/slack-alerts`, `shared-services/transit-gateway-peering-ca` HCL files, `root.hcl`
* **Evidence:** Units declare explicit `dependency` blocks referencing other units/accounts. Topography reports `0 cross-unit dependencies`. Multiple units set `has_mock_outputs: true`. Provider aliases are generated in per-unit `terragrunt.hcl` via `generate "provider_*"` blocks.
* **Interpretation:** The contradiction between declared dependencies and static metrics indicates a parser limitation, not an absence of dependencies. Mock outputs are actively configured to isolate plans, but alias generation requires strict validation to prevent provider conflicts during deploys. Hardening ensures predictable cross-account resolution.
* **Risk Statement:** Plan failures from provider alias conflicts or missing dependency resolution; misalignment between declared intent and automated metrics masks sequencing risks.
* **Estimated Effort:** M
* **Expected Impact:** Eliminates provider alias conflicts; aligns tooling metrics with actual dependency graph.

### 4. Standardize Module Input/Output Hygiene & Type Constraints
* **Rank:** 4
* **Confidence Level:** Confirmed
* **Focus Category:** Module Design Quality / Operational Correctness
* **Target Location:** `ecs-cluster`, `observability`, `transit-gateway*` modules, consuming units
* **Evidence:** TFLint enforces `terraform_naming_convention` and `terraform_typed_variables`. Many modules lack variable descriptions or strict type constraints. Outputs are inconsistently exposed across the dependency graph.
* **Interpretation:** Inconsistent input/output contracts increase cognitive load and drift risk. Enforcing typed variables with descriptions and explicit outputs aligns with Terragrunt's contract expectations and reduces refactoring fragility.
* **Risk Statement:** Fragile cross-module communication; manual ARN/path tracking increases risk of broken references during account restructuring.
* **Estimated Effort:** S
* **Expected Impact:** Strengthens dependency graph integrity; eliminates hardcoded references for state bucket policies.

### 5. Audit Cross-Account State Bucket Policies for Least Privilege
* **Rank:** 5
* **Confidence Level:** Confirmed
* **Focus Category:** Security / Operations
* **Target Location:** `tfstate-policy` module (`main.tf`, `variables.tf`), consuming units in `shared-services` and `shared-services-ca`
* **Evidence:** Module contains 1 resource (`aws_s3_bucket_policy`), 4 variables, 0 outputs. Depends on `cross-account-state-policy`. Cross-account state access is a core pattern across accounts.
* **Interpretation:** State bucket policies require explicit ARN propagation through the dependency graph. Verifying policy scope ensures spoke accounts only receive `s3:GetObject` on specific key prefixes without over-permissioning.
* **Risk Statement:** Over-permissive state access increases lateral movement risk if a spoke account is compromised; manual policy tracking increases drift risk.
* **Estimated Effort:** S
* **Expected Impact:** Reduces cross-account attack surface; ensures state access aligns with dependency graph scope.

---

## 3. Deferred Opportunities

* **ECS Cluster ASG & Launch Template Optimization:** Instance type sizing, health checks, and detailed monitoring improvements were noted but deferred. Compute optimization yields lower immediate ROI than state access and dependency correctness, and can be addressed in a dedicated capacity planning sprint.
* **Observability Module Separation (Grafana+Loki):** Decoupling Grafana and Loki into separate ECS tasks was considered but deferred. Colocation simplifies current observability deployment and reduces cross-task networking complexity. Splitting would require additional IAM, security group, and dependency graph changes with unproven operational benefit at this scale.
* **State Backend Naming Convention Enforcement:** While `${org}-${account_name}-tfstate-${aws_region}` naming is documented, automated enforcement via Terragrunt hooks or CI validation was deferred. Current manual compliance is sufficient; automation adds complexity without mitigating an active failure mode.

---

## 4. Concrete Implementation Suggestions

### Initiative 1: Validate & Document Checkov Baseline & Trivy Suppressions
* **Action:** Run `checkov --directory . --quiet --compact` and compare output against the existing baseline file. If empty, generate a fresh baseline with `--checkov-config`. Audit Trivy suppressions in `.github/workflows/` against current security group rules and IAM policies. Document suppression rationale and review cadence in `SECURITY.md`.
* **Testing:** Introduce a controlled vulnerability (e.g., overly permissive SG rule) to verify scanning catches it. Confirm baseline updates do not break CI pipelines.
* **Rollback:** Revert to previous baseline file; remove Trivy suppressions if they mask active risks and replace with targeted IAM/SG fixes.

### Initiative 2: Incrementally Decouple `app-wrapper` Module & Streamline Variable Contracts
* **Action:** Extract CloudFront + ACM + Route53 into a separate `cdn-dns` module. Keep ALB + ECS in the primary unit. Reduce variable count by grouping related inputs (e.g., `ecs_config { ... }`, `networking_config { ... }`). Add descriptions and type constraints to all variables. Validate outputs are strictly versioned using `output_version = "1.0"`.
* **Testing:** Deploy extracted module in dev account alongside existing `app-wrapper`. Verify ALB target groups, ECS service registrations, and DNS records function identically. Run TFLint to enforce `terraform_typed_variables` and `terraform_naming_convention`.
* **Rollback:** Merge modules back together if dependency graph conflicts arise; maintain original variable contract as fallback.

### Initiative 3: Enforce Dependency Graph Consistency & Provider Alias Validation
* **Action:** Pin provider versions in consuming units using `required_providers { aws = { source = "hashicorp/aws", version = "~> 6.0" } }`. Add explicit `depends_on` to route table propagation blocks to ensure CIDR advertisement completes before peering attachment activation. Validate `generate "provider_accepter"` aliases do not conflict with root provider configurations.
* **Testing:** Deploy peering in a non-prod region pair first. Verify TGW route tables propagate both `10.1.0.0/16` and `10.2.0.0/16` correctly. Confirm no routing loops via VPC flow logs.
* **Rollback:** Disable peering attachment in route tables; revert provider alias generation to static blocks if conflicts arise.

### Initiative 4: Standardize Module Input/Output Hygiene & Type Constraints
* **Action:** Add explicit outputs to `tfstate-policy/main.tf`: `output "bucket_policy_arn" { value = aws_s3_bucket_policy.this.arn }`. Update consuming units to reference these outputs via `dependency` blocks instead of hardcoded paths or data sources. Validate that `cross-account-state-policy` role ARNs are passed explicitly through the dependency chain.
* **Testing:** Run `terragrunt plan` in `shared-services` and `shared-services-ca` to verify policy application succeeds without manual ARN tracking. Confirm cross-account state access works post-deploy.
* **Rollback:** Revert to previous indirect references if output propagation causes circular dependency errors; add `skip_outputs = true` temporarily during rollout.

### Initiative 5: Audit Cross-Account State Bucket Policies for Least Privilege
* **Action:** Replace blanket `mock_outputs` with conditional fallback logic using `locals { state_exists = try(dependency.X.outputs.state_bucket_arn, null) != null }`. Use `skip_outputs = false` only when state is accessible. Add a CI step validating dependency graph consistency via `terragrunt hclfmt --check` and `tflint --enable-plugin terragrunt`.
* **Testing:** Run `terragrunt plan` in an isolated dev account with intentionally denied S3 state access to verify graceful fallback behavior. Validate that mock outputs are only used when state is genuinely unavailable, not as a bypass for missing dependencies.
* **Rollback:** Revert to previous `mock_outputs` configuration if fallback logic introduces plan drift; maintain backup state keys in each account.
