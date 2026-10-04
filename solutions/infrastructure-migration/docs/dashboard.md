# Operator dashboard

The installed service serves the dashboard at `/`. It uses the authenticated preparation API and actual PostgreSQL run records; there is no mock-data mode or cloud execution action.

## Connect and prepare

Start the provisioned API and preparation worker using [service deployment](service-deployment.md), then open the service root. Enter your organization UUID and a short-lived access token issued by the configured identity provider. Browser redirect/SSO login and automatic signing-key rotation remain pending. The token stays in tab memory, is never written to local/session storage, is cleared from the input after connection, and is discarded on disconnect, navigation, reload, or expiry. Serve customer deployments over HTTPS with the documented ingress and logging controls.

An assessor uploads an inventory JSON snapshot under 900 KB containing at most 1,000 resources. The dashboard checks basic scope and presents resource selection; the service validates the full schema and provisioned scope. Unsupported resource types are disabled in the selector. Subnet batches must also include their VPC. Uploaded inventories remain unverified snapshots and are blocked from live adoption.

Submitting creates an idempotent preparation job. A retry of the same unchanged submission recovers its original run. Scope/selection changes create a new intent. The preparation worker performs LangGraph assessment, generation, validation and durable review interruption. Runs show their real state, compile/model configuration, blockers, and disabled cloud execution. Metrics describe currently loaded runs, not a claimed organization-wide inventory. The list is cursor-paginated, loads at most 200 rows into the browser, and polls every five seconds while visible.

## Review and download

Select a run to inspect validation and migration blockers. The file selector previews generated TypeScript, exact import manifests, expected observed inputs and project configuration as plain text. Preview and ZIP export verify the bound artifact digest and reject extra/changed files, links and malformed manifests. Preview filenames must belong to the verified bundle; arbitrary filesystem paths are rejected. No generated code is executed in the browser.

The requester can cancel queued or awaiting-review work. A distinct authorized reviewer can approve or reject the exact package after acknowledging code/check/blocker review. The server rechecks role, requester separation, state and package digest; hiding a button is not authorization. Decisions are queued and resumed by the worker after restart. Approval results in `REVIEWED_EXECUTION_BLOCKED`; rejection requires a new package. Neither authorizes cloud changes.

Use a separate reviewer session/account to record the decision. Disconnect clears snapshot, resource, code and review data from the interface. The browser makes same-origin API requests only, does not accept an arbitrary API endpoint, does not send cookies, and refuses redirects. CSP restricts scripts/styles/connections to the service origin and blocks framing. Customer strings are inserted as text rather than HTML.

## Browser qualification

Install the locked Python dependencies and Chromium:

```sh
python -m playwright install --with-deps chromium
```

Run the normal suite with `INFRA_TEST_POSTGRES_DSN` pointing only to an explicitly disposable PostgreSQL instance, `INFRA_RUNNER_IMAGE` set to the immutable compiler image, and `INFRA_BROWSER_TESTS=1`. `INFRA_BROWSER_ARTIFACTS` optionally selects the screenshot output directory. GitHub CI provides these dependencies and retains browser screenshots for seven days.

The real Chromium campaign traverses authenticated HTTP requests, tenant PostgreSQL records and the LangGraph worker, then performs separate-reviewer approval, protected ZIP download, rejection, cancellation, responsive layout and reload/session-clearing checks. It also verifies that untrusted code/tag text does not execute. Screenshots use synthetic fixtures; they are not live AWS or customer acceptance evidence.

Browser automation and local visual inspection establish implementation checks. Hosted identity-provider interoperability, customer usability/accessibility acceptance, deployment security, operational recovery and live cloud qualification remain separate gates.
