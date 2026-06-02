"""
Loan Pre-Screening Portal — Backend server.

Serves portal/loan.html, handles document upload, runs the Microsoft Foundry
workflow in the background, streams progress via SSE (Server-Sent Events),
and processes the underwriter's Approve/Decline decision.

Endpoints:
  GET  /                              Loan portal UI (loan.html)
  POST /api/upload                    Upload application + income docs; returns session_id
  GET  /api/progress/<session_id>     SSE stream of workflow step updates
  POST /api/approve/<session_id>      Submit APPROVE or DECLINE decision
  GET  /api/status/<session_id>       Poll for final result after decision

Run: python loan_portal.py
Then open: http://localhost:5001

------------------------------------------------------------------
HOW POLICY DATA WORKS (no database needed):
  1. loan_policies.json sits in the project folder
  2. This backend reads it at startup
  3. When documents are uploaded, the JSON policy is appended to the
     document text before being sent to the Foundry workflow
  4. The loan-risk-scorer agent reads the policy from the conversation
------------------------------------------------------------------

FUTURE EXTENSION — Azure Document Intelligence (for scanned PDFs/images):
  To support scanned applications, install the SDK:
    pip install azure-ai-documentintelligence

  Then replace the plain text read with:
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    doc_client = DocumentIntelligenceClient(
        endpoint=os.environ["DOCUMENT_INTELLIGENCE_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["DOCUMENT_INTELLIGENCE_KEY"])
    )
    with open(file_path, "rb") as f:
        poller = doc_client.begin_analyze_document("prebuilt-read", f)
        result = poller.result()
        text = " ".join([line.content for page in result.pages for line in page.lines])

  This extracts text from any scanned PDF or image file automatically.
------------------------------------------------------------------
"""

import os
import sys
import json
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path

def _utc_ts():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

import re

# -------------------------------------------------------------------------
# Load lending policies from JSON (no database needed)
# -------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
POLICY_FILE = BASE_DIR / "loan_policies.json"

def load_policies():
    if POLICY_FILE.exists():
        return json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    return {}

LOAN_POLICIES = load_policies()

def policy_context_text():
    """Format loan_policies.json as readable text to inject into the workflow conversation."""
    return f"""
## LENDING POLICIES (from loan_policies.json)
These are the policy rules the risk scorer agent must use:

{json.dumps(LOAN_POLICIES, indent=2)}

Use the above policies to check the applicant's loan type and validate each rule.
"""

# -------------------------------------------------------------------------
# Extract a short meaningful summary from each agent's output
# (used for the progress panel in the UI)
# -------------------------------------------------------------------------
def _detect_step_id(text):
    """Detect special node types. Agent order is handled by position counter."""
    t = text.upper()
    if 'DEMO MODE' in t or 'EMAIL NOTIFICATION PREPARED' in t:
        return 'invoke-approval-email'
    if 'UNDERWRITER DECISION REQUIRED' in t:
        return 'approval-question'
    return None  # let position-based counter handle the 4 agents


