# Specialist architecture

Infrastructure Migration Agent is one product with a governed coordinator and seven focused LangGraph specialist subgraphs. New runs use `specialists-v1`. These are typed workflow specialists; only the optional reasoning specialist uses an LLM. Deterministic discovery, assessment, generation and compilation remain tools, not free-form model decisions.

```mermaid
flowchart LR
 D[Discovery] --> A[Assessment] --> R[Advisory reasoning]
 R --> P[Migration planning] --> C[Recovery planning]
 C --> G[Code generation] --> V[Validation]
 V --> H[Human package review]
 H --> B[Execution blocked]
```

The order follows data dependencies. The coordinator does not spawn unrelated agents or parallelize ownership changes. Specialist implementation is in `specialists.py`; each component has a private stateless LangGraph graph, strict Pydantic input/output contracts, and fixed injected tools. The parent `MigrationPipeline` persists results after each stage and owns the review interrupt. Specialists receive their own typed request, not the supervisor state dictionary, authentication context, credentials, executor, approval minting or adapter admission.

| Specialist | Input/output | Tools and limits |
| --- | --- | --- |
| Discovery | Trusted scope → scoped inventory | Existing bounded scoped reader or explicit snapshot callback; different scope rejected |
| Assessment | Inventory → deterministic candidate assessment | Existing mapping/ownership/dependency policy; no cloud or model calls |
| Advisory reasoning | Inventory → typed proposal and model receipt | Existing metadata-only reviewer, fixed document allowlist, persistent service call budget; optional |
| Migration planning | Bound assessment, inventory, selection, proposal → dependency waves and blockers | Deterministic dependency ordering; missing/cyclic dependencies, invented IDs, different assessment and model blocks rejected |
| Recovery planning | Bound inventory/plan → ownership-aware stop conditions and VPC/subnet observation requirements | No side effects; `qualified=false`; source re-adoption, state restore and deletion are never assumed safe rollback |
| Code generation | Bound inventory/selection plan → exact Pulumi project bundle | Fixed VPC/subnet generator and provider versions; no arbitrary model code execution |
| Validation | Bound project bundle → compiler receipt | Fixed directory and isolated compiler only; artifact verification before and after tool execution |

## Handoff and review integrity

Each completed stage emits a strict receipt with specialist/version, scope digest, previous handoff digest (or scope/selection seed), output digest, and `execution_enabled=false`. The coordinator verifies stage order and all prior state bindings before each specialist and at snapshot/review. Receipts include no customer tags, configuration, credentials or reasoning text. Typed outputs remain in the protected checkpoint. Unknown grant fields, changed scope/selection/output, reordered stages and incomplete review chains fail closed.

These hashes detect inconsistent state; they are not signatures, independent verification or tamper-proof audit custody. Trusted database/OS administrators can rewrite checkpoint material and remain outside this control's threat boundary. Models cannot generate authoritative handoff receipts or change execution policy.

The human review digest binds assessment, advisory proposal/receipt, migration plan, recovery plan, exact code bundle, compiler receipt, version and complete handoff chain. Authorized service members can inspect planning/recovery in the run result; the dashboard presents sequence and recovery requirements. Acknowledgement records review of this exact package; cloud execution remains blocked and requires separate execution approval/qualification. The service enforces a distinct reviewer and fresh database membership. The legacy local CLI retains its trusted single-operator acknowledgement convention.

## Restart and compatibility

Existing checkpoints without the version marker use the isolated compatibility implementation in `legacy_pipeline.py`, preserving original node names and package digest. They are not upgraded during review. New runs always use the specialist version. Unknown workflow versions are rejected. Keep the compatibility module until operators have reconciled all outstanding legacy jobs; do not delete pending checkpoints to force conversion.

After interruption, the parent graph resumes the failed pure preparation stage or its review interrupt. Completed stages and successful model review are not repeated when their checkpoint is present. A crash before a stage's durable output may rerun that stage. File generation is deterministic and verifies pre-existing output. Model reservation remains persistent, so ambiguous model calls consume budget rather than silently becoming unlimited. No specialist graph invokes live cloud writes; exactly-once cloud effects are not inferred from checkpoints.

Live AWS/model qualification, SSO, encrypted evidence custody, worker monitoring/recovery, broader adapters and deployment acceptance remain separate gates. This refactor does not extend supported cloud/resource/framework coverage.

The architecture follows [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) with parent-controlled checkpoints and scoped specialist subgraphs.
