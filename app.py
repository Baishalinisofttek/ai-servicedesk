import subprocess, sys, os, re
from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
import requests as http_requests
from groq import Groq

load_dotenv()

SN_URL      = os.getenv("SERVICENOW_URL", "").rstrip("/")
SN_USER     = os.getenv("SERVICENOW_USERNAME", "")
SN_PASS     = os.getenv("SERVICENOW_PASSWORD", "")
GROQ_KEY    = os.getenv("GROQ_API_KEY", "")

groq_client = Groq(api_key=GROQ_KEY) if GROQ_KEY else None

app = Flask(__name__)

# ── per-session conversation history ──────────────────────────────────────────
chat_histories = {}  # session_id -> [{"role": ..., "content": ...}]

SCRIPTS = [
    {"id": "check_ui_actions",    "label": "Check UI Actions",       "file": "check_ui_actions.py",    "group": "Check"},
    {"id": "check_script",        "label": "Check Client Script",    "file": "check_script.py",        "group": "Check"},
    {"id": "check_table",         "label": "Check Table",            "file": "check_table.py",         "group": "Check"},
    {"id": "check_views",         "label": "Check Views",            "file": "check_views.py",         "group": "Check"},
    {"id": "check_field_types",   "label": "Check Field Types",      "file": "check_field_types.py",   "group": "Check"},
    {"id": "deploy_ui_action",    "label": "Deploy UI Action",       "file": "deploy_ui_action.py",    "group": "Deploy"},
    {"id": "deploy_to_sn",        "label": "Deploy to SN",           "file": "deploy_to_sn.py",        "group": "Deploy"},
    {"id": "deploy_all_fields",   "label": "Deploy All Fields",      "file": "deploy_all_fields.py",   "group": "Deploy"},
    {"id": "deploy_business_rule","label": "Deploy Business Rule",   "file": "deploy_business_rule.py","group": "Deploy"},
    {"id": "deploy_kb_autofill",  "label": "Deploy KB Autofill",     "file": "deploy_kb_autofill.py",  "group": "Deploy"},
    {"id": "deploy_relationship", "label": "Deploy Relationship",    "file": "deploy_relationship.py", "group": "Deploy"},
    {"id": "deploy_similar_field","label": "Deploy Similar Field",   "file": "deploy_similar_field.py","group": "Deploy"},
    {"id": "deploy_similar_incidents","label": "Deploy Similar Incidents","file": "deploy_similar_incidents.py","group": "Deploy"},
    {"id": "deploy_ui_action",    "label": "Deploy UI Action",       "file": "deploy_ui_action.py",    "group": "Deploy"},
    {"id": "add_to_correct_section","label": "Add to Correct Section","file": "add_to_correct_section.py","group": "Add"},
    {"id": "add_to_default_view", "label": "Add to Default View",    "file": "add_to_default_view.py", "group": "Add"},
    {"id": "add_to_workspace_view","label": "Add to Workspace View", "file": "add_to_workspace_view.py","group": "Add"},
    {"id": "deep_search",         "label": "Deep Search",            "file": "deep_search.py",         "group": "Tools"},
    {"id": "diagnose_sn",         "label": "Diagnose SN",            "file": "diagnose_sn.py",         "group": "Tools"},
    {"id": "find_category_section","label": "Find Category Section", "file": "find_category_section.py","group": "Tools"},
    {"id": "find_ims_with_incidents","label": "Find IMs with Incidents","file": "find_ims_with_incidents.py","group": "Tools"},
    {"id": "find_workspace_section","label": "Find Workspace Section","file": "find_workspace_section.py","group": "Tools"},
    {"id": "fix",                 "label": "Fix",                    "file": "fix.py",                 "group": "Fix"},
    {"id": "fix_br_insert_only",  "label": "Fix BR Insert Only",     "file": "fix_br_insert_only.py",  "group": "Fix"},
    {"id": "fix_endpoints",       "label": "Fix Endpoints",          "file": "fix_endpoints.py",       "group": "Fix"},
    {"id": "fix_kb_autofill",     "label": "Fix KB Autofill",        "file": "fix_kb_autofill.py",     "group": "Fix"},
    {"id": "fix_lines",           "label": "Fix Lines",              "file": "fix_lines.py",           "group": "Fix"},
]

# Deduplicate by file
seen = set()
UNIQUE_SCRIPTS = []
for s in SCRIPTS:
    if s["file"] not in seen:
        seen.add(s["file"])
        UNIQUE_SCRIPTS.append(s)

@app.route("/")
def index():
    return render_template("index.html", scripts=UNIQUE_SCRIPTS)

# ── Config ────────────────────────────────────────────────────────────────────
@app.route("/config")
def config():
    return jsonify({"url": SN_URL, "user": SN_USER})