def _extract_step_summary(action_id, text):
    text = text.strip()
    details = {}
    summary = ""

    # Detect real step from content (new Foundry portal uses node IDs, not named action IDs)
    detected = _detect_step_id(text)
    resolved = detected or action_id

    if resolved == 'invoke-extractor':
        name_match  = re.search(r'(?:Applicant Name|Applicant)[:\s]+([A-Za-z ]+)', text, re.I)
        amt_match   = re.search(r'(?:Loan Amount|Amount Requested)[:\s]*\$?([\d,]+)', text, re.I)
        score_match = re.search(r'Credit Score[:\s]*([\d]+)', text, re.I)
        if name_match:
            details["applicant"] = name_match.group(1).strip()[:40]
        if amt_match:
            details["loan_amount"] = amt_match.group(1).strip()
        if score_match:
            details["credit_score"] = score_match.group(1).strip()
        parts = []
        if details.get("applicant"):  parts.append(details["applicant"])
        if details.get("loan_amount"): parts.append(f"${details['loan_amount']}")
        if details.get("credit_score"): parts.append(f"Credit {details['credit_score']}")
        summary = "Extracted: " + ", ".join(parts) if parts else "Application data extracted"

    elif resolved == 'invoke-analyzer':
        dti_match = re.search(r'DTI[:\s]*([\d.]+)%', text, re.I)
        ltv_match = re.search(r'LTV[:\s]*([\d.]+)%', text, re.I)
        if dti_match: details["dti"] = dti_match.group(1) + "%"
        if ltv_match: details["ltv"] = ltv_match.group(1) + "%"
        parts = []
        if details.get("dti"): parts.append(f"DTI {details['dti']}")
        if details.get("ltv"): parts.append(f"LTV {details['ltv']}")
        summary = "Financial analysis: " + ", ".join(parts) if parts else "Financial ratios calculated"

    elif resolved == 'invoke-risk-scorer':
        score_match = re.search(r'(?:TOTAL RISK SCORE|Risk Score)[:\s]*([\d]+)', text, re.I)
        level_match = re.search(r'Risk Level[:\s]*(LOW|MEDIUM|HIGH)', text, re.I)
        rec_match   = re.search(r'Recommendation[:\s]*(APPROVE|REFER|DECLINE)', text, re.I)
        if score_match: details["risk_score"] = score_match.group(1)
        if level_match: details["risk_level"] = level_match.group(1)
        if rec_match:   details["recommendation"] = rec_match.group(1)
        parts = []
        if details.get("risk_level"):    parts.append(f"{details['risk_level']} RISK")
        if details.get("risk_score"):    parts.append(f"Score {details['risk_score']}/100")
        if details.get("recommendation"): parts.append(f"→ {details['recommendation']}")
        summary = "Risk assessment: " + ", ".join(parts) if parts else "Risk scored"

    elif resolved in ("invoke-approval-email", "invoke-rejection-email"):
        summary = "Notification email prepared"

    else:
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        summary = lines[0][:150] if lines else f"Completed {action_id}"

    if text:
        details["raw_output"] = text[:2000] if len(text) > 2000 else text

    return {"summary": summary, "details": details, "detected_id": detected}


# -------------------------------------------------------------------------
# Fix Windows console encoding
# -------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from flask import Flask, request, Response, send_from_directory
except ImportError:
    print("Install Flask:  pip install flask")
    sys.exit(1)

try:
    from azure.identity import DefaultAzureCredential
    from azure.ai.projects import AIProjectClient
except ImportError:
    print("Install Azure SDK:  pip install azure-identity azure-ai-projects")
    sys.exit(1)

# Event type strings (azure-ai-projects 2.x uses openai string literals, not enum)
EVT_TEXT_DELTA    = "response.output_text.delta"
EVT_TEXT_DONE     = "response.output_text.done"
EVT_ITEM_ADDED    = "response.output_item.added"
EVT_ITEM_DONE     = "response.output_item.done"
EVT_ERROR         = "error"

# -------------------------------------------------------------------------
# Configuration — set AZURE_AIPROJECT_ENDPOINT in your .env file
# -------------------------------------------------------------------------
PORTAL_DIR   = BASE_DIR / "portal"
UPLOADS_DIR  = BASE_DIR / "uploads"
WORKFLOW_NAME = "Loan-Prescreening-Workflow"   # Must match name in Foundry Portal

ENDPOINT = os.environ.get("AZURE_MS_FOUNDRY_ENDPOINT", "YOUR_FOUNDRY_ENDPOINT_HERE")

UPLOADS_DIR.mkdir(exist_ok=True)

# Load .env if present
env_path = BASE_DIR / ".env"
if env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        if os.environ.get("AZURE_MS_FOUNDRY_ENDPOINT"):
            ENDPOINT = os.environ["AZURE_MS_FOUNDRY_ENDPOINT"]
        elif os.environ.get("AZURE_AIPROJECT_ENDPOINT"):
            ENDPOINT = os.environ["AZURE_AIPROJECT_ENDPOINT"]
    except ImportError:
        pass

