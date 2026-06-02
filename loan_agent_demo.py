"""
loan_agent_demo.py
==================
Standalone demo of the Microsoft Foundry 4-agent loan pre-screening workflow.

Shows the HITL (Human-in-the-Loop) pattern in its simplest form:
  1. Load documents from disk
  2. Run 3 agents (extract → analyze → score)  ← Phase 1
  3. PAUSE — human reads the output and types APPROVE or DECLINE
  4. Run 1 agent (notifier)                     ← Phase 2

No Flask, no web server, no threads. Just pure Foundry SDK calls.

Usage:
  python loan_agent_demo.py
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

ENDPOINT      = os.environ.get("AZURE_MS_FOUNDRY_ENDPOINT", "")
WORKFLOW_NAME = "Loan-Prescreening-Workflow"

# Files to load — change these paths to try different scenarios
LOAN_APP_FILE  = Path("sample_docs/LOAN_APP_001_GOOD.txt")
INCOME_FILE    = Path("sample_docs/INCOME_001_GOOD.txt")
POLICY_FILE    = Path("loan_policies.json")


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_documents():
    """Read both documents and the policy JSON, combine into one string."""
    app_text    = LOAN_APP_FILE.read_text(encoding="utf-8")
    income_text = INCOME_FILE.read_text(encoding="utf-8")

    # Load policy rules (no database — just a JSON file)
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    policy_text = f"\n## LENDING POLICIES\n{json.dumps(policy, indent=2)}"

    # Combine everything — the risk-scorer agent reads the policy from here
    return "\n\n---\n\n".join([app_text, income_text, policy_text])


def collect_stream_text(stream):
    """Pull all text chunks out of a Foundry streaming response."""
    chunks = []
    for event in stream:
        if event.type == "response.output_text.delta":
            chunks.append(getattr(event, "delta", "") or "")
        elif event.type == "response.output_text.done":
            chunks.append(getattr(event, "text", "") or "")
        elif event.type == "response.failed":
            # Surface the error so we can see what went wrong
            err = getattr(getattr(event, "response", None), "error", event)
            raise RuntimeError(f"Workflow failed: {err}")
    return "".join(chunks)


# ── Phase 1: Run agents 1–3, pause for human decision ────────────────────────

def run_until_approval(oc, conversation_id, doc_text):
    """
    Send documents to the workflow and let agents 1-3 run.
    The workflow pauses at the UNDERWRITER DECISION REQUIRED question.
    Returns the full agent output text.
    """
    print("\n[PHASE 1] Sending documents to workflow...")

    # Step 1: Send "Start" to trigger the workflow and get past the first
    #         Question node (the workflow asks for documents here)
    stream1 = oc.responses.create(
        conversation=conversation_id,
        extra_body={"agent_reference": {"name": WORKFLOW_NAME, "type": "agent_reference"}},
        input="Start",
        stream=True,
    )
    collect_stream_text(stream1)   # consume the welcome message, discard it
    print("[PHASE 1] Workflow started — sending documents...")

    # Step 2: Send the actual documents as the answer to the Question node.
    #         This triggers the 3 agents to run in sequence:
    #           loan-doc-extractor → loan-financial-analyzer → loan-risk-scorer
    #         The workflow then pauses at the UNDERWRITER DECISION REQUIRED question.
    stream2 = oc.responses.create(
        conversation=conversation_id,
        extra_body={"agent_reference": {"name": WORKFLOW_NAME, "type": "agent_reference"}},
        input=doc_text,
        stream=True,
    )
    output = collect_stream_text(stream2)

    print("[PHASE 1] All 3 agents completed.\n")
    return output


# ── Phase 2: Send decision, run notifier agent ────────────────────────────────

def run_after_decision(oc, conversation_id, decision):
    """
    Send the underwriter's decision (APPROVE or DECLINE) to the workflow.
    This resumes the paused workflow and triggers the loan-notifier agent.
    Returns the notifier agent's output (the formatted email).
    """
    print(f"\n[PHASE 2] Submitting decision: {decision}...")

    # The workflow is waiting at its Question node for APPROVE or DECLINE.
    # Sending the decision here resumes execution and runs loan-notifier.
    stream = oc.responses.create(
        conversation=conversation_id,
        extra_body={"agent_reference": {"name": WORKFLOW_NAME, "type": "agent_reference"}},
        input=decision,
        stream=True,
    )
    output = collect_stream_text(stream)

    print("[PHASE 2] Notifier agent completed.\n")
    return output


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Loan Pre-Screening Agent — Console Demo")
    print("  Microsoft Foundry Multi-Agent Workflow + HITL")
    print("=" * 60)

    # ── Load files from disk
    print(f"\nLoading documents:")
    print(f"  Application : {LOAN_APP_FILE}")
    print(f"  Income      : {INCOME_FILE}")
    doc_text = load_documents()

    # ── Connect to Microsoft Foundry
    print(f"\nConnecting to Foundry: {ENDPOINT[:60]}...")
    credential = DefaultAzureCredential()
    client     = AIProjectClient(endpoint=ENDPOINT, credential=credential)

    with client:
        oc = client.get_openai_client()

        # Create a fresh conversation — this is like a session ID
        conversation = oc.conversations.create()
        print(f"Conversation ID: {conversation.id}\n")

        # ── PHASE 1: Agents 1–3 run ──────────────────────────────────────────
        agent_output = run_until_approval(oc, conversation.id, doc_text)

        # Print what the agents produced so the human can read it
        print("-" * 60)
        print("AGENT OUTPUT (Extraction → Analysis → Risk Score):")
        print("-" * 60)
        print(agent_output)
        print("-" * 60)

        # ── HITL: Human makes the decision ───────────────────────────────────
        print("\n🔐 UNDERWRITER DECISION REQUIRED")
        print("   Review the risk assessment above.")

        while True:
            decision = input("\n   Type APPROVE or DECLINE: ").strip().upper()
            if decision in ("APPROVE", "DECLINE"):
                break
            print("   ⚠️  Please type exactly APPROVE or DECLINE.")

        # ── PHASE 2: Notifier agent runs ──────────────────────────────────────
        notification = run_after_decision(oc, conversation.id, decision)

        # Print the notification email the agent prepared
        print("-" * 60)
        print("NOTIFICATION OUTPUT (loan-notifier agent):")
        print("-" * 60)
        print(notification)
        print("-" * 60)

        print("\n✅ Workflow complete.")


if __name__ == "__main__":
    main()