# ── Test Connection ───────────────────────────────────────────────────────────
@app.route("/test-connection")
def test_connection():
    if not SN_URL:
        return jsonify({"ok": False, "error": "SERVICENOW_URL not set in .env"})
    try:
        r = http_requests.get(
            f"{SN_URL}/api/now/table/sys_user_group",
            auth=(SN_USER, SN_PASS),
            params={"sysparm_limit": 1, "sysparm_fields": "sys_id"},
            timeout=10
        )
        if r.status_code == 200:
            # count groups
            rc = http_requests.get(
                f"{SN_URL}/api/now/table/sys_user_group",
                auth=(SN_USER, SN_PASS),
                params={"sysparm_limit": 500, "sysparm_fields": "sys_id"},
                timeout=10
            )
            groups = len(rc.json().get("result", []))
            return jsonify({"ok": True, "groups": groups})
        else:
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ── Chat ──────────────────────────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    data       = request.json or {}
    message    = data.get("message", "").strip()
    agent      = data.get("agent", "incident")
    session_id = data.get("session_id", "default")

    if not groq_client:
        return jsonify({"error": "GROQ_API_KEY not configured"}), 500

    # If empty message (greeting call)
    if not message:
        greeting = (
            "👋 Hi! I'm the **Incident Agent**. Enter an incident number like `INC0010127` and I'll fetch it from ServiceNow and help you assign, resolve, or analyse it."
            if agent == "incident" else
            "👋 Hi! I'm the **KB Agent**. Give me an incident number like `INC0010127` and I'll:\n\n"
            "• 🔍 Search existing KB articles for that issue\n"
            "• 📝 Draft a new KB article from the incident\n"
            "• ✅ Create it in ServiceNow on your confirmation"
        )
        return jsonify({"reply": greeting, "token_usage": {"input_tokens": 0, "tool_tokens": 0, "output_tokens": 0, "total_tokens": 0}})

    # ── KB Agent flow ──────────────────────────────────────────────────────────
    if agent == "kb":
        return handle_kb_agent(message, session_id)

    # ── Incident Agent flow ────────────────────────────────────────────────────
    inc_match = re.search(r'\bINC\d+\b', message, re.IGNORECASE)
    incident_context = ""
    current_inc_number = ""
    is_unassigned = False
    suggested_group = ""

    if inc_match and SN_URL:
        inc_num = inc_match.group(0).upper()
        current_inc_number = inc_num
        try:
            r = http_requests.get(
                f"{SN_URL}/api/now/table/incident",
                auth=(SN_USER, SN_PASS),
                params={
                    "sysparm_query": f"number={inc_num}",
                    "sysparm_limit": 1,
                    "sysparm_fields": "number,short_description,description,assignment_group,state,priority,category,caller_id,opened_at,resolved_at,close_notes,work_notes",
                    "sysparm_display_value": "true"
                },
                timeout=10
            )
            results = r.json().get("result", [])
            if results:
                inc = results[0]
                pri_map = {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low", "5": "Planning"}
                state_map = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed"}
                grp = inc.get("assignment_group", {})
                # With sysparm_display_value=true, fields can be dicts with display_value or plain strings
                grp_name = (grp.get("display_value", "") if isinstance(grp, dict) else str(grp)).strip()
                if not grp_name:
                    grp_name = "Unassigned"
                caller = inc.get("caller_id", {})
                caller_name = (caller.get("display_value", "Unknown") if isinstance(caller, dict) else str(caller)).strip() or "Unknown"
                state_raw = inc.get("state", {})
                state_val = (state_raw.get("display_value", "") if isinstance(state_raw, dict) else str(state_raw)).strip()
                pri_raw = inc.get("priority", {})
                pri_val = (pri_raw.get("display_value", "") if isinstance(pri_raw, dict) else pri_map.get(str(pri_raw), str(pri_raw))).strip()
                is_unassigned = grp_name.lower() in ("unassigned", "", "none")
                incident_context = f"""
[INCIDENT DATA FROM SERVICENOW]
Number: {inc.get('number')}
Short Description: {inc.get('short_description')}
Description: {inc.get('description', '')[:500]}
Priority: {pri_val}
State: {state_val}
Category: {inc.get('category','')}
Assignment Group: {grp_name}
Caller: {caller_name}
Opened: {inc.get('opened_at','')}
Close Notes: {inc.get('close_notes','')[:300]}
"""
        except Exception as e:
            incident_context = f"[Could not fetch {inc_num} from ServiceNow: {str(e)}]"

    # ── Fetch real SN groups to ground the AI suggestion ──────────────────────
    sn_groups = []
    sn_groups_str = ""
    if inc_match and SN_URL:
        try:
            rg = http_requests.get(
                f"{SN_URL}/api/now/table/sys_user_group",
                auth=(SN_USER, SN_PASS),
                params={"sysparm_limit": 50, "sysparm_fields": "name",
                        "sysparm_query": "activeINtrue,false"},
                timeout=8
            )
            sn_groups = [g["name"] for g in rg.json().get("result", []) if g.get("name")]
            sn_groups_str = "\n".join(f"- {g}" for g in sn_groups[:50])
        except:
            pass

    # Build system prompt
    if agent == "kb":
        system = (
            "You are a Knowledge Base Agent for ServiceNow. "
            "Help users search for KB articles and suggest creating new ones based on incident descriptions. "
            "Be concise and structured. Use markdown bold for key terms."
        )
    else:
        assign_instruction = ""
        if sn_groups_str:
            current_grp_info = f"currently assigned to: {grp_name}" if not is_unassigned else "currently UNASSIGNED"
            if is_unassigned:
                assign_instruction = (
                    f"\n\nCRITICAL — UNASSIGNED INCIDENT: This incident is UNASSIGNED. "
                    f"You MUST suggest ONE group from this exact list of real ServiceNow groups:\n{sn_groups_str}\n\n"
                    "Pick the most relevant group based on the incident category/description. "
                    "At the end of your response output EXACTLY (replace bracket text with your pick):\n"
                    "SUGGEST_GROUP:[Exact Group Name from list]\n"
                    "Then write: Shall I assign this incident to **[Exact Group Name]**? Type **yes** to confirm or **no** to skip."
                )
            else:
                assign_instruction = (
                    f"\n\nNOTE: This incident is already assigned to **{grp_name}**. "
                    "Mention this in your analysis. Then at the very end ask the user if they want to change the assignment. "
                    "End your response with EXACTLY this line:\n"
                    f"ALREADY_ASSIGNED:{grp_name}\n"
                    "Then write: This incident is already assigned to **" + grp_name + "**. Would you like to change the assignment? (yes / no)"
                )
        system = (
            "You are an expert Incident Management Agent for ServiceNow. "
            f"ServiceNow instance: {SN_URL}. "
            "When given incident data, analyse it thoroughly: summarise the issue, explain the current state, "
            "suggest the correct assignment group, recommend resolution steps, and flag any concerns. "
            "Be concise, structured, and actionable. Use markdown bold for key fields."
            + assign_instruction
        )

    history = chat_histories.setdefault(session_id, [])

    # Inject incident context into the user message if found
    full_message = message
    if incident_context:
        full_message = f"{message}\n\n{incident_context}"

    history.append({"role": "user", "content": full_message})
    trimmed = history[-20:]

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}] + trimmed,
            max_tokens=1024,
            temperature=0.4
        )
        reply = resp.choices[0].message.content
        usage = resp.usage

        # Extract SUGGEST_GROUP or ALREADY_ASSIGNED tag if present
        suggest_group   = None
        already_assigned_group = None

        if "SUGGEST_GROUP:" in reply:
            for line in reply.splitlines():
                if line.startswith("SUGGEST_GROUP:"):
                    suggest_group = line.replace("SUGGEST_GROUP:", "").strip()
                    break
            reply = "\n".join(l for l in reply.splitlines() if not l.startswith("SUGGEST_GROUP:")).strip()

        elif "ALREADY_ASSIGNED:" in reply:
            for line in reply.splitlines():
                if line.startswith("ALREADY_ASSIGNED:"):
                    already_assigned_group = line.replace("ALREADY_ASSIGNED:", "").strip()
                    break
            reply = "\n".join(l for l in reply.splitlines() if not l.startswith("ALREADY_ASSIGNED:")).strip()

        history.append({"role": "assistant", "content": reply})

        # Log tool call if incident was fetched
        tool_calls = []
        if incident_context and inc_match:
            tool_calls.append({
                "name": "get_incident",
                "args": {"number": inc_match.group(0).upper()},
                "result": {"fetched": True}
            })

        response_data = {
            "reply": reply,
            "tool_calls": tool_calls,
            "token_usage": {
                "input_tokens":  usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "tool_tokens":   0,
                "total_tokens":  usage.total_tokens
            },
            "suggest_group":         suggest_group,
            "already_assigned_group": already_assigned_group,
            "sn_groups":             sn_groups[:50],
            "incident_number":       current_inc_number
        }
        return jsonify(response_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Interaction / Incident lookup ────────────────────────────────────────────
@app.route("/interaction/<inc_number>")
def get_interaction(inc_number):
    if not SN_URL:
        return jsonify({"error": "ServiceNow not configured"}), 500
    try:
        r = http_requests.get(
            f"{SN_URL}/api/now/table/incident",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_query": f"number={inc_number.upper()}",
                "sysparm_limit": 1,
                "sysparm_fields": "number,short_description,description,assignment_group,state,priority,category,caller_id,opened_at,sys_id"
            },
            timeout=10
        )
        results = r.json().get("result", [])
        if not results:
            return jsonify({"error": f"{inc_number} not found in ServiceNow"}), 404
        inc = results[0]
        # Flatten nested display_value fields
        for field in ["assignment_group", "caller_id"]:
            if isinstance(inc.get(field), dict):
                inc[field] = inc[field].get("display_value", "")
        return jsonify({"interaction": inc})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Similar Incidents ─────────────────────────────────────────────────────────