# -------------------------------------------------------------------------
# Document Intelligence — Option B (loan application PDF → custom model)
# Income proof stays as plain text → loan-doc-extractor agent handles it
# -------------------------------------------------------------------------
def extract_text_from_file(file_path, is_loan_application=False):
    """
    Extract text from an uploaded file.
    - Loan application PDF  → Document Intelligence custom model (structured fields)
    - Income proof (any format) → plain text read (agent handles any format)
    """
    path = Path(file_path)

    # Read DI config at call time (after load_dotenv has run)
    DI_ENDPOINT = os.environ.get("DOCUMENT_INTELLIGENCE_ENDPOINT", "")
    DI_KEY      = os.environ.get("DOCUMENT_INTELLIGENCE_KEY", "")

    print(f"[DI] Processing: {path.name} (is_app={is_loan_application})")

    # Loan application PDF → custom DI model extracts structured fields
    if is_loan_application and path.suffix.lower() == ".pdf" and DI_ENDPOINT and DI_KEY:
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential

            DI_MODEL = os.environ.get("DOCUMENT_INTELLIGENCE_MODEL_ID", "prebuilt-read")
            print(f"[DI] Running custom model '{DI_MODEL}' on {path.name}")

            client = DocumentIntelligenceClient(
                endpoint=DI_ENDPOINT,
                credential=AzureKeyCredential(DI_KEY)
            )
            with open(path, "rb") as f:
                poller = client.begin_analyze_document(DI_MODEL, f)
                result = poller.result()

            if not result.documents:
                print("[DI] WARNING: model returned no documents — check model ID")
            else:
                print(f"[DI] Extracted {len(result.documents[0].fields)} fields (confidence={result.documents[0].confidence:.2f})")

            fields = result.documents[0].fields if result.documents else {}

            # Use .content (raw OCR text) — works for all field types
            def get_val(key):
                f = fields.get(key)
                if not f or not f.content:
                    return None
                return str(f.content).strip()

            # Map DI field names → labels the loan-doc-extractor agent expects
            FIELD_MAP = {
                "loan_amount":        "Loan Amount Requested ($)",
                "loan_purpose":       "Loan Purpose",
                "loan_type":          "Loan Type",
                "loan_term":          "Loan Term (years)",
                "property_value":     "Property Value ($)",
                "credit_score":       "Credit Score (self-reported by applicant)",
                "monthly_income":     "Monthly Income (self-reported, $)",
                "total_monthly_debt": "Monthly Debt Payments (self-reported, $)",
                "employment_type":    "Employment Type",
                "years_at_employer":  "Years at Current Employer",
                "employer_name":      "Employer Name",
            }

            lines = ["## LOAN APPLICATION (extracted by Document Intelligence)"]

            # Full name
            first = get_val("first_name") or ""
            last  = get_val("last_name")  or ""
            if first or last:
                lines.append(f"Applicant Full Name: {(first + ' ' + last).strip()}")

            for di_key, agent_label in FIELD_MAP.items():
                val = get_val(di_key)
                if val:
                    lines.append(f"{agent_label}: {val}")

            return "\n".join(lines)

        except ImportError:
            print("[DI] azure-ai-documentintelligence not installed — falling back")
        except Exception as e:
            print(f"[DI] Custom model failed: {e} — falling back")

    # Fallback: plain text read
    # PDF files cannot be read as plain text — return helpful placeholder
    if path.suffix.lower() == ".pdf":
        print(f"[DI] WARNING: {path.name} is PDF but DI did not run — upload .txt version or configure DI endpoint/key")
        return f"[PDF file {path.name} could not be read — Document Intelligence not configured or failed]"
    return path.read_text(encoding="utf-8", errors="replace")

# -------------------------------------------------------------------------
# In-memory session store
# session_id → { conversation_id, events[], status, vector_store_id, file_ids }
# -------------------------------------------------------------------------
sessions = {}
sessions_lock = threading.Lock()


def emit(session_id, obj):
    with sessions_lock:
        if session_id in sessions:
            sessions[session_id]["events"].append(obj)


def cleanup_foundry_files(session_id):
    """Delete uploaded files from Foundry after workflow completes."""
    with sessions_lock:
        s = sessions.get(session_id)
        if not s:
            return
        vs_id    = s.pop("vector_store_id", None)
        file_ids = s.pop("file_ids", []) or []
    if not vs_id:
        return
    try:
        cred   = DefaultAzureCredential()
        client = AIProjectClient(endpoint=ENDPOINT, credential=cred)
        with client:
            oc = client.get_openai_client()
            try:
                oc.vector_stores.delete(vector_store_id=vs_id)
            except Exception:
                pass
            for fid in file_ids:
                try:
                    oc.files.delete(file_id=fid)
                except Exception:
                    pass
    except Exception:
        pass


