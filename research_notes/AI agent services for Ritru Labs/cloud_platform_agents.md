# AI Agents for Cloud Platform Engineering (Landing Zones, Migration, FinOps, IaC/Drift, Governance, Kubernetes Upgrades), as of October 2026

Research date: 2026-10-11. Method: web search (17 tool calls). Note: direct page fetches to aws.amazon.com, learn.microsoft.com and infoq.com failed (DNS errors in the research sandbox), so several AWS/Microsoft facts come from search-result extracts of those primary pages rather than full-page reads. Those cases are flagged.

## 1. What do AWS, Azure and Google Cloud already provide (agents/tools, cost, GA vs preview, single-cloud)?

### Takeaway
By late 2026 all three hyperscalers ship agents for migration planning, operations/incident response, cost anomaly analysis and IaC/MCP access, often free or bundled. Migration agents (AWS Transform for VMware/mainframe/.NET) are free. Ops agents (AWS DevOps Agent, Azure SRE Agent) are GA and metered. Google's Cloud Assist is mostly free preview. Each is built around its own cloud: none generates or governs landing zones across clouds. AWS already has an open-source LZA MCP server (Mar 2026). Azure Copilot Migration Agent (preview) generates Azure platform landing zone Terraform but does not deploy it.

### Cited Findings

