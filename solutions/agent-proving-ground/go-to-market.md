# Ritru Proving Ground: how we sell it

- Status: Draft v0.1 for review
- Date: 2026-10-11

## The name

- **Service:** Ritru Proving Ground
- **Framework and score:** Ritru PROVE Score (see [PROVE-framework.md](framework/PROVE-framework.md))
- **One-line pitch:** *"Your vendor says 95%. We test it on your own work before it touches production."*

## The problem, with numbers

- Vendors advertise accuracy of 95% or more. Independent tests find far less:
  - On incident root-cause benchmarks, agents find the exact cause in only about 10–25% of cases, and invent a root cause in 7–40% of reports ([OpenRCA, ICLR 2025](https://iclr.cc/virtual/2025/poster/32093); [ORCA-bench, 2026 preprint](https://www.alphaxiv.org/abs/2607.28545.md)).
  - In one October 2026 study, a security triage agent looked efficient by escalating only 8.5% of alerts. It did that by **dismissing 54.2% of the alerts that were real attacks** ([arXiv 2610.10608](https://arxiv.org/html/2610.10608)).
- Gartner, as quoted in a vendor-sponsored article: by 2028, 70% of large SOCs will pilot AI agents, but **only 15% will see measurable improvement without structured evaluation** ([Help Net Security](https://www.helpnetsecurity.com/2026/09/09/prophet-security-evaluating-ai-soc-agents/)).
- Even big vendor comparisons say they did no hands-on testing. Palo Alto Networks' AI SOC tools comparison states that its figures are vendor-reported or customer-reported ([Palo Alto Networks](https://www.paloaltonetworks.com/cyberpedia/ai-soc-tools-comparison)).
- Gartner expects more than 40% of agentic AI projects to be cancelled by the end of 2027, partly because of weak risk controls ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027)).

**In plain words:** companies are spending money on agents they cannot check.

## Who buys and when

| Buyer | Their moment of need | What they ask |
|---|---|---|
| CISO / Head of SOC | Choosing an AI SOC agent, or switching on auto-close | "Which one misses fewer real attacks?" |
| VP Engineering / Platform | Rolling out coding agents (Copilot, Claude Code, Codex, Cursor, Devin) | "Which one writes code our reviewers accept?" |
| Head of SRE | Trialling an AI SRE agent | "Does its root cause match what really happened?" |
| Risk, compliance, internal audit | Board, regulator or auditor asks how AI agents are controlled | "Show me evidence it is safe and supervised." |
| Team that built its own agent | Before go-live, or before giving it write access | "Is it ready, and how much freedom can it have?" |

**The trigger moments to sell into:** before signing a vendor contract, before go-live, before increasing an agent's permissions, and after a vendor model update.

## Offers and starting prices

These prices are starting points to test with design partners, not market facts.

| Offer | What the client gets | Price (to validate) |
|---|---|---|
| **Agent Health Check** (lead offer) | 1–2 days. 20 scenarios against one agent. A short scorecard. | Free for design partners; $3K–$5K after that |
| **Agent Bake-off** | 3–4 weeks. 2–3 agents tested head to head on the client's own cases. Full PROVE report and a recommendation. | $20K–$40K |
| **Go-live Readiness Score** | 2–3 weeks. One agent, full 14 layers, autonomy level, and a list of fixes. | $15K–$30K per agent |
| **Continuous PROVE** | Monthly retest on the hidden suite, drift alerts, and a quarterly evidence pack for auditors. | $2K–$6K per agent per month |

## Competition and our position

| Who | What they do | How we differ |
|---|---|---|
| **AIUC-1** (with Schellman as auditor) | A certification for AI agents covering security, safety and reliability controls. Certificates last a year, with quarterly retests. Holders include UiPath and Sierra. AIUC raised $40M in September 2026. ([IT Brief](https://itbrief.co.uk/story/uipath-secures-landmark-aiuc-1-certification-for-ai-agents); [Schellman](https://www.schellman.com/blog/news/schellman-becomes-the-first-accredited-auditor-for-aiuc-1); [SiliconANGLE](https://siliconangle.com/2026/09/15/ai-agent-certification-startup-aiuc-raises-40m-to-begin-auditing-frontier-models/)) | Mainly sought by agent **vendors**, to prove their product is safe in general. We work for the **buyer** and test whether it does *their* job in *their* environment, head to head with rivals. We complement it: our Evidence pillar maps to AIUC-1. |
| **Eval tools** (Braintrust, Langfuse, Arize, LangSmith) | Software that lets teams build their own tests | They sell the tool. We bring the domain test cases, the red team and a neutral verdict. We may use these tools inside our harness. |
| **Vendor self-tests and benchmarks** | Vendors publish their own scores | Not neutral. Public benchmarks have been gamed. Our test cases stay secret. |
| **Consultancy rubrics** (for example, UnderDefense's 12-point AI SOC scoring) | Paper scoring of features and architecture ([UnderDefense](https://underdefense.com/ai-soc-agents-%C2%B7-2026/)) | We test hands-on, with planted cases and measured miss rates. |
| **Data-labelling firms** (for example Toloka) | Custom evaluation datasets and human graders | General purpose. We specialise in cloud, DevOps and security operations work. |

**Honest note:** AIUC-1 is the closest player. If it adds hands-on, buyer-side performance testing, the gap narrows. Our defence is to go deep where AIUC-1 is broad: cloud, DevOps and security agents, tested on the client's own work, every month.

## Where we start: one test pack first

A deep test pack beats a shallow framework for every kind of agent. Recommended order:

1. **Cloud and DevOps coding agents.** This is the biggest spend; Menlo Ventures put coding at about $4B of 2025 enterprise AI spend. It is closest to Ritru's skills, and results are easy to prove because tests pass or fail and reviewers merge or reject.
2. **AI SRE / incident agents.** These replay the client's past incidents with known root causes.
3. **AI SOC agents.** This has the biggest claim-versus-reality gap but needs realistic attack data. Partner with a security specialist if needed.

## How we prove it before the first sale

1. **Publish the PROVE method openly** as a Ritru blueprint, so anyone can inspect it. The test cases stay private.
2. **Run a public demo:** score 2–3 open-source or freely available coding agents on a public scenario set that we build, and publish the method and results. Use only agents whose licences allow benchmarking.
3. **Write Field Notes and LinkedIn posts** on what the tests found, especially the failure modes: missed cases, false "tests passed" claims, prompt injection.
4. **Two design partners** get the Bake-off at a discount in return for an anonymised case study.

## Objections and answers

| They say | We answer |
|---|---|
| "We'll just test it ourselves." | Most teams test the demo cases the vendor suggests. We bring planted must-catch cases, a red team and secret tests. Gartner says structured evaluation is what separates the 15% who gain from the rest. |
| "The vendor already gave us benchmark numbers." | Those are self-reported on public benchmarks the vendor can tune for. We test on your work, with cases the vendor has never seen. |
| "Is this safe for our environment?" | We test in a sandbox or staging copy, never production, and your data stays with you. |
| "What if the score is wrong?" | Every number shows its sample size and range. A human expert checks at least 20% of the grading. |
| "We already have AIUC-1 or ISO 42001." | Good, our evidence maps to both. Those say the agent is controlled. We show whether it does your job well, and whether it still does next month. |

## 90-day plan

| Days | Build | Sell |
|---|---|---|
| 1–30 | Finalise the PROVE framework. Build harness v0.1: scenario runner, observer, deterministic graders, scorer. Write the first 50 coding/IaC scenarios. | 5 conversations with platform and security leaders: "Would you pay to test an agent before trusting it?" |
| 31–60 | Add the red-team agent, the AI judge with human checks, and the report generator. Run the public demo on 2–3 open agents. | Publish the method and demo results. Sign 2 design partners for Health Checks. |
| 61–90 | Harden the harness for a client sandbox. Add the SRE test pack. | Convert at least 1 Health Check into a paid Bake-off or Readiness Score. Propose Continuous PROVE. |

**Day-90 go / no-go:** continue if at least one client pays, and at least 2 of 5 leaders say they would buy. If not, narrow to one niche, for example coding-agent bake-offs only, or SOC agents for banks only.

## Risks

- **Vendor licences:** some licences restrict publishing benchmarks. Check each vendor, and keep client reports private.
- **Liability:** a score is a measurement on a date and a version, not a guarantee. Contracts must say so and cap liability at fees.
- **Ground truth effort:** good test cases need domain experts. Budget for this; it is our moat and our main cost.
- **Low competition can mean unproven demand.** The five buyer conversations in days 1–30 are the real test before building more.