# -------------------------------------------------------------------------
# MAIN WORKFLOW RUNNER — uploads docs to Foundry, runs agents, stops at approval
# -------------------------------------------------------------------------
def run_workflow_until_approval(session_id, doc_text, uploaded_paths=None):
    """
    Background thread:
      1. Upload documents to Foundry vector store (so agents can search them)
      2. Start the workflow conversation
      3. Send documents + policy JSON → triggers all 4 agents
      4. Pause when workflow asks for underwriter decision
    """
    uploaded_paths = uploaded_paths or []
    try:
        cred   = DefaultAzureCredential()
        client = AIProjectClient(endpoint=ENDPOINT, credential=cred)
        with client:
            oc = client.get_openai_client()

            # -- Step A: Vector store upload (not needed — doc text is sent directly
            #            in the conversation, no semantic search required for this workflow)
            #
            # FUTURE EXTENSION: Uncomment if you add file_search tool to agents
            # and want agents to query documents via embeddings/RAG instead of
            # receiving the full text in the conversation.
            #
            # vs = oc.vector_stores.create(name=f"LoanSession-{session_id[:20]}")
            # for path in uploaded_paths:
            #     with open(path, "rb") as fh:
            #         oc.vector_stores.files.upload_and_poll(vector_store_id=vs.id, file=fh)
            vs_id    = None
            file_ids = []

            # -- Step B: Create conversation --
            conv = oc.conversations.create()
            with sessions_lock:
                if session_id not in sessions:
                    return
                sessions[session_id]["conversation_id"] = conv.id

            # -- Helper: process streaming events from Foundry --
            # Position-based fallback: agents always run in this order
            AGENT_ORDER = ['invoke-extractor', 'invoke-analyzer', 'invoke-risk-scorer', 'invoke-notifier']

            def stream_and_emit(stream, label):
                all_text           = []
                last_text_index    = 0
                last_action_id     = None
                saw_approval_q     = False
                agent_invoke_count = 0

                for event in stream:
                    if event.type == "response.failed":
                        err_obj = getattr(event, 'response', None)
                        err_msg = str(getattr(err_obj, 'error', event)) if err_obj else str(event)
                        emit(session_id, {"type": "step", "step": "error", "status": "error",
                                          "message": err_msg, "timestamp": _utc_ts()})
                        continue

                    if event.type == EVT_TEXT_DELTA:
                        all_text.append(getattr(event, "delta", "") or "")

                    elif event.type == EVT_TEXT_DONE:
                        t = getattr(event, "text", "") or ""
                        if t:
                            all_text.append(t)

                    elif event.type == EVT_ITEM_ADDED:
                        item = getattr(event, "item", None)
                        if item and getattr(item, "type", None) == "workflow_action":
                            last_text_index = len(all_text)

                    elif event.type == EVT_ITEM_DONE:
                        item = getattr(event, "item", None)
                        if item and getattr(item, "type", None) == "workflow_action":
                            aid       = getattr(item, "action_id", None) or ""
                            step_text = "".join(all_text[last_text_index:]).strip()

                            # Skip nodes with no output (SetVariable, etc.)
                            if not step_text:
                                last_text_index = len(all_text)
                                continue

                            summary  = _extract_step_summary(aid, step_text)
                            detected = summary.get("detected_id")

                            # Fallback: use position if content detection fails
                            if not detected:
                                if agent_invoke_count < len(AGENT_ORDER):
                                    detected = AGENT_ORDER[agent_invoke_count]
                                agent_invoke_count += 1
                            elif detected in AGENT_ORDER:
                                agent_invoke_count += 1

                            mapped_id      = detected or aid
                            last_action_id = mapped_id

                            # Detect approval gate
                            if mapped_id == "approval-question":
                                saw_approval_q = True

                            emit(session_id, {
                                "type": "step", "step": mapped_id, "status": "active",
                                "message": "Awaiting underwriter..." if mapped_id == "approval-question" else f"Running {mapped_id}...",
                                "timestamp": _utc_ts(),
                            })
                            emit(session_id, {
                                "type": "step", "step": mapped_id, "status": "completed",
                                "message": summary.get("summary", f"Completed {mapped_id}"),
                                "data":    summary.get("details"),
                                "timestamp": _utc_ts(),
                            })
                            last_text_index = len(all_text)

                    elif event.type == EVT_ERROR:
                        err = getattr(event, "error", None) or str(event)
                        emit(session_id, {
                            "type": "step", "step": "error", "status": "error",
                            "message": str(err), "timestamp": _utc_ts(),
                        })
                        with sessions_lock:
                            if session_id in sessions:
                                sessions[session_id]["status"] = "error"
                        return False

                full    = "".join(all_text)
                waiting = (
                    saw_approval_q
                    or last_action_id == "invoke-risk-scorer"
                    or ("APPROVE" in full.upper() and "DECLINE" in full.upper())
                )
                return waiting

            # -- Step C: Start workflow (answers the initial trigger) --
            stream1 = oc.responses.create(
                conversation=conv.id,
                extra_body={"agent_reference": {"name": WORKFLOW_NAME, "type": "agent_reference"}},
                input="Start",
                stream=True,
                metadata={"x-ms-debug-mode-enabled": "1"},
            )
            stream_and_emit(stream1, "init")

            # -- Step D: Send documents + policy rules → triggers all agents --
            stream2 = oc.responses.create(
                conversation=conv.id,
                extra_body={"agent_reference": {"name": WORKFLOW_NAME, "type": "agent_reference"}},
                input=doc_text,
                stream=True,
                metadata={"x-ms-debug-mode-enabled": "1"},
            )
            waiting = stream_and_emit(stream2, "documents")

            with sessions_lock:
                if session_id not in sessions:
                    return
                sessions[session_id]["stream_complete"] = True
                sessions[session_id]["status"] = "waiting" if waiting else "complete"

            if waiting:
                emit(session_id, {
                    "type": "step", "step": "approval", "status": "waiting",
                    "message": "Please APPROVE or DECLINE this application.",
                    "show_approval_buttons": True,
                    "timestamp": _utc_ts(),
                })
            else:
                cleanup_foundry_files(session_id)

    except Exception as e:
        import traceback
        full_error = traceback.format_exc()
        print("=== WORKFLOW ERROR ===")
        print(full_error)
        print("=====================")
        emit(session_id, {
            "type": "step", "step": "error", "status": "error",
            "message": str(e), "timestamp": _utc_ts(),
        })
        with sessions_lock:
            if session_id in sessions:
                sessions[session_id]["status"] = "error"
        cleanup_foundry_files(session_id)


