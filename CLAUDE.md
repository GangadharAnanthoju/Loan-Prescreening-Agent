# CLAUDE.md — Loan Pre-Screening Agent

This file gives Claude instant context about this project when opened in a new chat.

---

## What This Project Is

A **Loan Application Pre-Screening Portal** built on **Microsoft Foundry** (formerly Azure AI Foundry). It takes a loan application and income document, runs them through 4 chained AI agents, scores the risk, and lets an underwriter approve or decline via a web portal.

**Business value:** Replaces manual loan pre-screening — shows how multi-agent AI workflows automate real financial decisions.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent Orchestration | Microsoft Foundry Workflow (YAML) |
| Agent Runtime | Microsoft Foundry Agent Service |
| Backend | Python 3 / Flask |
| Real-time Updates | Server-Sent Events (SSE) |
| Policy Rules | `loan_policies.json` (no database) |
| Frontend | Vanilla HTML/CSS/JS (glassmorphism dark UI) |
| Auth | `azure-identity` DefaultAzureCredential |
| SDK | `azure-ai-projects` + `azure.ai.projects.models` |

---

## Project Structure

```
Loan-Prescreening-Agent/
├── CLAUDE.md                 ← you are here
├── workflow_loan.yaml        ← Microsoft Foundry workflow (the agent chain)
├── agents_loan.md            ← System prompts for all 4 agents (copy-paste into Foundry)
├── loan_portal.py            ← Flask backend (upload, SSE stream, approve/decline)
├── loan_policies.json        ← Lending policy rules (min credit score, max DTI, etc.)
├── portal/
│   └── loan.html             ← Web UI (dark teal glassmorphism, 6-step progress tracker)
├── sample_docs/
│   ├── LOAN_APP_001_GOOD.txt     Scenario 1: Low risk (credit 740, DTI 9.9%) → Approve
│   ├── INCOME_001_GOOD.txt
│   ├── LOAN_APP_002_HIGH_DTI.txt Scenario 2: Medium risk (income mismatch, DTI 39.7%) → Refer
│   ├── INCOME_002_HIGH_DTI.txt
│   ├── LOAN_APP_003_DECLINE.txt  Scenario 3: High risk (credit 595, DTI 44%, LTV 97.5%) → Decline
│   └── INCOME_003_DECLINE.txt
├── env.example               ← Copy to .env — only AZURE_AIPROJECT_ENDPOINT needed
└── README.md                 ← Full setup guide
```

---

## How to Run

```bash
pip install flask azure-identity azure-ai-projects python-dotenv
cp env.example .env          # fill in AZURE_AIPROJECT_ENDPOINT
python loan_portal.py        # opens on http://localhost:5001
```

---

## The 4 Agents (Microsoft Foundry)

All agents must be created manually in **Microsoft Foundry Portal → Agents → New Agent**.
Use the exact names below. System prompts are in `agents_loan.md`.

| # | Agent Name | MCP Tool | Job |
|---|---|---|---|
| 1 | `loan-doc-extractor` | None | Extracts all fields from application + income docs |
| 2 | `loan-financial-analyzer` | None | Calculates DTI, LTV, affordability, income consistency |
| 3 | `loan-risk-scorer` | None | Checks policy rules, scores risk 0–100, recommends decision |
| 4 | `loan-notifier` | `outlookworkflowemail` | Sends approval or decline email |

---

## Critical Foundry Agent Chain Pattern

This is the most important technical detail in the project:

```yaml
# FIRST agent — reads what the user submitted
input:
  messages: =System.LastMessage

# ALL OTHER agents — each reads the previous agent's output
input:
  messages: =Local.LatestMessage
```

Every agent in the chain receives all prior agent outputs automatically because each agent's response becomes `Local.LatestMessage`. This is how context accumulates without a shared database.

---

## How Policy Rules Work (No Database)

`loan_portal.py` reads `loan_policies.json` at startup and appends it to the document text before sending to the workflow:

```python
parts.append(policy_context_text())   # adds the JSON as readable text
doc_text = "\n\n---\n\n".join(parts)  # combined with application + income text
```

The `loan-risk-scorer` agent reads the policy directly from the conversation — no SQL, no Cosmos DB needed.

**To change a policy limit:** edit `loan_policies.json` — takes effect on the next upload.

---

## How the Backend Streams Progress (SSE)

The Flask backend uses Server-Sent Events to push live updates to the browser:

1. `POST /api/upload` — saves files, appends policy JSON, starts background thread, returns `session_id`
2. `GET /api/progress/<session_id>` — SSE stream; browser connects here and receives step events
3. `POST /api/approve/<session_id>?decision=APPROVE|DECLINE` — submits underwriter decision
4. `GET /api/status/<session_id>` — polled after decision until `complete`

The background thread calls Microsoft Foundry in two parts:
- First `responses.create(input="Start")` — triggers the workflow
- Then `responses.create(input=doc_text)` — sends documents, runs all 4 agents, pauses at approval gate
- After underwriter decides: `responses.create(input=decision)` — runs notifier agent and closes

---

## Frontend (portal/loan.html)

- **Design:** Dark blue/teal glassmorphism (same pattern as the invoice portal in the parent project)
- **Two upload boxes:** Loan Application + Income Verification
- **6-step progress tracker:** Upload → Extraction → Analysis → Risk → Decision → Notification
- **Step states:** `active` (blue pulse), `completed` (green), `waiting` (amber), `error` (red)
- **Approval buttons:** "Approve to Underwriter" / "Decline Application" — appear after risk assessment
- **Inline output:** Each completed agent step shows its output with Show More/Less toggle
- **Workflow summary:** Built at the end showing what every agent did

---

## Test Scenarios

| Scenario | Applicant | Key Issues | Expected Risk |
|---|---|---|---|
| 1 (GOOD) | Michael Johnson | Credit 740, DTI 9.9%, LTV 80%, 5yr employment | LOW → Approve |
| 2 (HIGH DTI) | Sarah Chen | Self-employed, income mismatch 33%, DTI 39.7% | MEDIUM → Refer |
| 3 (DECLINE) | Robert Williams | Credit 595 (below min), DTI 44%, LTV 97.5%, 8mo employment | HIGH → Decline |

---

## Future Extension: Scanned PDFs (Azure Document Intelligence)

The hook is already documented in `loan_portal.py` (search: `FUTURE EXTENSION`).

Install: `pip install azure-ai-documentintelligence`
Add to `.env`: `DOCUMENT_INTELLIGENCE_ENDPOINT` and `DOCUMENT_INTELLIGENCE_KEY`

The extension converts scanned PDFs and images to text before the workflow runs — no other changes needed.

---

## Owner

**Gangadhar Ananthoju** — Portfolio project demonstrating Microsoft Foundry multi-agent workflows.
Part of a career showcase series. Related project: `../Microsoft-Foundry-Multi-Agent-Workflow` (invoice processing).
