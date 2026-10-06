# Infrastructure migration and recovery runbooks

Execution adapters are implemented but have not passed live cloud acceptance. Follow these procedures only after exact-adapter qualification and authenticated, scoped authorization. Preparation and proposal CLI commands perform no cloud writes.

## Manual infrastructure adoption

1. Authenticate, establish tenant/account/region/resource scope, verify provider identity, and obtain read-only access. Approve any inventory-service setup separately.
2. Discover resources and configuration, identify ownership and dependencies, and report coverage gaps. Agree on resource-specific health checks.
3. Freeze relevant manual and automated changes for the bounded migration window. Record baseline resource IDs, configuration, health and destination state.
4. Generate code and import manifests using qualified resource adapters. Install dependencies in an isolated build environment without cloud credentials.
5. Run compilation, policy and semantic checks. Produce a structured adoption plan and review all destination differences. Keep existing resources protected.
6. Approve the exact bounded import batch. Acquire a fenced lock and recheck drift and state before execution. New drift invalidates approval.
7. Journal each operation before submitting it. Import using exact resource identifiers and the pinned provider. A partial batch stops further work until reconciled.
8. Verify resource identities, configuration, destination state, explained zero-change preview and application health. Release the window only after operator acceptance and evidence retention.

## CloudFormation ownership transfer

1. Complete assessment and validation above, including stack parameters, source/live drift, nested stacks, references, exports, custom resources and external consumers. Block unsupported semantics.
2. Freeze the original deployment pipeline and identify the current owner of every resource. Prepare resource-specific recovery before releasing ownership.
3. Prepare and separately approve source retention changes. Verify retention behavior through actual stack configuration and reviewed changes; source text alone is insufficient.
4. Determine a dependency-safe release/adoption order. Avoid leaving an uncontrolled window of overlapping managers. Hold the execution lock throughout the transfer.
5. Release only the approved resources from source management without deleting physical infrastructure. Stop on unexpected changes or unknown outcomes.
6. Import the same physical resources into the destination. Verify ownership, configuration, preview and health before the next batch.
7. Retire source automation only after all intended resources are accounted for. Do not delete a whole source stack merely to simplify migration.

## Unknown external outcome

Stop new writes for the affected scope. Preserve logs, journal and tool receipts. Query actual cloud resources, source manager and destination state with read-only tools. Match provider request IDs and resource identities. Classify the attempt as succeeded, failed without effect, partially completed, or unresolved. Retry only when absence of the intended effect is demonstrated and authorization remains valid. Otherwise require a resource-specific operator decision.

## Recovery by transfer stage

| Failure stage | Recovery requirement |
| --- | --- |
| Assessment or generation | Preserve artifacts and blockers; no cloud recovery is needed |
| Retention update | Observe stack outcome; reconcile before further source changes |
| Ownership released, import absent | Keep physical resources unchanged and source automation frozen; use tested destination import or source re-adoption procedure |
| Partial destination import | Compare every manifest identity with state and cloud; do not re-import the whole batch blindly |
| Destination controls resources | Use tested source re-adoption if feasible or an approved forward correction; never restore old state to conceal management changes |
| Configuration or health changed | Stop, preserve evidence, activate the resource/application recovery procedure and obtain explicit approval for corrective writes |

There is no universal rollback guarantee. Source re-adoption may be unsupported. A backup of an IaC state file cannot undo a changed cloud resource or restore lost data. Migration batches whose recovery cannot be demonstrated remain blocked.