# -------------------------------------------------------------------------
# APPROVAL RUNNER — continues the workflow after underwriter decides
# -------------------------------------------------------------------------
def run_workflow_approval(session_id, decision):
    with sessions_lock:
        conv_id = sessions.get(session_id, {}).get("conversation_id")
    if not conv_id:
        return None, "Session or conversation not found"
    try:
        cred   = DefaultAzureCredential()
        client = AIProjectClient(endpoint=ENDPOINT, credential=cred)
        all_text        = []
        last_text_index = 0
        with client:
            oc = client.get_openai_client()
            stream = oc.responses.create(
                conversation=conv_id,
                extra_body={"agent_reference": {"name": WORKFLOW_NAME, "type": "agent_reference"}},
                input=decision,
                stream=True,
                metadata={"x-ms-debug-mode-enabled": "1"},
            )
            for event in stream:
                if event.type == "response.failed":
                    err_obj = getattr(event, 'response', None)
                    err_msg = str(getattr(err_obj, 'error', event)) if err_obj else str(event)
                    emit(session_id, {"type": "step", "step": "error", "status": "error",
                                      "message": err_msg, "timestamp": _utc_ts()})
                    continue
                if event.type == EVT_TEXT_DELTA:
                    all_text.append(getattr(event, "delta", "") or "")
                elif event.type == EVT_TEXT_DONE:
                    all_text.append(getattr(event, "text", "") or "")
                elif event.type == EVT_ITEM_ADDED:
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "workflow_action":
                        last_text_index = len(all_text)
                elif event.type == EVT_ITEM_DONE:
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "workflow_action":
                        aid       = getattr(item, "action_id", None) or ""
                        step_text = "".join(all_text[last_text_index:]).strip()
                        if not step_text:
                            last_text_index = len(all_text)
                            continue
                        summary   = _extract_step_summary(aid, step_text)
                        mapped_id = summary.get("detected_id") or aid
                        emit(session_id, {
                            "type": "step", "step": mapped_id, "status": "active",
                            "message": "Sending notification...", "timestamp": _utc_ts(),
                        })
                        emit(session_id, {
                            "type": "step", "step": mapped_id, "status": "completed",
                            "message": summary.get("summary", "Notification prepared"),
                            "data":    summary.get("details"),
                            "timestamp": _utc_ts(),
                        })
                        last_text_index = len(all_text)
        cleanup_foundry_files(session_id)
        return "".join(all_text), None
    except Exception as e:
        cleanup_foundry_files(session_id)
        return None, str(e)


