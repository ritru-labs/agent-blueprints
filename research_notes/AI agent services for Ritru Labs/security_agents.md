# AI Agents for Cybersecurity Work (state as of October 2026) and Service Openings for Ritru Labs

Research date: 2026-10-11. Method: about 18 web searches and fetches. Two primary pages could not be fetched because their DNS lookups failed (labs.cloudsecurityalliance.org, levelblue.com). Many figures below come from search-result summaries of secondary pages rather than full reads of the primary documents. Each bullet notes whether the source is the vendor, independent, or secondary. **Much of the comparison content in this market is written by vendors, including content that claims to be independent.** One SOC 2 comparison page warns that nearly every "SOC 2 platform comparison" is written by a platform or a paid affiliate ([Aliteq](https://aliteq.com/vanta-vs-drata-vs-secureframe)).

---

## Q1. What exists and what does it cost (platform vendors, AI SOC startups, remediation, compliance automation)?

### Takeaway
All the large platforms now ship agents, mostly built into their own suites. Microsoft prices by consumption (SCUs) but now bundles capacity into M365 E5. CrowdStrike and Palo Alto are opening agent builders and orchestration layers. Well-funded AI SOC startups (Exaforce, 7AI, Dropzone, Prophet) sell overlays that work across tools. Remediation agents (Wiz Green Agent, Copilot Autofix, Snyk Agent Fix, Mobb) converge on one pattern: produce a fix and open a PR for a human to review. Compliance platforms are mid-five-figure SaaS with quote-based pricing. Public per-agent pricing is rare everywhere.

### Cited Findings

**Microsoft Security Copilot (platform)**
- Security Copilot is included in Microsoft 365 E5 and E7. Eligible tenants are provisioned automatically, with no Azure setup or capacity purchase. The allotment is 400 SCUs per month per 1,000 user licenses, capped at 10,000 SCUs per month. Rollout began November 18, 2025 and continues "in the upcoming months" — [Microsoft Learn: Security Copilot inclusion FAQ](https://learn.microsoft.com/en-us/copilot/security/security-copilot-inclusion); [Microsoft Security blog, 2025-11-18](https://www.microsoft.com/en-us/security/blog/2025/11/18/agents-built-into-your-workflow-get-security-copilot-with-microsoft-365-e5/)
- A Tech Community post (not official product documentation) says the tenant rollout is phased between April 20 and June 30, 2026, with 7 days' notice before activation. Official pages give no dates — [Microsoft Tech Community discussion](https://techcommunity.microsoft.com/discussions/microsoft-security/security-copilot-agents-in-defender-xdr-where-things-actually-stand/4514689)
- Standalone pricing according to third-party guides: provisioned capacity is $4 per SCU-hour and overage is $6 per SCU-hour. One always-on SCU costs about $2,920 per month — [SAMexpert licensing guide](https://samexpert.com/security-copilot-licensing-guide/); [Redress Compliance](https://redresscompliance.com/microsoft-security-copilot-pricing-guide)
- Microsoft does not publish per-run SCU rates for most agents. One third-party catalogue estimates about 0.5 SCU per run for several triage agents, and notes that Microsoft points customers to the in-tenant usage dashboard instead — [SCU Calculator agent reference](https://www.scucalculator.com/agents)
- The Phishing Triage Agent, renamed the Security Alert Triage Agent, is GA for email and collaboration alerts (phishing). Its extension to identity and cloud alerts (starting with containers) is in **preview** — [Microsoft Defender docs: Security Alert Triage Agent](https://learn.microsoft.com/en-us/defender-xdr/security-alert-triage-agent); [Security Copilot blog](https://techcommunity.microsoft.com/blog/securitycopilotblog/from-alert-overload-to-decisive-action-how-security-copilot-agents-are-transform/4504213)
- There are four Intune agents: Policy Configuration, Change Review, Device Offboarding, and Vulnerability Remediation. They consume SCUs and require an Intune license. Agent 365, the governance layer for AI agents across Entra, Purview, and Defender, is a separate $15/user/month add-on that went GA on May 1, 2026 (third-party guide; not verified on a Microsoft price list) — [aguidetocloud Security Copilot guide](https://www.aguidetocloud.com/licensing/security-copilot/)

**CrowdStrike Charlotte AI**
- At RSAC 2026, CrowdStrike announced Charlotte AI AgentWorks, a no-code builder for custom agents on Falcon that can use external frontier models. Launch partners include Anthropic, AWS, NVIDIA, OpenAI, Accenture, Deloitte, Kroll, Salesforce, and Telefónica Tech. GA status was not confirmed — [VentureBeat RSAC 2026](https://venturebeat.com/security/rsac-2026-agentic-soc-agent-telemetry-security-gap); [CrowdStrike Agentic Security Workforce page](https://www.crowdstrike.com/en-us/platform/charlotte-ai/agentic-security-workforce/)
- CrowdStrike says its Detection Triage Agent is ">98% accurate" against decisions by its Falcon Complete MDR team. **This is a vendor claim** — [CrowdStrike press release](https://www.crowdstrike.com/en-us/press-releases/crowdstrike-unleashes-agentic-security-workforce-to-transform-security-operations)

**Palo Alto Networks Cortex AgentiX / XSIAM**
- According to a roundup published by Palo Alto itself, Cortex AgentiX launched in late 2025 as the successor to XSOAR. It shipped first inside Cortex Cloud and XSIAM and was due to reach Cortex XDR and a standalone platform in early 2026. No explicit GA press release was verified — [Palo Alto Cyberpedia AI SOC tools](https://www.paloaltonetworks.com/cyberpedia/ai-soc-tools-comparison)
- VentureBeat reports that CrowdStrike, Cisco, and Palo Alto all shipped agentic SOC tools at RSAC 2026. It says all three left an "agent behavioral baseline" gap, meaning they have no telemetry baseline for the behaviour of the agents themselves — [VentureBeat](https://venturebeat.com/security/rsac-2026-agentic-soc-agent-telemetry-security-gap)

**Google Security Operations (Gemini agents)**
- A vendor comparison says the Google SecOps Triage Agent has processed 5M+ alerts and cut 30-minute analysis to 60 seconds. It also says the newer threat-hunting and detection-engineering agents are still in **preview**. Google's SecOps remote MCP server is reported as GA — [Prophet Security: Top 5 AI SOC platforms](https://www.prophetsecurity.ai/blog/top-5-ai-soc-analyst-platforms) (competitor-authored; not verified on Google's docs)

**AI SOC startups (funding as a proxy for scale)**
- Exaforce raised a $125M Series B on May 12, 2026, at a $725M valuation, bringing total funding to about $200M — [SC World](https://www.scworld.com/brief/ai-cybersecurity-startup-exaforce-raises-125-million); [Ventureburn](https://ventureburn.com/exaforce-raises-125m-series-b-to-scale-ai-native-security-operations-platform/)
- 7AI raised a $130M Series A in December 2025, led by Index Ventures, at about a $700M valuation. Total funding is about $166M — [BuildMVPFast market map](https://www.buildmvpfast.com/blog/ai-cybersecurity-funding-boom-exaforce-market-map-2026) (secondary)
- Dropzone AI raised a $37M Series B in July 2025, led by Theory Ventures. Reported total funding conflicts: $57.4M in one source, $94.35M in another — [parsers.vc](https://parsers.vc/startup/dropzone.ai/); [checkthat.ai](https://checkthat.ai/brands/dropzone-ai)
- Prophet Security raised a $30M Series A in July 2025, led by Accel with Bain Capital Ventures. Total funding is about $41M over 3 rounds — [Tracxn](https://tracxn.com/d/companies/prophet-security/__kJs_oWGhcSlK1rmcWvyLCq8cSEVT3gEKFjNCQyhVS5c)

**Vulnerability and code remediation**
- **Wiz Green Agent:** it starts from a Wiz issue and gathers context (attack paths, ownership, past fixes). It then builds a plan that a coding agent executes, ending in a PR for review. Wiz MCP supports a similar flow: find the issue, trace it to the owning repo, generate a fix, open a PR — [Wiz blog: agentic coding](https://wiz.io/blog/securing-software-age-of-agentic-coding); [Wiz MCP blog](https://www.wiz.io/es-es/blog/introducing-wiz-mcp)
- **GitHub Copilot Autofix:** in the public beta (2024), SQL injection flaws were fixed in 18 minutes on average versus 3.7 hours manually. A later GitHub-sourced figure says remediation is about 3x faster overall and up to 12x faster for SQLi (vendor). In June 2026 Autofix came to Azure DevOps, where it uses the Copilot coding agent to open PRs — [TechTarget](https://www.techtarget.com/searchsecurity/news/366603045/GitHub-Copilot-Autofix-tackles-vulnerabilities-with-AI); [InfoQ, June 2026](https://www.infoq.com/news/2026/06/azuredevops-copilot-autofix/)
- **Snyk Agent Fix:** Snyk markets 80% autofix accuracy and an 84% cut in MTTR (vendor). It validates each patch with a sandboxed unit-test pass and re-runs Snyk Code on the result — [Safeguard field test 2026](https://safeguard.sh/resources/blog/snyk-agent-fix-autofix-field-test-2026)
- **Mobb:** a remediation layer that turns scanner findings into ready-to-merge PRs, favouring deterministic, validated changes. Its merge rate is not published — [Pixee: best automated remediation tools 2026](https://www.pixee.ai/blog/best-automated-remediation-tools-2026) (competitor-authored)

**Compliance automation (price indications; most are quote-based)**
- **Vanta:** Vendr's median is about $20,000 per year, with a range of $7,500–$57,221. Entry is around $10–12K per year — [Aliteq comparison](https://aliteq.com/vanta-vs-drata-vs-secureframe); [Cavanex](https://cavanex.com/blog/soc-2-compliance-platforms-compared-2026)
- **Drata:** entry is about $7,500 per year and the Vendr median is $25,000. **Secureframe:** the Vendr median is $20,000. **Sprinto:** about $7.5–10K per year; its $15K Vendr median rests on very few deals — [Sprinto blog (competitor-authored)](https://sprinto.com/blog/secureframe-vs-vanta-vs-drata/); [SecureLeap](https://www.secureleap.tech/blog/soc-2-tools-vanta-drata-secureframe-guide-2025)
- Drata covers NIS2 and DORA through framework mapping, not as native EU-first modules. EU-native alternatives such as Orbiq treat ISO 27001, NIS2, and DORA as primary frameworks — [Orbiq: Vanta alternatives](https://www.orbiqhq.com/comparisons/vanta-alternatives) (competitor-authored)

### Inferences
- The leading platforms (Microsoft, CrowdStrike, Palo Alto, Google) will absorb basic triage for customers who already use only their stack. Bundling Security Copilot into E5 means that for Microsoft-heavy mid-market firms, an alert-triage agent is now effectively free. A consultancy should not sell "a triage agent" to those firms. It should sell configuration, tuning, governance, and measurement of the agents they already own.
- AgentWorks, Google's MCP server, Wiz MCP, and AgentiX are all moves toward **open agent platforms**. They create integration work: custom agents, connectors, playbooks. Large SIs (Accenture, Deloitte, Kroll) are launch partners, so the small-firm opening is the long tail of mid-market and specialist customers those SIs do not serve.
- Remediation vendors all converge on "agent proposes, human merges a PR". The PR workflow is the trust boundary the market accepts.

### Gaps
- No primary pricing found for CrowdStrike Charlotte AI, SentinelOne Purple AI, Google SecOps Gemini, Palo Alto AgentiX, Torq, Intezer, Dropzone, Prophet, 7AI or Exaforce. These are quote-only, and searches surfaced no reliable per-alert or per-seat figures.
- SentinelOne Purple AI, Torq HyperSOC, and Intezer were not covered by any source retrieved in this pass.
- Zest Security: no results returned.
- No confirmed GA dates for CrowdStrike AgentWorks or Palo Alto AgentiX standalone.
- Vanta's and Drata's own AI-agent products (for example "Vanta AI Agent") were not confirmed from primary sources in this pass.

---

## Q2. What measurable results are reported? Independent data vs vendor claims

### Takeaway
Independent evidence shows LLM triage can be very accurate on labelled alert sets, but end-to-end investigation remains weak. Some architectures quietly dismiss real attacks while looking efficient. Vendor claims of 90%+ autonomous closure and sub-2-minute triage are not backed by independent studies. The strongest independent operational number is IBM's $1.9M breach-cost saving for organisations with extensive security AI and automation. That number is correlational and covers AI and automation in general, not agents specifically.

### Cited Findings

**Independent or academic**
- In an October 2026 arXiv study, a structured agent design (AIDA) reached F1 0.958 on the same alerts where other approaches scored 0.371–0.744. It cut false negatives from 40.4% to 3.1% while escalating 18.4% of alerts. A "self-consistency" approach escalated only 8.5% of alerts but **dismissed 54.2% of attack-related alerts**. The authors warn that low analyst load alone does not show good triage — [arXiv 2610.10608](https://arxiv.org/html/2610.10608) (from a search summary; full paper not read)
- In the SIABench 2026 benchmark, as summarised by a vendor, GPT-5 reached 98% triage accuracy on TII-SSRC-23 and Claude 4.5 Sonnet reached 96%. On full end-to-end incidents, the best model solved only 8 of 25 scenarios, which places these systems at junior-analyst-level work — [Simbian blog](https://simbian.ai/blog/ai-soc-agent-alert-triage-evidence) (vendor summary of a benchmark)
- A January 2026 "OpenSec" study, surfaced in search summaries, found that only 37.5–54.2% of agent containment actions were preceded by fetching trusted evidence about the target, with false-positive rates of 45–82.5%. Which page reported it was not confirmed; it appears alongside [Simbian](https://simbian.ai/blog/ai-soc-agent-alert-triage-evidence) and [arXiv agentic security survey](https://arxiv.org/pdf/2510.06445). **Treat as unverified.**
- SANS 2025 AI Survey: only 33% use AI to investigate incidents and 26% use it to respond. 66% say AI systems generate excessive false positives — [SANS 2025 AI Survey](https://www.sans.org/white-papers/sans-2025-ai-survey-measuring-ai-impact-security-three-years-later); summary via [ZeroFox](https://www.zerofox.com/blog/the-sans-2025-ai-survey-what-security-leaders-need-to-know/)
- In SANS 2025 SOC Survey coverage, about 40% of SOCs use AI/ML tools without making them a defined part of operations. AI/ML ranked at the bottom of technology satisfaction — [Torq summary of SANS 2025 SOC Survey](https://torq.io/blog/sans-2025-soc-survey/) (vendor summary)
- IBM Cost of a Data Breach 2025: organisations using AI and automation extensively across security operations saved an average of $1.9M per breach and shortened the breach lifecycle by 80 days. The global average breach cost is $4.44M — [TechRepublic on IBM 2025](https://www.techrepublic.com/article/news-ai-breach-risks-rise-as-governance-lags/); [VentureBeat](https://venturebeat.com/business/ibm-shadow-ai-breaches-cost-670k-more-97-of-firms-lack-controls) (secondary coverage of IBM's report)
- Gartner Hype Cycle for Security Operations 2025: AI SOC agents sat at the Innovation Trigger with 1–5% market penetration. Gartner advises baselining current operations, running vendor-neutral pilots, and evaluating AI built into the incumbent SIEM or XDR before buying standalone tools. Gartner also warns of "agent washing", saying only about 130 of thousands of "agentic" vendors are real — [Prophet summary](https://www.prophetsecurity.ai/blog/ai-soc-agents-in-gartner-hype-cycle-for-security-operations); [7AI summary](https://blog.7ai.com/7ai-recognized-as-a-sample-vendor-in-gartner-hype-cycle-for-security-operations-2025). Vectra's post conflicts on the stage ("adolescent to peak") — [Vectra](https://www.vectra.ai/blog/vectra-ai-on-the-gartner-hype-cycle-for-security-operations-2025)
- Gartner Hype Cycle 2026: AI SOC agents have moved to the **Peak of Inflated Expectations** — [Simbian](https://simbian.ai/blog/gartner-hype-cycle-security-operations-2026-ai-soc-agents); [Dropzone](https://www.dropzone.ai/blog/gartner-hype-cycle-security-operations-2026) (vendor summaries; the Gartner report was not read)
- Independent field test of Snyk Agent Fix on 412 SAST findings: effective accuracy ranged from 87% for missing-sanitizer fixes down to 65% for access-control fixes, versus Snyk's marketed 80% — [Safeguard field test](https://safeguard.sh/resources/blog/snyk-agent-fix-autofix-field-test-2026) (third party; its own commercial interest is unknown)

**Vendor claims (unverified)**
- CrowdStrike says its Detection Triage Agent is >98% accurate against MDR analyst decisions — [CrowdStrike](https://www.crowdstrike.com/en-us/press-releases/crowdstrike-unleashes-agentic-security-workforce-to-transform-security-operations)
- Google's SecOps Triage Agent reportedly handled 5M+ alerts and cut analysis from 30 minutes to 60 seconds (as reported by competitor Prophet) — [Prophet](https://www.prophetsecurity.ai/blog/top-5-ai-soc-analyst-platforms)
- UnderDefense, an MDR vendor, claims AI closes 95%+ of investigations with about 5% escalated, and that triage drops from about 45 minutes to under 2 minutes. It also cites "independent 2026 testing across 47 SOCs" showing AI false-positive rates of 68–72% but does not name the study — [UnderDefense](https://underdefense.com/blog/ai-soc-automation/); [UnderDefense 500 incidents](https://underdefense.com/blog/ai-soc-real-incidents/)
- GitHub reports SQLi fixes take 18 minutes versus 3.7 hours, about 3x faster overall — [TechTarget](https://www.techtarget.com/searchsecurity/news/366603045/GitHub-Copilot-Autofix-tackles-vulnerabilities-with-AI)
- Snyk claims an 84% reduction in MTTR — [Safeguard](https://safeguard.sh/resources/blog/snyk-agent-fix-autofix-field-test-2026)
- Dropzone has published a write-up of a Cloud Security Alliance study of 148 analysts' views on AI SOC agents. The CSA primary report was not retrieved — [Dropzone](https://www.dropzone.ai/blog/soc-analysts-ai-perception-148-study)

### Inferences
- Escalation rate on its own can mislead. Any service Ritru offers should report false-negative rate (missed true positives, measured by seeding known-bad alerts or by retrospective review) **alongside** auto-close rate and time saved. That kind of independent measurement is itself a sellable service: an "AI SOC agent evaluation and bake-off", which fits Gartner's advice to run vendor-neutral pilots.
- Accuracy on single alerts is high but end-to-end investigation is weak. Agents should therefore be scoped to bounded subtasks: enrichment, a triage verdict with evidence, and a draft write-up.

### Gaps
- No independent, peer-reviewed field study (as opposed to a benchmark) reports production MTTR, analyst-capacity gains, or false-positive reduction for a named commercial AI SOC product.
- The IBM primary report was not read directly. Figures come from secondary coverage.
- The SANS 2026 AI Survey reportedly says 37% trust AI, 39% use automated IR, and 63% cite detection or response shortcomings. Those figures come from Swimlane's summary and were not verified against SANS — [Swimlane](https://swimlane.com/blog/sans-ai-2026-soc-trust/)
- The CSA research note on AI SOC alert noise (Sept 2026) could not be fetched (DNS failure).

---

## Q3. Which tasks do buyers trust agents with today vs not?

### Takeaway
Trust is concentrated in read-only and advisory work: enrichment, triage verdicts on high-volume, low-ambiguity alerts (phishing especially), investigation summaries, and **proposed** fixes delivered as PRs. Automated containment and direct production changes remain a minority practice. Independent tests show agents often act without gathering evidence first, which justifies the caution.

### Cited Findings
- The most mature GA agent from a leading vendor is phishing and email alert triage. Microsoft's agent "classifies incoming alerts and resolves false positives, escalating only the malicious cases". Identity and cloud alert triage remain in preview — [Microsoft Defender docs](https://learn.microsoft.com/en-us/defender-xdr/security-alert-triage-agent)
- Only 26% of organisations use AI to respond to incidents and 33% to investigate them. Most organisations use automated detection, but few trust fully automated response — [SANS 2025 AI Survey](https://www.sans.org/white-papers/sans-2025-ai-survey-measuring-ai-impact-security-three-years-later); [ZeroFox summary](https://www.zerofox.com/blog/the-sans-2025-ai-survey-what-security-leaders-need-to-know/)
- In the OpenSec 2026 study (unverified), only 37.5–54.2% of containment actions were preceded by fetching trusted evidence — [Simbian](https://simbian.ai/blog/ai-soc-agent-alert-triage-evidence)
- Remediation tools universally end in a reviewed PR rather than a direct change. Examples: Wiz Green Agent ("submit a pull request for review"), Copilot Autofix in Azure DevOps ("creates pull requests that developers can review and merge"), and Mobb ("ready-to-merge PRs") — [Wiz](https://wiz.io/blog/securing-software-age-of-agentic-coding); [InfoQ](https://www.infoq.com/news/2026/06/azuredevops-copilot-autofix/); [Pixee](https://www.pixee.ai/blog/best-automated-remediation-tools-2026)
- Microsoft's Intune agents include Change Review and Vulnerability Remediation, both framed as assisting admins rather than acting autonomously — [aguidetocloud](https://www.aguidetocloud.com/licensing/security-copilot/)
- An MDR vendor's review of 500 real incidents documents where AI still "asks for a human" — [UnderDefense](https://underdefense.com/blog/ai-soc-real-incidents/) (vendor)

### Inferences
- A practical trust ladder for client engagements: (1) read-only enrichment and summaries; (2) triage verdict with an evidence trail, human approves the close; (3) agent drafts a PR or ticket, human merges; (4) pre-approved, reversible containment (isolate host, revoke session) with a human on call; (5) autonomous production change, which is not yet accepted. Most clients will buy rungs 1–3 in 2026–27.

### Gaps
- No quantitative buyer survey broke down trust by specific action type (for example host isolation vs account disable vs firewall change).

---

## Q4. What gaps remain (mid-market without a SOC, multi-tool, custom detections, data residency, cross-framework evidence, IaC remediation)?

### Takeaway
Agents from the leading platforms work best inside a single-vendor stack and for customers with a SOC that can tune them. The underserved areas are: mid-market firms with mixed tools and no SOC; agents in multi-vendor environments; custom detection engineering, still in preview at Google; EU and regulated customers who need DORA- and NIS2-native evidence and data residency; and the "last mile" of remediation into the client's own IaC and repos.

### Cited Findings
- Gartner advises evaluating AI built into the incumbent SIEM or XDR before buying a standalone tool, and puts AI SOC agent penetration at only 1–5% (2025) — [Prophet summary of Gartner](https://www.prophetsecurity.ai/blog/ai-soc-agents-in-gartner-hype-cycle-for-security-operations)
- About 40% of SOCs use AI/ML tools without defining their role in operations, and satisfaction is lowest for AI/ML — [Torq summary of SANS SOC Survey 2025](https://torq.io/blog/sans-2025-soc-survey/)
- Google's detection-engineering and threat-hunting agents are still in preview — [Prophet](https://www.prophetsecurity.ai/blog/top-5-ai-soc-analyst-platforms)
- Microsoft's identity and cloud alert triage is still in preview; only email triage is GA — [Microsoft Defender docs](https://learn.microsoft.com/en-us/defender-xdr/security-alert-triage-agent)
- Leading compliance platforms handle NIS2 and DORA by mapping them onto other frameworks rather than with native modules. EU-native tools are emerging to fill that gap — [Orbiq](https://www.orbiqhq.com/comparisons/vanta-alternatives)
- Remediation agents depend on context: ownership, attack path, which repo owns the resource. Wiz's agent explicitly gathers "ownership data and past fixes" before generating a plan — [Wiz](https://wiz.io/blog/securing-software-age-of-agentic-coding)
- Independent fix accuracy drops sharply for logic-heavy classes: 65% for access-control fixes versus 87% for sanitizer fixes — [Safeguard](https://safeguard.sh/resources/blog/snyk-agent-fix-autofix-field-test-2026)
- Vendors' agentic SOC releases at RSAC 2026 left a gap in behavioural baselines and telemetry for the agents themselves — [VentureBeat](https://venturebeat.com/security/rsac-2026-agentic-soc-agent-telemetry-security-gap)
- Microsoft agents' SCU consumption per run is unpublished, so cost is hard for buyers to predict — [SCU Calculator](https://www.scucalculator.com/agents)

### Inferences
- **Mid-market without a SOC:** E5 bundling gives these firms triage agents they lack the staff to configure, tune, or monitor. That creates demand for "agent-assisted managed triage" or "Copilot agent enablement" services.
- **Multi-tool:** MCP servers from Google SecOps and Wiz, plus AgentWorks, make cross-tool agent glue feasible for a small firm, for example correlating Wiz findings with Defender alerts and Jira ownership.
- **IaC remediation:** cloud posture tools find issues in the running cloud, but fixes must land in the Terraform, Bicep, or CDK modules that created the resource. Mapping a finding to its IaC module, generating the PR, and passing the client's CI checks requires knowledge of each client's setup. That makes it a good fit for a services firm.
- **Cross-framework evidence:** SOC 2 + ISO 27001 + DORA/NIS2 crosswalks, plus collecting evidence from systems the SaaS connectors don't cover (on-prem, custom apps), is classic consultancy work. Agents can draft the mapping and pull evidence, with a human reviewing.

### Gaps
- No source quantified the mid-market "no SOC" segment size, or MSSP adoption of AI SOC agents.
- Data-residency limits for Security Copilot, Gemini in SecOps, or AI SOC startups (for example EU-only processing) were not verified in this pass.
- No source was found on agent support for custom detections (Sigma/KQL/YARA-L authoring) beyond the Google preview.

---

## Q5. What security risks do AI agents themselves introduce, and is there a service market for securing clients' agents (AI red teaming, agent reviews, AI governance)?

### Takeaway
The risk taxonomy is now standardised (OWASP Top 10 for Agentic Applications, December 2025) and is backed by a steady stream of real MCP CVEs. IBM data shows most organisations breached through AI lacked basic access controls. EU AI Act high-risk deadlines have slipped to December 2027 and August 2028. That reduces regulatory urgency but leaves more time for paid readiness work. Agent security reviews, MCP and tool-permission audits, and AI governance (ISO 42001, NIST AI RMF) are credible service lines, though no market-size data was found.

### Cited Findings
- OWASP Top 10 for Agentic Applications 2026 was released in December 2025 (most sources say December 9) by the OWASP GenAI Security Project's Agentic Security Initiative. Its categories are: ASI01 Agent Goal Hijack, ASI02 Tool Misuse and Exploitation, ASI03 Identity and Privilege Abuse, ASI04 Agentic Supply Chain, ASI05 Unexpected Code Execution, ASI06 Memory and Context Poisoning, ASI07 Insecure Inter-Agent Communication, ASI08 Cascading Failures, ASI09 Human-Agent Trust Exploitation, ASI10 Rogue Agents. It is a companion to the OWASP LLM Top 10 — [Giskard](https://www.giskard.ai/knowledge/owasp-top-10-for-agentic-application-2026); [Teleport](https://goteleport.com/blog/owasp-top-10-agentic-applications/); [Astrix](https://astrix.security/learn/blog/the-owasp-agentic-top-10-just-dropped-heres-what-you-need-to-know/) (secondary; the OWASP primary page was not fetched)
- Anthropic's official Git MCP server had three prompt-injection-exploitable flaws affecting versions before December 8, 2025, in default installations — [Infosecurity Magazine](https://www.infosecurity-magazine.com/news/prompt-injection-bugs-anthropic/)
- CVE-2026-13341 (Kong Konnect MCP server): indirect prompt injection caused the server to make unintended API requests, a confused-deputy failure — [arXiv 2608.10281](https://arxiv.org/pdf/2608.10281) (from a search summary)
- One aggregator reports 30+ CVEs filed against MCP servers in a 60-day window in early 2026, about 43% of them command injection (secondary aggregation, unverified) — [Practical DevSecOps](https://www.practical-devsecops.com/mcp-security-statistics-2026-report/)
- CSA reports that major IDEs auto-execute project-defined MCP servers with developer-level OS privileges (vulnerabilities disclosed mid-2025 to June 2026). Tool-description poisoning cannot be distinguished at the protocol level — [CSA research note](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/); [arXiv 2607.05744 Unicode TAG concealment](https://arxiv.org/pdf/2607.05744)
- An OX Security advisory describes RCE vulnerabilities across the MCP supply chain — [OX Security](https://www.ox.security/blog/mcp-supply-chain-advisory-rce-vulnerabilities-across-the-ai-ecosystem/)
- IBM 2025: 13% of organisations reported breaches of their own AI models or apps, and 97% of those lacked proper AI access controls. One in five attributed a breach to shadow AI, which added about $670K to breach cost. 63% lacked AI governance policies or were still developing them. Only 37% have policies to manage or detect shadow AI — [VentureBeat](https://venturebeat.com/business/ibm-shadow-ai-breaches-cost-670k-more-97-of-firms-lack-controls); [TechRepublic](https://www.techrepublic.com/article/news-ai-breach-risks-rise-as-governance-lags/)
- EU AI Act Digital Omnibus: a provisional agreement on May 7, 2026 defers obligations for Annex III stand-alone high-risk systems to December 2, 2027, and for Annex I product-embedded systems to August 2, 2028. It takes effect only on formal adoption and publication in the Official Journal; publication was not confirmed. One Winston & Strawn note gives a conflicting Annex I date, likely a typo — [Gibson Dunn](https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines/); [Sidley, 2026-06-22](https://datamatters.sidley.com/2026/06/22/eu-lawmakers-reach-provisional-agreement-to-delay-key-eu-ai-act-obligations/); [Hogan Lovells](https://www.hoganlovells.com/en/publications/eu-legislators-agree-to-delay-for-highrisk-ai-rules)
- Leading vendors are productising agent governance. Microsoft Agent 365 ($15/user/month, GA May 2026 per a third-party guide) extends Entra, Purview, and Defender to AI agents — [aguidetocloud](https://www.aguidetocloud.com/licensing/security-copilot/)
- Security agents themselves need governing. Practitioners on Microsoft forums are discussing operational issues with Security Copilot agents in Defender XDR and Entra — [Microsoft Tech Community](https://techcommunity.microsoft.com/discussions/microsoftthreatprotection/operational-notes-on-microsoft-security-copilot-agents-in-defender-xdr-and-micro/4525826)

### Inferences
- Securing a client's agents is a natural adjacent service. It also shields Ritru's credibility: a firm that deploys agents for clients must show it secures them. Packages could include an OWASP ASI-mapped agent threat model, an MCP server inventory and permission review, token-scope least-privilege hardening, prompt-injection and tool-misuse red-team tests, and agent audit logging.
- The AI Act delay makes this a "readiness" sale rather than a deadline-driven panic sale. ISO 42001 and NIST AI RMF alignment can be packaged with the SOC 2 and ISO 27001 work clients already buy.

### Gaps
- No market-size or pricing data was found for AI red teaming or agent security review services.
- ISO 42001 certification uptake numbers and NIST AI RMF agent-specific profiles were not retrieved.
- The OWASP primary page, the ENISA guidance, and NIST agent-specific guidance (for example any 2026 AI agent hijacking or overlay work) were not fetched in this pass.

---

## Q6 (Objective). Which security agent services could Ritru Labs build, integrate, or operate without competing head-on with the leading vendors?

### Takeaway
Ritru should not build a competing AI SOC or remediation product. Vendors are either bundling these for free (E5) or are funded with $40M–$200M. Ritru should sell the integration, tuning, measurement, and governance layer around agents clients already own or can buy. It should focus on the mid-market, multi-tool, IaC, and EU-compliance seams that leading vendors and large SIs leave open.

### Cited Findings (supporting evidence for the opportunity map)
- Microsoft bundles triage agents into E5, which makes "a triage agent" a commodity in Microsoft-heavy accounts — [Microsoft Learn](https://learn.microsoft.com/en-us/copilot/security/security-copilot-inclusion)
- AI SOC startups are heavily funded: Exaforce about $200M total, 7AI about $166M — [SC World](https://www.scworld.com/brief/ai-cybersecurity-startup-exaforce-raises-125-million); [BuildMVPFast](https://www.buildmvpfast.com/blog/ai-cybersecurity-funding-boom-exaforce-market-map-2026)
- Vendors are opening platforms to custom agents and MCP: CrowdStrike AgentWorks, Google SecOps MCP (reported GA), Wiz MCP — [VentureBeat](https://venturebeat.com/security/rsac-2026-agentic-soc-agent-telemetry-security-gap); [Wiz MCP](https://www.wiz.io/es-es/blog/introducing-wiz-mcp)
- Gartner recommends vendor-neutral pilots and baselining before buying, and warns of agent washing — [Prophet summary](https://www.prophetsecurity.ai/blog/ai-soc-agents-in-gartner-hype-cycle-for-security-operations)
- Microsoft's per-agent SCU cost is unpublished, so buyers need cost visibility — [SCU Calculator](https://www.scucalculator.com/agents)
- Compliance platforms treat DORA and NIS2 as mapped frameworks, not native ones — [Orbiq](https://www.orbiqhq.com/comparisons/vanta-alternatives)

### Inferences (candidate service lines, ranked by fit for a small consultancy)
1. **AI SOC agent evaluation and bake-off (fixed-fee, 4–6 weeks).** Baseline the client's current alert volume and MTTR. Pilot 2–3 options (built-in Copilot, Gemini, or Charlotte agents versus one startup) on the client's real alerts, using seeded true positives to measure false negatives. Deliver a scorecard and a cost model (SCU/consumption). This is low conflict with vendors because Ritru recommends rather than competes. Basis: Gartner's advice and the arXiv finding that low escalation can hide missed attacks.
2. **"Built-in agent enablement" for Microsoft E5 and Google SecOps mid-market tenants.** Turn on and tune the triage agents the client already pays for: feedback loops, exclusions, SCU budget guardrails, KPIs. Optionally operate them on a monthly retainer as "agent-assisted managed triage" for firms without a SOC (rungs 1–3 of the trust ladder in Q3).
3. **Cloud posture to IaC remediation PR pipeline.** Glue Wiz, Defender for Cloud, or Security Hub findings to the client's Terraform or Bicep repos. Use a coding agent (Copilot coding agent, Claude, and so on) behind the client's CI policy checks (tfsec/checkov/OPA). Every fix ships as a reviewed PR with an owner mapped. Price as a build plus a per-month operate fee. This works alongside Wiz Green Agent rather than against it, because it handles client-specific module structure and the CI process around the fix.
4. **Cross-framework compliance evidence agent and audit readiness**, sitting on top of Vanta, Drata, or Sprinto rather than replacing them. Cover the SOC 2 + ISO 27001 + DORA/NIS2 (+ ISO 42001) crosswalk, evidence collection from systems without connectors, policy-to-control gap drafts, and pre-audit review by a human. This fits EU and regulated clients poorly served by US-first platforms.
5. **Agent security review and AI governance.** Covers OWASP ASI01–10 threat modelling, MCP server inventory and permission audit, token least-privilege, prompt-injection and tool-misuse red-team tests, and agent logging and kill-switch design. Also offer ISO 42001 and NIST AI RMF readiness and EU AI Act scoping. Pitch it to every client that buys services 1–4 ("we secure the agents we deploy").
6. **Custom-detection and multi-tool agent glue (selective).** Build custom agents on AgentWorks or AgentiX, or MCP-based cross-tool enrichment (identity + EDR + cloud + ticketing). Take this only where clients have mixed stacks. It overlaps with large SIs, so position Ritru on speed and mid-market price.
7. **Identity and access review agent (adjacent).** Draft quarterly access-review packets from IdP and SaaS data for SOC 2 and ISO evidence, with a human approving revocations. This is low-risk and read-only, and it feeds into service 4.
8. **Avoid:** building a standalone AI SOC triage product, autonomous containment or production-change services, and generic phishing triage in E5 tenants, where the GA agent is effectively free.

### Gaps
- No pricing benchmarks were found for comparable consultancy or MSSP agent services (retainer or per-alert rates), so Ritru's pricing needs separate validation.
- Partner-program terms (Microsoft Security Copilot partner, CrowdStrike AgentWorks partner, Wiz integration partner, Vanta or Drata auditor and partner networks) were not researched. They would determine how Ritru could resell or co-sell.
- No direct demand-side data (buyer surveys) specific to mid-market interest in outsourced agent operation was found.
