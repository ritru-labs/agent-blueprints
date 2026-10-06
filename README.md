# Agent blueprints

This repository contains independent enterprise agent solutions. Each solution documents its scope, trust boundaries, implemented capabilities, and qualification requirements.

| Solution | Purpose | Included capability |
| --- | --- | --- |
| [Jira to PR](solutions/jira-to-pr/architecture/ADR-0001-phase-0.md) | Governed coding and draft PR delivery | Architecture document |
| [Infrastructure migration](solutions/infrastructure-migration/README.md) (**archived, reference only**; continued as the IaC agent on `feat/iac-agent-v0`) | Governed AWS adoption and CloudFormation migration to Pulumi using LangGraph | AWS reader, model review, Pulumi generation, isolated validation, and gated execution adapters |

Read each solution's instructions and acceptance status before enabling external integrations. Design documents and fixture tests do not establish production readiness.