def _run_approval_background(session_id, decision):
    emit(session_id, {
        "type": "step", "step": "approval_submit", "status": "active",
        "message": f"Processing {decision} decision...", "timestamp": _utc_ts(),
    })
    try:
        response_text, err = run_workflow_approval(session_id, decision)
        with sessions_lock:
            s = sessions.get(session_id)
            if s:
                s["status"]            = "complete" if err is None else "error"
                s["approval_response"] = response_text
                s["approval_error"]    = err
        emit(session_id, {
            "type": "step", "step": "approval_submit",
            "status": "completed" if err is None else "error",
            "message": response_text or err or f"{decision} processed.",
            "timestamp": _utc_ts(),
        })
    except Exception as e:
        with sessions_lock:
            s = sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["approval_error"] = str(e)
        emit(session_id, {
            "type": "step", "step": "approval_submit", "status": "error",
            "message": str(e), "timestamp": _utc_ts(),
        })


# -------------------------------------------------------------------------
# Flask API
# -------------------------------------------------------------------------
app = Flask(__name__, static_folder=str(PORTAL_DIR), static_url_path="")


@app.route("/")
def index():
    return send_from_directory(PORTAL_DIR, "loan.html")


@app.route("/samples/<filename>")
def download_sample(filename):
    """Serve sample documents for download — only allows known safe filenames."""
    allowed = {
        "LOAN_APP_001_GOOD.txt", "INCOME_001_GOOD.txt",
        "LOAN_APP_002_HIGH_DTI.txt", "INCOME_002_HIGH_DTI.txt",
        "LOAN_APP_003_DECLINE.txt", "INCOME_003_DECLINE.txt",
        "LOAN_APP_001_GOOD.pdf", "LOAN_APP_002_HIGH_DTI.pdf", "LOAN_APP_003_DECLINE.pdf",
    }
    if filename not in allowed:
        return Response("Not found", status=404)
    return send_from_directory(BASE_DIR / "sample_docs", filename, as_attachment=True)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "application" not in request.files or "income" not in request.files:
        return Response("Missing application or income file", status=400)

    app_file    = request.files["application"]
    income_file = request.files["income"]

    if not app_file or not income_file:
        return Response("Missing application or income file", status=400)

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    prefix     = f"loan_{session_id}"
    parts      = []
    uploaded_paths = []

    for key, f in [("application", app_file), ("income", income_file)]:
        if f and f.filename:
            safe = f.filename.replace("..", "_").replace(os.sep, "_")
            path = UPLOADS_DIR / f"{prefix}_{key}_{safe}"
            f.save(path)
            uploaded_paths.append(path)
            try:
                # Loan application PDF → Document Intelligence custom model
                # Income proof → plain text (agent handles any format)
                is_app = (key == "application")
                parts.append(extract_text_from_file(path, is_loan_application=is_app))
            except Exception:
                parts.append(f"[Contents of {f.filename}]")

    if not any(p.strip() for p in parts):
        return Response("Uploaded files are empty", status=400)

    # Append policy rules so the risk-scorer agent can reference them
    parts.append(policy_context_text())

    doc_text = "\n\n---\n\n".join(parts)

    with sessions_lock:
        sessions[session_id] = {
            "conversation_id": None,
            "events":          [],
            "status":          "running",
            "vector_store_id": None,
            "file_ids":        [],
            "stream_complete": False,
        }

    emit(session_id, {
        "type": "step", "step": "upload", "status": "completed",
        "message": "Documents received. Starting pre-screening workflow...",
        "data": {
            "application": app_file.filename,
            "income":      income_file.filename,
            "policy":      "loan_policies.json loaded",
        },
        "timestamp": _utc_ts(),
    })

    t = threading.Thread(target=run_workflow_until_approval, args=(session_id, doc_text, uploaded_paths))
    t.daemon = True
    t.start()

    return Response(json.dumps({"session_id": session_id}), status=200, mimetype="application/json")