**AWS**
- AWS Transform pricing page (via search extract): Mainframe modernization agent = Free; VMware migration agent = Free; Custom transformation agent = Paid; "Custom transformation agent – managed .NET transformation" = Free. Paid custom transformations and "AWS Transform continuous modernization" are metered at $0.035 per agent-minute. The Visual Studio (VSIX) .NET integration is free. — [AWS Transform Pricing](https://aws.amazon.com/transform/pricing/); [AWS Transform FAQs](https://aws.amazon.com/transform/faq/)
- AWS Transform reached GA on 15 May 2025. It is a browser-based, multi-agent experience with human approval gates covering Windows/.NET/SQL Server, mainframe, VMware-to-EC2 and custom transformations. — [Qovery blog (secondary)](https://www.qovery.com/blog/aws-transform-what-it-does-and-what-it-costs); [Techzine](https://www.techzine.eu/blogs/infrastructure/131452/aws-transform-reaches-ga-legacy-gets-the-boot/)
- A third-party review says language/framework code transformations are paid while Windows/VMware/mainframe migrations are free. This only matches the official table if .NET runs through the "managed" path. — [StackPick review](https://stackpick.net/tools/aws-transform/)
- AWS DevOps Agent went GA on 31 March 2026 (What's New post). It is billed per second of active agent time at about $0.0083 per agent-second (about $0.50 per agent-minute, roughly $30 per agent-hour). AWS Support customers get monthly DevOps Agent credits equal to 100% (Unified Operations), 75% (Enterprise Support) or 30% (Business Support+) of the prior month's Support spend. Queries the agent runs against other AWS services (e.g., CloudWatch Logs Insights) are billed separately. — [AWS What's New: DevOps Agent GA](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-devops-agent-generally-available); [InfoQ GA coverage](https://www.infoq.com/news/2026/04/aws-devops-agent-ga/); [re:Post cost forecasting article](https://repost.aws/articles/AR0juLZf1VT8yQD_kj4QEK3Q/forecasting-aws-devops-agent-costs-for-your-organization); [devopsinside "$30/hour"](https://devopsinside.com/the-30-hour-is-the-aws-devops-agent-a-genius-or-a-gold-digger/)
  - Conflicts: secondary sources disagree on a 2-month free trial (unconfirmed by AWS), the billing start date (one says 10 April 2026), and region count (6 vs 11). — [aiagentslist review](https://www.aiagentslist.io/agents/aws-devops-agent); [techjacksolutions](https://techjacksolutions.com/ai-tools/aws/what-is-aws-devops-agent/)
- AWS publishes an "eks-upgrade-readiness" skill for DevOps Agent. It answers natural-language questions such as whether a cluster is ready for a target version, and checks deprecated APIs and add-on compatibility. — [AWS tools-for-devops-agent: eks-upgrade-readiness](https://aws.github.io/tools-for-devops-agent/skills/eks-upgrade-readiness/)
- EKS upgrade insights scan clusters for upgrade-impacting issues. Since March 2025, EKS blocks control-plane upgrades while insights are unresolved (an override flag exists). — [AWS What's New Mar 2025](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-eks-enforces-upgrade-insights-check-cluster-upgrades)
- A single news article reports EKS Version Rollback (July 2026), which reverts to the previous minor version within 7 days via UpdateClusterVersion. Not verified against AWS docs. — [The New Stack](https://thenewstack.io/eks-kubernetes-upgrade-rollback)
- The AWS MCP Server went GA on 6 May 2026 as the core of the "Agent Toolkit for AWS". Access is governed by IAM, with IAM context-key support and logging via CloudWatch/CloudTrail. It is available in us-east-1 and eu-central-1, and clients include Claude Code, Kiro and Cursor. Secondary sources say the server itself is free (the AWS resources it touches are billed). — [AWS What's New: AWS MCP Server GA](https://aws.amazon.com/about-aws/whats-new/2026/05/aws-mcp-server/); [compareclaw (secondary)](https://compareclaw.com/blog/post/aws-mcp-server-general-availability-2026)
- **LZA MCP Server (direct competitor to the landing zone idea):** open source, announced 12 March 2026. It has 20 specialized tools: searching docs across LZA versions, managing configurations, monitoring LZA pipelines, surfacing insights on deployment failures, and merging LZA Universal Configuration templates. It runs as a container (Docker/Finch), uses temporary credentials, and needs S3/CodePipeline/CodeBuild/CloudWatch Logs permissions. It works with Kiro, Amazon Q Developer and Claude Code, in all commercial regions plus GovCloud where LZA runs. It was first proposed in GitHub issue #990 (Dec 2025). — [AWS What's New: LZA MCP Server](https://aws.amazon.com/about-aws/whats-new/2026/03/new-lza-mcp-server-ai-assisted); [GitHub awslabs/lza-mcp-server](https://github.com/awslabs/lza-mcp-server); [GitHub issue #990](https://github.com/awslabs/landing-zone-accelerator-on-aws/issues/990); [DeepWiki getting started](https://deepwiki.com/awslabs/lza-mcp-server/1.1-getting-started)
- MCP Proxy for AWS went GA in October 2025. — [AWS What's New Oct 2025](https://aws.amazon.com/about-aws/whats-new/2025/10/model-context-protocol-proxy-available)

**Azure**
- **Azure Copilot Migration Agent (public preview):** a planning-focused experience that reasons over Azure Migrate data. It does not support migration execution. It can generate platform landing zone configurations/templates, with one source specifying a downloadable Terraform template. Deployment happens outside the agent. Users must enable "Agents (preview)" in Azure Copilot and reach it from Azure Migrate's "Accelerate migration" experience. It covers VMware, Hyper-V and bare-metal servers. — [Microsoft Learn: Azure Copilot Migration Agent (preview)](https://learn.microsoft.com/en-us/azure/migrate/azure-copilot-migration-agent?view=migrate); [Microsoft Tech Community blog](https://techcommunity.microsoft.com/blog/azuremigrationblog/azure-copilot-migration-agent/4501292); [4sysops](https://4sysops.com/archives/azure-copilot-migration-agent-ai-assisted-migration-planning-for-vmware-hyper-v-and-bare-metal-servers/)
  - Conflict: InfoQ noted the agent was billed as "generally available" in messaging while documentation says public preview. — [InfoQ Azure news index](https://www.infoq.com/Microsoft-Azure/news)
- Azure Migrate Collector (public preview) supports inventory collection without direct connectivity to Azure. — [4sysops](https://4sysops.com/archives/azure-copilot-migration-agent-ai-assisted-migration-planning-for-vmware-hyper-v-and-bare-metal-servers/)
- **Azure SRE Agent:** billed in Azure Agent Units (AAUs). There is a fixed always-on charge of 4 AAU per agent-hour plus variable "active flow" usage. The product page lists 0.25 AAU per second per task, and from 15 April 2026 active flow is calculated as AAUs per million tokens. Unit price varies by region. A 30-day trial waives always-on charges for up to 3 agents. — [Azure SRE Agent pricing page](https://azure.microsoft.com/en-us/pricing/details/sre-agent/); [Microsoft billing-model blog](https://techcommunity.microsoft.com/blog/appsonazureblog/announcing-a-flexible-predictable-billing-model-for-azure-sre-agent/4427270); [Microsoft for Developers: no always-on charges trial](https://developer.microsoft.com/blog/try-azure-sre-agent-with-no-always-on-charges/)
  - Third-party estimate: at about $0.10/AAU, the always-on baseline is about $292 per agent per month before any work is done (secondary). Secondary sources date GA to about 10 March 2026, but this is not confirmed on Microsoft pages I could access. — [synchronizedcodelab](https://synchronizedcodelab.com/blogs/azure-sre-agent-pricing-guide); [infrazen](https://infrazen.io/blog/azure-sre-agent-production-playbook); [stackpick](https://stackpick.net/pricing/azure-sre-agent/)
- Azure Landing Zones Terraform: Azure Verified Modules (AVM) for Platform Landing Zone reached GA. The classic `Azure/caf-enterprise-scale/azurerm` module is deprecated, gets no updates, and its registry notice said it would be archived on 1 August 2026. Microsoft "strongly recommends" migration (aka.ms/alz/tf/migrate) and supplies tooling that generates Terraform import blocks. That tooling only supports resources the classic module could deploy. — [Microsoft: AVM ALZ GA announcement](https://techcommunity.microsoft.com/blog/azuretoolsblog/announcing-general-availability-of-terraform-azure-verified-modules-for-platform/4366027); [Microsoft: ALZ AVM migration guidance & tooling](https://techcommunity.microsoft.com/blog/azuretoolsblog/terraform-azure-verified-modules-for-platform-landing-zone-alz-migration-guidanc/4432035); [GitHub terraform-azurerm-caf-enterprise-scale](https://github.com/Azure/terraform-azurerm-caf-enterprise-scale); [Terraform Registry](https://registry.terraform.io/modules/Azure/caf-enterprise-scale/azurerm/latest/submodules/management)
- The community `Azure/caf-terraform-landingzones` (aztfmod "rover") repo no longer receives Microsoft contributions and was scheduled for removal on 1 July 2025. Users are told to move to AVM. — [GitHub Azure/caf-terraform-landingzones](https://github.com/Azure/caf-terraform-landingzones)

**Google Cloud**
- Gemini Cloud Assist (Next '26, published 22 April 2026) was repositioned as a multi-agent platform:
  - a redesigned Application Design Center (natural language to architecture to deployable Terraform, with curated catalogs of pre-approved templates and custom Terraform modules, multi-turn updates to existing resources, and Security Command Center integration)
  - proactive alert investigations (Gemini 3)
  - multi-turn ops/incident agents (gcloud, kubectl, Terraform)
  - a 24/7 FinOps agent (cost anomaly root cause correlated with resource creation, autoscaling and pricing changes; natural-language cost reports by project/AppHub app)
  - Cloud Assist MCP servers for IDEs, Gemini CLI, ServiceNow and Slack

  The post gives no GA/preview labels or pricing. Petco reported 60% fewer touch points with dev teams. — [Google Cloud blog: Gemini Cloud Assist at Next '26](https://cloud.google.com/blog/products/application-development/gemini-cloud-assist-at-next26)
- The Gemini Cloud Assist product page states it is "currently in preview and offered free of charge. Select features will incur costs upon general availability." — [Gemini Cloud Assist product page](https://cloud.google.com/products/gemini/cloud-assist?hl=en); [Gemini for Google Cloud pricing](https://cloud.google.com/products/gemini/pricing)
- Google added FinOps controls for Gemini Enterprise agents in August 2026 (pay-as-you-go, pooled quotas, project spend caps, Flexible Savings Plans). This is FinOps *for* AI agents, not a cloud-infra FinOps agent. — [ITPro](https://www.itpro.com/technology/artificial-intelligence/google-targets-ai-cost-efficiency-with-new-finops-features-for-gemini-enterprise)

### Inferences
- The "migration planning + target landing zone" step is now free and native on Azure (preview) and AWS (Transform free agents, LZA MCP). A consultancy cannot charge for "AI generates a landing zone for one cloud" alone. Value has to come from cross-cloud consistency, brownfield adoption, company-specific standards, evidence and approvals, and actually executing and operating the change.
- Hyperscaler agents meter by agent time (AWS DevOps Agent about $30/agent-hour active; AWS Transform custom $0.035/agent-minute; Azure SRE Agent with an always-on baseline). This creates a FinOps-for-agents need that a consultancy can address.
- The Azure Copilot Migration Agent explicitly stops at planning, and Google's Application Design Center targets new designs. Neither covers PR-based execution, policy gates or drift reporting on an existing estate. That gap matches the Ritru landing zone agent idea, but the window is narrowing as vendors add MCP servers.
- The LZA MCP Server focuses on LZA configuration help and pipeline troubleshooting. It does not do requirements gathering, decision records, or Control Tower / Azure / GCP parity.

### Gaps
- Could not verify Amazon Q Developer "transform" (Java upgrade) pricing in 2026, Kiro pricing tiers, or whether a distinct "Amazon Q Developer transform" brand still exists separately from AWS Transform. Searches returned nothing.
- Could not confirm Cost Optimization Hub's current AI features or pricing (historically free). No 2026 source retrieved.
- Could not retrieve the official AWS DevOps Agent pricing page or official Azure SRE Agent GA date (fetch blocked).
- No evidence found on the specific "Azure Migrate platform landing zone generation with GitHub Copilot + Azure MCP server" flow mentioned in the brief. The search found no source linking the Migration Agent to GitHub Copilot. The Azure MCP Server and GitHub Copilot for Azure status/pricing were not retrieved.
- No GA date or pricing found for Gemini Cloud Assist features. Google migration tooling (Migration Center, Migrate to VMs agents) was not covered.
- No AKS- or GKE-native AI upgrade agent was found in searches.
- GCP Cloud Foundation Fabric FAST: no AI/agent tooling found (not specifically searched due to budget).

## 2. What do third-party vendors sell (FinOps agents, IaC/drift, platform engineering agents) and at what price?

### Takeaway
FinOps vendors have added agents (Vantage FinOps Agent, nOps Clara, Finout "Billy" MCP) on top of savings-share or spend-based pricing. IaC platforms (Spacelift Intelligence/Intent, env0 Cloud Analyst, Pulumi Neo, HashiCorp MCP/Infragraph, Firefly AIaC) now ship MCP servers and natural-language provisioning. Pricing is mostly opaque or enterprise-tier gated (e.g., Spacelift paid tiers reportedly start around $20k/year). Most comparison data comes from competitor-written listicles, so treat it cautiously.

### Cited Findings

**FinOps**
- nOps: rate optimization is priced as a share of realized savings; visibility/allocation has a fixed fee based on cloud spend; 14-day trial; no published figures. — [PointFive comparison](https://www.pointfive.co/guides/best-finops-tools)
- ProsperOps: savings-share model, cited at 20–35% of savings delivered via managed commitments. One guide says ProsperOps is now part of Flexera (unverified, secondary). — [usage.ai guide](https://www.usage.ai/blogs/finops/tools/best-ai-agents-for-finops/); [PointFive](https://www.pointfive.co/guides/best-finops-tools)
- Vantage tiers (per PointFive): Starter free up to $2,500/month tracked spend; Pro $30/month up to $7,500; Business $200/month up to $20,000. The Vantage FinOps Agent reportedly charges 5% of savings from commitments it purchases plus a per-token conversation fee. It proposes remediations for individual approval and sits on the Enterprise plan. Sources conflict on coverage: one says AWS/Azure/GCP, another says AWS only with Azure/GCP planned. — [PointFive](https://www.pointfive.co/guides/best-finops-tools); [usage.ai](https://www.usage.ai/blogs/finops/tools/best-ai-agents-for-finops/); [Amnic AI agents roundup](https://amnic.com/blogs/top-ai-agent-tools-for-finops)
- CloudZero: one source cites $19 per $1,000 of monthly cloud spend under management; another says there are no published prices (conflict). CloudZero leads on unit economics and AI token cost attribution. — [cloudaware](https://cloudaware.com/blog/finops-tools/); [PointFive](https://www.pointfive.co/guides/best-finops-tools)
- Finout: sources conflict, citing about 1% of cloud spend annually, "from $6,000/year", or Business/Pro from about $1K/month. Its "Billy" MCP integration works with Claude and Cursor. A review says it does not autonomously buy Savings Plans or RIs. — [Finout comparison](https://www.finout.io/blog/finout-vs.-cloudability-vs.-cloudzero-which-finops-platform-is-right-for-you); [dev.to buyer comparison](https://dev.to/zop_8abedcc7e12/top-10-ai-agent-finops-tools-in-2026-an-honest-buyer-comparison-4j5j)
- nOps "Clara" agent is bundled with nOps, not sold standalone. One comparison reports no GCP/Azure coverage. — [usage.ai](https://www.usage.ai/blogs/finops/tools/best-ai-agents-for-finops/)

**IaC / drift / platform**
- Spacelift "Intent" (natural-language provisioning through Spacelift's MCP) launched 8 October 2025. The umbrella "Spacelift Intelligence" launched 18 March 2026. Spacelift offers a hosted MCP server. — [Pulumi: Best AI infrastructure tools 2026 (competitor-authored)](https://www.pulumi.com/blog/ai-infrastructure-tools/); [Scalr: best Terraform MCP server](https://scalr.com/learning-center/which-terraform-platform-has-the-best-mcp-server)
- Spacelift pricing has a free tier (2 users, 1 concurrency). The entry paid tier, "Starter+", is about $20,000/year per StackPick; PricingSaaS shows "$20000/month" (likely an error). Business/Enterprise prices are undisclosed. — [StackPick Spacelift pricing](https://stackpick.net/pricing/spacelift-ai/); [PricingSaaS](https://pricingsaas.com/companies/spacelift)
- env0 "Cloud Analyst" is gated to its top "Cloud Pilot" tier (as of July 2026). env0's MCP server can deploy and approve environments. — [Scalr MCP comparison](https://scalr.com/learning-center/which-terraform-platform-has-the-best-mcp-server)
- HashiCorp (IBM) ships a Terraform MCP server. "Infragraph" was in public preview as of July 2026. — [Scalr MCP comparison](https://scalr.com/learning-center/which-terraform-platform-has-the-best-mcp-server)
- Pulumi Neo is described as agentic AI that runs deployments within user-set guardrails. Pricing is tied to Pulumi Cloud tiers, with no separate figure found. — [Pulumi AI infrastructure tools blog](https://www.pulumi.com/blog/ai-infrastructure-tools/); [Pulumi TACOS comparison](https://www.pulumi.com/docs/iac/comparisons/tacos/)
- Firefly (firefly.ai) "AIaC" focuses on codifying existing cloud assets and IaC generation. Pricing is freemium plus usage-based on assets under management, with no dollar figures. — [Pulumi IaC tools 2026](https://www.pulumi.com/blog/infrastructure-as-code-tools/); [Firefly academy](https://www.firefly.ai/academy/terraform-cloud-alternatives)
- Kubernetes upgrades: Plural "Upgrade Autopilot" claims EKS/AKS/GKE pre-flight checks and deprecated-API detection (vendor marketing). OpsTree "KubeLift" automates the EKS upgrade lifecycle, EKS only. — [Plural upgrade management](https://plural.sh/products/upgrade-management); [Plural blog](https://www.plural.sh/blog/kubernetes-io-guide/); [OpsTree KubeLift](https://opstree.com/?p=24233)

### Inferences
- Third-party platforms are converging on "MCP server + natural-language provisioning + approval" and are sold as SaaS to platform teams. They are tools, not outcome-based services. A small consultancy can sit on top of these (or the free hyperscaler MCPs) and sell outcomes: a landing zone delivered, an AVM migration completed, an upgrade done.
- FinOps commitment automation (ProsperOps, nOps) is mature and savings-share priced, so it is hard to compete with directly. Gaps lie in unit economics for AI/agent spend and in tying cost findings to IaC PRs.
- Kubernetes upgrade automation is fragmented: an AWS skill, EKS-only tools, and Plural. A multi-cloud, PR-based upgrade-readiness service is plausible.

### Gaps
- No pricing found for StackGen, Brainboard, Firefly (dollar figures), Harness CCM, or Pulumi Neo specifically.
- Most third-party data comes from competitor-authored comparisons (Pulumi, Scalr, PointFive, nOps, Finout), so the claims about rivals are unverified against vendor pricing pages.
- No Gartner Peer Insights data retrieved.

## 3. What do customers complain these tools miss (multi-cloud, brownfield, standards, audit evidence, approvals)?

### Takeaway
Direct practitioner evidence is thin in what I retrieved. What exists points to hallucinated IaC (non-existent resources and keys), reliance on human PR review as the main safeguard, IaC weakness on drift and external changes, and planning-only hyperscaler agents that stop before execution. Single-cloud scope is a structural limit visible in the products themselves rather than in sourced complaints.

### Cited Findings
- A compilation of 66 r/devops practitioner comments (self-described as unverified) rates autonomous agent-driven IaC generation as moderate-to-high risk for hallucinated keys and resources. One commenter claimed hallucinations about "80% of the time" (3 upvotes). Human sign-off before merge, sometimes with an internal review bot, is the usual safeguard. — [GitHub essentialols/ai-infrastructure-as-code-guide](https://github.com/essentialols/ai-infrastructure-as-code-guide)
- A Rackspace engineer building a Terraform agent reported hallucinated and duplicated Terraform code as a major early problem and added human-in-the-loop manual sign-off before changes reach critical infrastructure. — [Rackspace blog](https://www.rackspace.com/blog/building-terraform-ai-agent-automating-cloud-infrastructure-with-ai)
- Plain IaC handles creation but struggles with drift and external events. — [DEV Community](https://dev.to/devopsfundamentals/terraform-fundamentals-bedrock-agents-e1)
- The Azure Copilot Migration Agent "does not support migration execution", and landing zone deployment happens outside the agent. — [Microsoft Learn](https://learn.microsoft.com/en-us/azure/migrate/azure-copilot-migration-agent?view=migrate)
- Microsoft's AVM migration tooling only supports resources the classic CAF Enterprise Scale module could deploy. Customizations outside it need manual handling (a brownfield gap). — [Microsoft ALZ AVM migration guidance](https://techcommunity.microsoft.com/blog/azuretoolsblog/terraform-azure-verified-modules-for-platform-landing-zone-alz-migration-guidanc/4432035)
- FinOps agents vary in cloud coverage (nOps Clara reportedly AWS-only; Vantage agent coverage disputed). — [usage.ai](https://www.usage.ai/blogs/finops/tools/best-ai-agents-for-finops/)
- Azure SRE Agent's always-on baseline (about $292/agent/month, secondary estimate) and AWS DevOps Agent's about $30/active hour prompted "genius or gold digger" style cost criticism. — [devopsinside](https://devopsinside.com/the-30-hour-is-the-aws-devops-agent-a-genius-or-a-gold-digger/); [synchronizedcodelab](https://synchronizedcodelab.com/blogs/azure-sre-agent-pricing-guide)

### Inferences
- Likely unmet needs: one requirements-to-decision-record flow across AWS, Azure and GCP; codifying brownfield estates into official accelerators; encoding company-specific standards as policy; producing audit evidence (decision records, plan output, policy results, approvals); and a vendor-neutral approval workflow. These follow from product scope, not from direct customer quotes.
- Agent metering cost is an emerging complaint category that consultancies can address with cost guardrails.

### Gaps
- Reddit r/aws and r/devops threads were not directly retrieved. Searches did not return them.
- No direct customer complaints found about the LZA MCP Server, Azure Copilot Migration Agent or Cloud Assist specifically.
- No sourced data on audit-evidence requirements (SOC 2/ISO/DORA) for AI-generated infrastructure changes.

## 4. Is there demand for landing zone automation and migration services (VMware exit, mainframe, CAF-to-AVM)?

### Takeaway
Yes, with nuance. VMware reduction is widespread but gradual: 86% are shrinking footprints, yet only 4% have fully migrated, and about 75% of those migrating go to public cloud. Microsoft's forced deprecation of the classic CAF Enterprise Scale Terraform module (archive date 1 August 2026, now past) creates a concrete, time-bound migration workload for every Terraform-based Azure landing zone. AWS making its VMware and mainframe agents free signals that hyperscalers want this migration volume and may subsidize partner delivery.

### Cited Findings
- CloudBolt survey (February 2026, 302 North American IT decision-makers): 86% are actively reducing VMware footprints (some outlets say 87%), and only 4% have completed a full migration. — [Channel Dive](https://www.channeldive.com/news/broadcom-vmware-migrations-costs-cloudbolt-report/812735/); [TechRadar](https://www.techradar.com/pro/vmware-customers-are-still-trying-to-ditch-its-software-two-years-after-broadcom-acquisition?rand=141)
- Rimini Street–commissioned survey (October 2026): about nine in ten respondents say higher licensing costs push them to consider alternatives. Sample size conflicts (269 per The Register vs 300 per BigGo), and the sponsor has an interest in this result. — [The Register, 7 Oct 2026](https://www.theregister.com/virtualization/2026/10/07/nine-in-10-vmware-customers-eye-the-exit-as-licensing-bills-bite/5301591); [BigGo Finance](https://finance.biggo.com/news/ba9c1849-d4fa-4ae5-ad26-a69ecab43157)
- "Mass exodus never happened": enterprises are scaling back gradually, and close to 75% of those migrating go to public cloud rather than another hypervisor. — [Everywan](https://everywan.com/en/blog/vmware-broadcom-two-years-later-the-exodus-that-wasnt); [ServnetUK market share 2026](https://www.servnetuk.com/research/virtualisation-market-share-2026)
- The classic CAF Enterprise Scale Terraform module is deprecated with a planned 1 August 2026 archive. Microsoft "strongly recommends" migrating to AVM for Platform Landing Zone and supplies state-migration and import-block tooling. — [GitHub caf-enterprise-scale](https://github.com/Azure/terraform-azurerm-caf-enterprise-scale); [Microsoft migration guidance](https://techcommunity.microsoft.com/blog/azuretoolsblog/terraform-azure-verified-modules-for-platform-landing-zone-alz-migration-guidanc/4432035)
- The aztfmod CAF landing zones repo was scheduled for removal on 1 July 2025, and its users must also move to AVM. — [GitHub Azure/caf-terraform-landingzones](https://github.com/Azure/caf-terraform-landingzones)
- AWS Transform's VMware and mainframe agents are free. — [AWS Transform pricing](https://aws.amazon.com/transform/pricing/)

### Inferences
- The CAF-to-AVM migration is the most concrete, defensible near-term wedge for a small consultancy. It is deadline-driven, Terraform-state-heavy (import blocks, `moved` blocks, plan diffs) and brownfield. Microsoft's tooling explicitly excludes custom resources, and an agent that does plan-only diffing plus PRs fits it well.
- VMware exit demand favors "assessment + landing zone + wave planning" packages, but hyperscaler free agents compete directly on planning. The services value is in execution, landing zone readiness and multi-cloud choice.

### Gaps
- No 2026 data found on mainframe modernization demand or AWS Transform mainframe adoption numbers.
- No quantified data on how many organizations still run the classic CAF Terraform module.
- No data on landing zone services market size or typical consultancy pricing for landing zone engagements.

## 5. What safe-automation patterns are used (plan-only, PR-based, policy-as-code gates, read-only roles)?

### Takeaway
The patterns in the sources are: human approval gates and PR review before merge or apply; agents that stop at planning or template generation, with deployment run by the customer's pipeline; IAM-scoped, temporary-credential MCP servers with CloudTrail logging; and platform-level approval in IaC tools (env0 MCP "approve environments", Pulumi Neo "guardrails"). Policy-as-code engines (OPA, Checkov, Sentinel) are standard industry practice, but I did not retrieve 2026 primary sources specific to agent workflows.

### Cited Findings
- AWS Transform uses human approval gates in its multi-agent flow. — [Qovery](https://www.qovery.com/blog/aws-transform-what-it-does-and-what-it-costs)
- The AWS MCP Server is governed by IAM, with IAM context keys (enabling policies that distinguish agent-originated calls), CloudWatch metrics and CloudTrail logging. — [AWS What's New: AWS MCP Server GA](https://aws.amazon.com/about-aws/whats-new/2026/05/aws-mcp-server/)
- The LZA MCP Server uses temporary credentials, and its listed permissions are S3, CodePipeline, CodeBuild and CloudWatch Logs. Deployment remains in the LZA pipeline. — [AWS What's New: LZA MCP](https://aws.amazon.com/about-aws/whats-new/2026/03/new-lza-mcp-server-ai-assisted); [DeepWiki](https://deepwiki.com/awslabs/lza-mcp-server/1.1-getting-started)
- The Azure Copilot Migration Agent produces landing zone configurations "ready for deployment workflows". Execution occurs outside the agent, which keeps the agent in plan-only mode. — [Microsoft Learn](https://learn.microsoft.com/en-us/azure/migrate/azure-copilot-migration-agent?view=migrate)
- Google's Application Design Center lets platform teams curate pre-approved templates and custom Terraform modules, constraining agent output to approved building blocks. — [Google Cloud blog](https://cloud.google.com/blog/products/application-development/gemini-cloud-assist-at-next26)
- Vantage FinOps Agent proposes remediations that users approve individually. — [PointFive](https://www.pointfive.co/guides/best-finops-tools)
- Practitioners rely on PR review (sometimes with review bots) before merge. Rackspace adds manual sign-off before critical changes. — [essentialols guide](https://github.com/essentialols/ai-infrastructure-as-code-guide); [Rackspace](https://www.rackspace.com/blog/building-terraform-ai-agent-automating-cloud-infrastructure-with-ai)
- EKS enforces upgrade-insight checks as a gate before control-plane upgrades. — [AWS What's New Mar 2025](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-eks-enforces-upgrade-insights-check-cluster-upgrades)

### Inferences
- The Ritru landing zone agent design matches current best practice and should be pitched as such: read-only discovery role, generate configs for official accelerators, policy checks and plan in CI, PR for human approval, apply by the customer's pipeline, then scheduled drift reports. Its differentiators should be multi-cloud parity, brownfield import/AVM migration, company standards as policy, and an audit evidence bundle (decision record, plan, policy results, approver, apply log).
- Using hyperscaler MCP servers (AWS MCP Server, LZA MCP, Azure MCP, Cloud Assist MCP) as tools, rather than rebuilding them, lowers build cost and positions Ritru as the cross-cloud orchestration and governance layer.

### Gaps
- No 2026 primary sources retrieved on OPA/Conftest, Checkov or Sentinel used specifically as gates for AI-generated IaC, though these are well-established tools.
- No sourced data on how many enterprises allow agents write access versus read-only.