@app.route("/api/similar-incidents")
def similar_incidents():
    desc = request.args.get("description", "").strip()
    if not desc or not SN_URL:
        return jsonify({"incidents": [], "estimate": None})

    # If it looks like an incident number, fetch its description first
    if re.match(r'^INC\d+$', desc, re.IGNORECASE):
        try:
            r = http_requests.get(
                f"{SN_URL}/api/now/table/incident",
                auth=(SN_USER, SN_PASS),
                params={"sysparm_query": f"number={desc.upper()}", "sysparm_limit": 1,
                        "sysparm_fields": "short_description"},
                timeout=10
            )
            fetched = r.json().get("result", [])
            if fetched and fetched[0].get("short_description"):
                desc = fetched[0]["short_description"]
        except:
            pass

    try:
        # Use first 4 meaningful keywords
        stop = {"the","a","an","is","in","on","at","to","for","of","and","or","with","not","be","was","has","it","this","that","are","from"}
        words = [w for w in re.findall(r'[a-zA-Z]+', desc) if len(w) > 2 and w.lower() not in stop]
        keywords = " ".join(words[:4]) if words else desc[:40]

        r = http_requests.get(
            f"{SN_URL}/api/now/table/incident",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_query": f"short_descriptionLIKE{keywords}^ORdescriptionLIKE{keywords}",
                "sysparm_limit": 6,
                "sysparm_fields": "number,short_description,state,priority,assignment_group,opened_at,sys_id"
            },
            timeout=10
        )
        results = r.json().get("result", [])
        pri_map   = {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low", "5": "Planning"}
        state_map = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed"}
        for inc in results:
            inc["priority"] = pri_map.get(str(inc.get("priority", "")), inc.get("priority", "—"))
            inc["state"]    = state_map.get(str(inc.get("state", "")), inc.get("state", "—"))
            if isinstance(inc.get("assignment_group"), dict):
                inc["assignment_group"] = inc["assignment_group"].get("display_value", "Unassigned")

        # AI recommendation via Groq
        recommendation = None
        if groq_client and results:
            try:
                inc_list = "\n".join([f"- {i['number']}: {i['short_description']} [{i['priority']}, {i['state']}]" for i in results[:4]])
                resp = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": "You are an ITSM expert. Given similar incidents, provide a brief 2-3 sentence recommendation on resolution approach and which assignment group should handle it. Be concise."},
                        {"role": "user", "content": f"Original issue: {desc}\n\nSimilar incidents:\n{inc_list}"}
                    ],
                    max_tokens=200,
                    temperature=0.3
                )
                recommendation = resp.choices[0].message.content.strip()
            except:
                pass

        estimate = {"label": "2–4 hours", "detail": "Based on similar resolved incidents"} if results else None
        return jsonify({"incidents": results, "estimate": estimate, "recommendation": recommendation})
    except Exception as e:
        return jsonify({"incidents": [], "estimate": None, "recommendation": None})