@app.route("/api/progress/<session_id>")
def api_progress(session_id):
    def generate():
        idx = 0
        while True:
            with sessions_lock:
                s = sessions.get(session_id)
                if not s:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Session not found'})}\n\n"
                    return
                evs    = s["events"]
                status = s["status"]

            while idx < len(evs):
                ev = evs[idx]
                idx += 1
                payload = {
                    "type":      ev.get("type", "step"),
                    "step":      ev.get("step", ""),
                    "status":    ev.get("status", ""),
                    "message":   ev.get("message", ""),
                    "timestamp": ev.get("timestamp"),
                }
                if ev.get("data"):
                    payload["data"] = ev["data"]
                if ev.get("show_approval_buttons"):
                    payload["show_approval_buttons"] = True
                    payload["status"] = "waiting"
                yield f"data: {json.dumps(payload)}\n\n"

            if status in ("complete", "error"):
                yield f"data: {json.dumps({'status': status})}\n\n"
                return

            with sessions_lock:
                s2 = sessions.get(session_id)
                current_status  = s2.get("status", "running") if s2 else "error"
                stream_complete = s2.get("stream_complete", False) if s2 else True

            if current_status == "waiting" and stream_complete:
                yield f"data: {json.dumps({'status': 'waiting', 'step': 'approval', 'show_approval_buttons': True})}\n\n"
                return
            if current_status in ("complete", "error"):
                yield f"data: {json.dumps({'status': current_status})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
            import time
            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/status/<session_id>")
def api_status(session_id):
    with sessions_lock:
        s = sessions.get(session_id)
        if not s:
            return Response(json.dumps({"status": "not_found"}), status=200, mimetype="application/json")
        out = {"status": s["status"]}
        if "approval_response" in s:
            out["response"] = s.get("approval_response") or ""
        if s.get("approval_error"):
            out["error"] = s["approval_error"]
    return Response(json.dumps(out), status=200, mimetype="application/json")


@app.route("/api/approve/<session_id>", methods=["POST"])
def api_approve(session_id):
    decision = (request.args.get("decision") or "APPROVE").upper()
    if decision not in ("APPROVE", "DECLINE"):
        return Response(
            json.dumps({"error": "decision must be APPROVE or DECLINE"}),
            status=400, mimetype="application/json",
        )
    with sessions_lock:
        if session_id not in sessions:
            return Response(json.dumps({"error": "Session not found"}), status=404, mimetype="application/json")
        if sessions[session_id]["status"] != "waiting":
            return Response(json.dumps({"error": "Session is not waiting for approval"}), status=400, mimetype="application/json")
        sessions[session_id]["status"] = "approving"

    t = threading.Thread(target=_run_approval_background, args=(session_id, decision))
    t.daemon = True
    t.start()

    return Response(json.dumps({"ok": True, "status": "processing"}), status=202, mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"Loan Portal running at: http://localhost:{port}")
    print(f"Workflow: {WORKFLOW_NAME}")
    print(f"Endpoint: {ENDPOINT[:60]}..." if len(ENDPOINT) > 60 else f"Endpoint: {ENDPOINT}")
    print(f"Policies loaded: {list(LOAN_POLICIES.get('loan_types', {}).keys())}")
    app.run(host="0.0.0.0", port=port, threaded=True)