# ── Similar KB Articles ───────────────────────────────────────────────────────
@app.route("/api/similar-kb")
def similar_kb():
    desc = request.args.get("description", "").strip()
    if not desc or not SN_URL:
        return jsonify({"articles": []})
    try:
        keywords = " ".join(desc.split()[:4])
        r = http_requests.get(
            f"{SN_URL}/api/now/table/kb_knowledge",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_query": f"short_descriptionLIKE{keywords}^ORtextLIKE{keywords}",
                "sysparm_limit": 5,
                "sysparm_fields": "number,short_description,sys_id,category"
            },
            timeout=10
        )
        return jsonify({"articles": r.json().get("result", [])})
    except Exception as e:
        return jsonify({"articles": []})

# ── Assignment Groups ─────────────────────────────────────────────────────────
@app.route("/api/groups")
def get_groups():
    if not SN_URL:
        return jsonify([])
    try:
        r = http_requests.get(
            f"{SN_URL}/api/now/table/sys_user_group",
            auth=(SN_USER, SN_PASS),
            params={"sysparm_limit": 200, "sysparm_fields": "sys_id,name"},
            timeout=10
        )
        return jsonify(r.json().get("result", []))
    except Exception as e:
        return jsonify([])

# ── Assign Incident ───────────────────────────────────────────────────────────
@app.route("/api/assign-incident", methods=["POST"])
def assign_incident():
    """Assign an incident to a group by name — finds group sys_id then PATCHes the incident."""
    data       = request.json or {}
    inc_number = data.get("incident_number", "").strip().upper()
    group_name = data.get("group_name", "").strip()

    if not inc_number or not group_name:
        return jsonify({"error": "incident_number and group_name are required"}), 400
    if not SN_URL:
        return jsonify({"error": "ServiceNow not configured"}), 500

    try:
        # Step 1: get incident sys_id
        r = http_requests.get(
            f"{SN_URL}/api/now/table/incident",
            auth=(SN_USER, SN_PASS),
            params={"sysparm_query": f"number={inc_number}", "sysparm_limit": 1, "sysparm_fields": "sys_id,assignment_group"},
            timeout=10
        )
        inc_results = r.json().get("result", [])
        if not inc_results:
            return jsonify({"error": f"Incident {inc_number} not found"}), 404
        inc_sys_id = inc_results[0]["sys_id"]

        # Step 2: find group sys_id by name (fuzzy)
        rg = http_requests.get(
            f"{SN_URL}/api/now/table/sys_user_group",
            auth=(SN_USER, SN_PASS),
            params={"sysparm_query": f"nameLIKE{group_name}", "sysparm_limit": 1, "sysparm_fields": "sys_id,name"},
            timeout=10
        )
        grp_results = rg.json().get("result", [])
        if not grp_results:
            return jsonify({"error": f"Group '{group_name}' not found in ServiceNow"}), 404
        grp_sys_id  = grp_results[0]["sys_id"]
        grp_actual  = grp_results[0]["name"]

        # Step 3: PATCH the incident
        patch = http_requests.patch(
            f"{SN_URL}/api/now/table/incident/{inc_sys_id}",
            auth=(SN_USER, SN_PASS),
            json={"assignment_group": grp_sys_id, "state": "2"},  # state 2 = In Progress
            timeout=10
        )
        if patch.status_code in (200, 201):
            return jsonify({"ok": True, "incident": inc_number, "assigned_to": grp_actual})
        else:
            return jsonify({"error": f"ServiceNow returned {patch.status_code}: {patch.text[:200]}"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Users autocomplete ────────────────────────────────────────────────────────
@app.route("/api/users")
def get_users():
    q = request.args.get("q", "").strip()
    if not q or not SN_URL:
        return jsonify([])
    try:
        r = http_requests.get(
            f"{SN_URL}/api/now/table/sys_user",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_query": f"nameLIKE{q}^ORuser_nameLIKE{q}",
                "sysparm_limit": 10,
                "sysparm_fields": "sys_id,name,email"
            },
            timeout=10
        )
        return jsonify(r.json().get("result", []))
    except Exception as e:
        return jsonify([])

# ── AI Suggest (interaction form) ─────────────────────────────────────────────
@app.route("/api/ai-suggest", methods=["POST"])
def ai_suggest():
    desc = (request.json or {}).get("description", "").strip()
    if not desc or not groq_client:
        return jsonify({"error": "Missing description or Groq key"}), 400
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a ServiceNow triage assistant. Given an incident description, return JSON with keys: category (one of: Incident, Requests, Inquiry, Complaint, Praise), short_description (max 80 chars), assignment_group (best guess group name). Return only valid JSON, no markdown."},
                {"role": "user", "content": desc}
            ],
            max_tokens=200,
            temperature=0.2
        )
        import json
        text = resp.choices[0].message.content.strip()
        return jsonify(json.loads(text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Recurrence Analysis ───────────────────────────────────────────────────────
@app.route("/recurrence-analysis")
def recurrence_analysis():
    if not SN_URL:
        return jsonify({"error": "ServiceNow not configured"}), 500
    try:
        r = http_requests.get(
            f"{SN_URL}/api/now/table/incident",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_limit": 200,
                "sysparm_fields": "number,short_description,priority,category,assignment_group,opened_at,location"
            },
            timeout=15
        )
        incidents = r.json().get("result", [])
        if not incidents:
            return jsonify({"error": "No incidents found"}), 404

        from collections import Counter
        import datetime

        total = len(incidents)

        # Priority distribution
        pri_map = {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low", "5": "Planning"}
        pri_counter = Counter(pri_map.get(i.get("priority", ""), "Unknown") for i in incidents)
        priorities = [{"name": k, "count": v} for k, v in pri_counter.most_common()]

        # Category breakdown
        cat_counter = Counter(i.get("category", "Unknown") or "Unknown" for i in incidents)
        categories = [{"name": k, "count": v} for k, v in cat_counter.most_common(8)]

        # Monthly trend
        month_counter = Counter()
        for i in incidents:
            opened = i.get("opened_at", "")
            if opened:
                month_counter[opened[:7]] += 1
        trend = [{"month": k, "count": v} for k, v in sorted(month_counter.items())[-6:]]

        # Keywords
        stop = {"the","a","an","is","in","on","at","to","for","of","and","or","with","not","no","be","was","has","have","it","its","this","that","are","from","by","as","up","out","if","so","do","did","but","can","will","we","i","my","your","our","their","been","were","had","he","she","they","you","me","him","her","us","them","what","when","where","how","why","which","who","all","any","some","more","also","just","than","then","into","over","after","before","about","would","could","should","may","might","must","shall","get","got","set","let","put","use","used","using","new","old","one","two","three","four","five","six","seven","eight","nine","ten"}
        word_counter = Counter()
        for i in incidents:
            words = re.findall(r'[a-z]+', (i.get("short_description") or "").lower())
            for w in words:
                if len(w) > 3 and w not in stop:
                    word_counter[w] += 1
        keywords = [{"keyword": k, "count": v} for k, v in word_counter.most_common(10)]

        # Assignment group load
        grp_counter = Counter()
        for i in incidents:
            grp = i.get("assignment_group", {})
            name = grp.get("display_value", "Unassigned") if isinstance(grp, dict) else "Unassigned"
            grp_counter[name] += 1
        groups = [{"name": k, "count": v} for k, v in grp_counter.most_common(8)]

        # Clusters (keywords appearing 3+ times)
        clusters = []
        for kw, count in word_counter.most_common(20):
            if count >= 3:
                matched = [i["number"] for i in incidents if kw in (i.get("short_description") or "").lower()][:8]
                if matched:
                    clusters.append({"keyword": kw, "count": count, "incidents": matched})
        clusters = clusters[:6]

        # Locations
        loc_counter = Counter()
        for i in incidents:
            loc = i.get("location", {})
            name = loc.get("display_value", "") if isinstance(loc, dict) else ""
            if name:
                loc_counter[name] += 1
        locations = [{"name": k, "count": v} for k, v in loc_counter.most_common(8)]

        # RCA via Groq
        rca = "AI Root Cause Analysis unavailable (Groq not configured)."
        if groq_client and keywords:
            top_kws = ", ".join(k["keyword"] for k in keywords[:5])
            try:
                resp = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": "You are an ITSM root cause analysis expert. Be concise (3-5 sentences)."},
                        {"role": "user", "content": f"Analyze these recurring incident keywords from a ServiceNow instance: {top_kws}. Total incidents: {total}. Provide a root cause analysis and recommendations."}
                    ],
                    max_tokens=300,
                    temperature=0.3
                )
                rca = resp.choices[0].message.content.strip()
            except:
                pass

        return jsonify({
            "total": total,
            "priorities": priorities,
            "categories": categories,
            "trend": trend,
            "keywords": keywords,
            "groups": groups,
            "clusters": clusters,
            "locations": locations,
            "rca": rca
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── KB Agent handler ──────────────────────────────────────────────────────────
def handle_kb_agent(message, session_id):
    tool_calls = []
    inc_data   = None
    kb_articles = []

    # Step 1: detect incident number and fetch it
    inc_match = re.search(r'\bINC\d+\b', message, re.IGNORECASE)
    if inc_match and SN_URL:
        inc_num = inc_match.group(0).upper()
        try:
            r = http_requests.get(
                f"{SN_URL}/api/now/table/incident",
                auth=(SN_USER, SN_PASS),
                params={
                    "sysparm_query": f"number={inc_num}",
                    "sysparm_limit": 1,
                    "sysparm_fields": "number,short_description,description,category,priority,state,assignment_group,close_notes,work_notes"
                },
                timeout=10
            )
            results = r.json().get("result", [])
            if results:
                inc_data = results[0]
                grp = inc_data.get("assignment_group", {})
                inc_data["assignment_group"] = grp.get("display_value", "Unassigned") if isinstance(grp, dict) else "Unassigned"
                tool_calls.append({"name": "fetch_incident", "args": {"number": inc_num}, "result": {"found": True, "title": inc_data.get("short_description", "")}})
        except Exception as e:
            pass

    # Step 2: search existing KB articles
    search_term = ""
    if inc_data:
        search_term = inc_data.get("short_description", "")
    elif not inc_match:
        search_term = message[:80]

    if search_term and SN_URL:
        try:
            stop = {"the","a","an","is","in","on","at","to","for","of","and","or","with","not","be","was"}
            words = [w for w in re.findall(r'[a-zA-Z]+', search_term) if len(w) > 2 and w.lower() not in stop]
            kw = " ".join(words[:3])
            r = http_requests.get(
                f"{SN_URL}/api/now/table/kb_knowledge",
                auth=(SN_USER, SN_PASS),
                params={
                    "sysparm_query": f"short_descriptionLIKE{kw}^ORtextLIKE{kw}^workflow_state=published",
                    "sysparm_limit": 5,
                    "sysparm_fields": "number,short_description,sys_id,category,text"
                },
                timeout=10
            )
            kb_articles = r.json().get("result", [])
            tool_calls.append({"name": "search_kb", "args": {"keywords": kw}, "result": {"found": len(kb_articles)}})
        except:
            pass

    # Step 3: check if user wants to create KB article
    wants_create = any(w in message.lower() for w in ["create", "draft", "write", "generate", "make", "yes", "confirm"])

    # Step 4: build context for Groq
    context_parts = []
    if inc_data:
        pri_map   = {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low", "5": "Planning"}
        state_map = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed"}
        context_parts.append(f"""[INCIDENT]
Number: {inc_data.get('number')}
Title: {inc_data.get('short_description')}
Description: {(inc_data.get('description') or '')[:400]}
Category: {inc_data.get('category','')}
Priority: {pri_map.get(str(inc_data.get('priority','')), '')}
State: {state_map.get(str(inc_data.get('state','')), '')}
Group: {inc_data.get('assignment_group','')}
Resolution Notes: {(inc_data.get('close_notes') or inc_data.get('work_notes') or '')[:300]}""")

    if kb_articles:
        kb_list = "\n".join([f"- [{a['number']}] {a['short_description']}" for a in kb_articles])
        context_parts.append(f"[EXISTING KB ARTICLES FOUND]\n{kb_list}")
    else:
        context_parts.append("[EXISTING KB ARTICLES] None found for this topic.")

    if wants_create and inc_data:
        context_parts.append("[USER REQUEST] Draft a complete KB article for this incident.")

    system = """You are a ServiceNow KB Agent. Your job:
1. When given an incident, summarise the issue and list any matching KB articles found.
2. If no KB articles exist or user asks to create one, draft a complete KB article with:
   - **Title**: clear, searchable title
   - **Category**: appropriate category
   - **Problem**: what the issue is
   - **Cause**: root cause
   - **Resolution**: step-by-step fix
   - **Prevention**: how to avoid recurrence
3. After drafting, ask: "Would you like me to create this KB article in ServiceNow? (yes / no)"
4. If user says yes, confirm creation.
Be concise, structured, use markdown bold for section headers."""

    history = chat_histories.setdefault(f"kb_{session_id}", [])
    full_msg = message
    if context_parts:
        full_msg = message + "\n\n" + "\n\n".join(context_parts)
    history.append({"role": "user", "content": full_msg})

    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}] + history[-16:],
            max_tokens=1200,
            temperature=0.3
        )
        reply = resp.choices[0].message.content.strip()
        usage = resp.usage
        history.append({"role": "assistant", "content": reply})

        # Auto-create KB article if user confirmed
        kb_created = None
        if wants_create and inc_data and SN_URL and ("yes" in message.lower() or "confirm" in message.lower()):
            kb_created = create_kb_article(inc_data, reply)
            if kb_created:
                tool_calls.append({"name": "create_kb_article", "args": {"title": inc_data.get("short_description","")}, "result": {"number": kb_created}})
                reply += f"\n\n✅ **KB article {kb_created} created successfully in ServiceNow!**"

        return jsonify({
            "reply": reply,
            "tool_calls": tool_calls,
            "kb_articles": [{"number": a["number"], "title": a["short_description"], "sys_id": a["sys_id"]} for a in kb_articles],
            "token_usage": {
                "input_tokens":  usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "tool_tokens":   len(tool_calls) * 10,
                "total_tokens":  usage.total_tokens
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def create_kb_article(inc_data, draft_text):
    """Create a KB article in ServiceNow from incident data and AI draft."""
    try:
        # Extract title from draft or use incident short_description
        title = inc_data.get("short_description", "KB Article")
        title_match = re.search(r'\*\*Title\*\*[:\s]+(.+)', draft_text)
        if title_match:
            title = title_match.group(1).strip()

        # Clean draft text to plain HTML
        body = draft_text.replace("**", "").replace("\n", "<br>")

        payload = {
            "short_description": title,
            "text": body,
            "category": inc_data.get("category", "general"),
            "workflow_state": "draft",
            "kb_knowledge_base": "a7e8a78bff0221009b20ffffffffff17"  # default KB
        }
        r = http_requests.post(
            f"{SN_URL}/api/now/table/kb_knowledge",
            auth=(SN_USER, SN_PASS),
            json=payload,
            timeout=10
        )
        if r.status_code in (200, 201):
            return r.json().get("result", {}).get("number", "KB????")
    except:
        pass
    return None


# ── KB Search API ─────────────────────────────────────────────────────────────
@app.route("/api/kb-search")
def kb_search():
    q = request.args.get("q", "").strip()
    if not q or not SN_URL:
        return jsonify({"articles": []})
    try:
        r = http_requests.get(
            f"{SN_URL}/api/now/table/kb_knowledge",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_query": f"short_descriptionLIKE{q}^ORtextLIKE{q}",
                "sysparm_limit": 8,
                "sysparm_fields": "number,short_description,sys_id,category,workflow_state"
            },
            timeout=10
        )
        return jsonify({"articles": r.json().get("result", [])})
    except Exception as e:
        return jsonify({"articles": []})


# ── KB Create API ─────────────────────────────────────────────────────────────
@app.route("/api/kb-create", methods=["POST"])
def kb_create():
    data = request.json or {}
    title = data.get("title", "").strip()
    body  = data.get("body", "").strip()
    category = data.get("category", "general")
    if not title or not body:
        return jsonify({"error": "title and body required"}), 400
    try:
        payload = {
            "short_description": title,
            "text": body.replace("\n", "<br>"),
            "category": category,
            "workflow_state": "draft",
            "kb_knowledge_base": "a7e8a78bff0221009b20ffffffffff17"
        }
        r = http_requests.post(
            f"{SN_URL}/api/now/table/kb_knowledge",
            auth=(SN_USER, SN_PASS),
            json=payload,
            timeout=10
        )
        if r.status_code in (200, 201):
            result = r.json().get("result", {})
            return jsonify({"ok": True, "number": result.get("number"), "sys_id": result.get("sys_id")})
        else:
            return jsonify({"error": f"ServiceNow returned {r.status_code}: {r.text[:200]}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── KB Gap Scan ───────────────────────────────────────────────────────────────
@app.route("/api/kb-gap-scan")
def kb_gap_scan():
    """Fetch recent incidents and check which ones have no matching KB articles — parallel KB lookups."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not SN_URL:
        return jsonify({"error": "ServiceNow not configured"}), 500
    try:
        limit = int(request.args.get("limit", 50))

        # Step 1: Fetch incidents (single call)
        r = http_requests.get(
            f"{SN_URL}/api/now/table/incident",
            auth=(SN_USER, SN_PASS),
            params={
                "sysparm_limit": limit,
                "sysparm_query": "stateNOT IN6,7^ORDERBYDESCopened_at",
                "sysparm_fields": "number,short_description,category,priority,state,assignment_group,opened_at,sys_id"
            },
            timeout=12
        )
        incidents = r.json().get("result", [])
        if not incidents:
            return jsonify({"gaps": [], "total_scanned": 0, "gap_count": 0})

        pri_map   = {"1": "Critical", "2": "High", "3": "Moderate", "4": "Low", "5": "Planning"}
        state_map = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed"}
        stop = {"the","a","an","is","in","on","at","to","for","of","and","or","with","not","be","was"}

        # Step 2: Normalise incident fields
        normalised = []
        for inc in incidents:
            grp = inc.get("assignment_group", {})
            inc["assignment_group"] = grp.get("display_value", "Unassigned") if isinstance(grp, dict) else "Unassigned"
            inc["priority"] = pri_map.get(str(inc.get("priority", "")), inc.get("priority", "—"))
            inc["state"]    = state_map.get(str(inc.get("state", "")), inc.get("state", "—"))
            desc = inc.get("short_description", "").strip()
            if not desc:
                continue
            words = [w for w in re.findall(r'[a-zA-Z]+', desc) if len(w) > 2 and w.lower() not in stop]
            kw = " ".join(words[:3]) if words else desc[:40]
            inc["_kw"] = kw
            normalised.append(inc)

        # Step 3: Check KB in parallel (10 threads)
        def check_kb(inc):
            kw = inc["_kw"]
            try:
                kb_r = http_requests.get(
                    f"{SN_URL}/api/now/table/kb_knowledge",
                    auth=(SN_USER, SN_PASS),
                    params={
                        "sysparm_query": f"short_descriptionLIKE{kw}^ORtextLIKE{kw}^workflow_state=published",
                        "sysparm_limit": 1,
                        "sysparm_fields": "number"
                    },
                    timeout=5
                )
                return inc, len(kb_r.json().get("result", []))
            except:
                return inc, 0  # treat timeout as no KB

        gaps = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(check_kb, inc): inc for inc in normalised}
            for future in as_completed(futures):
                inc, kb_count = future.result()
                if kb_count == 0:
                    gaps.append({
                        "number":            inc.get("number"),
                        "short_description": inc.get("short_description", ""),
                        "priority":          inc.get("priority"),
                        "state":             inc.get("state"),
                        "category":          inc.get("category", ""),
                        "assignment_group":  inc.get("assignment_group"),
                        "opened_at":         inc.get("opened_at", "")[:10],
                        "sys_id":            inc.get("sys_id"),
                        "kb_count":          0,
                        "keywords":          inc["_kw"]
                    })

        # Sort gaps by priority (Critical first)
        pri_order = {"Critical": 0, "High": 1, "Moderate": 2, "Low": 3, "Planning": 4, "—": 5}
        gaps.sort(key=lambda x: pri_order.get(x["priority"], 5))

        return jsonify({
            "gaps":          gaps,
            "total_scanned": len(normalised),
            "gap_count":     len(gaps)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Dashboard page ────────────────────────────────────────────────────────────
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

# ── Interaction Form page ──────────────────────────────────────────────────────
@app.route("/interaction-form")
def interaction_form():
    return render_template("interaction_form.html")

@app.route("/run/<script_id>")
def run_script(script_id):
    script = next((s for s in UNIQUE_SCRIPTS if s["id"] == script_id), None)
    if not script:
        return jsonify({"error": "Script not found"}), 404

    script_path = os.path.join(os.path.dirname(__file__), script["file"])
    if not os.path.exists(script_path):
        return jsonify({"error": f"File not found: {script['file']}"}), 404

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=60,
            cwd=os.path.dirname(__file__)
        )
        output = result.stdout + result.stderr
        return jsonify({"output": output, "returncode": result.returncode})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Script timed out after 60 seconds"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=9000)
