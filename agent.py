import os
import re
import sys
import time
import json
import getpass
import logging
import urllib.parse
import requests
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

# Silence noisy httpx and google_genai logs in the console
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
logging.getLogger("google_genai").setLevel(logging.CRITICAL)
logging.getLogger("google").setLevel(logging.CRITICAL)

try:
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("Oracle EPM Autonomous AI Architect")
except ImportError:
    class _DummyMCP:
        def tool(self):
            def decorator(fn):
                return fn
            return decorator
        def run(self):
            print("MCP package not installed. Running in interactive CLI mode.")
    mcp = _DummyMCP()

# =====================================================================
# 1. CONFIGURATION, RUNTIME PROMPTS (RTPs) & DYNAMIC ENVIRONMENT STATE
# =====================================================================
os.environ.setdefault("GOOGLE_API_KEY", "AQ.Ab8RN6KcgRSnXwvp4DCF7a3gABbKvIdO4PRKPvW09dOl_Y3k7w")

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned_memory.json")

session = requests.Session()
_tool_called_in_turn = False

ACTIVE_APP = None
ALL_APPLICATIONS = []
ACTIVE_CUBES = []
ACTIVE_CUBE = None
_CUBE_ARCHITECTURE_CACHE = {}
_HIERARCHY_CACHE = {}


def _clean_url(raw_url: str) -> str:
    """Strip accidental markdown formatting, quotes, and trailing slashes from URLs."""
    u = (raw_url or "").strip().strip('"').strip("'")
    if "](" in u and u.endswith(")"):
        u = u.split("](", 1)[1][:-1]
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def _mask_password(pw: str) -> str:
    """Mask password showing first 2 and last 2 characters, e.g., 'Ci********26'."""
    if not pw:
        return ""
    if len(pw) <= 4:
        return "*" * len(pw)
    return f"{pw[:2]}{'*' * (len(pw) - 4)}{pw[-2:]}"


def _read_password_masked(prompt_text: str) -> str:
    """
    Read password character-by-character on Windows (msvcrt) or fallback to standard input,
    then display the masked format (e.g., Ci********26).
    """
    try:
        import msvcrt
        sys.stdout.write(prompt_text)
        sys.stdout.flush()
        chars = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                break
            elif ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            elif ch in ("\b", "\x7f"):  # Backspace
                if chars:
                    chars.pop()
                    current_pw = "".join(chars)
                    preview = _mask_password(current_pw)
                    sys.stdout.write(f"\r\033[K{prompt_text}{preview}")
                    sys.stdout.flush()
            else:
                chars.append(ch)
                current_pw = "".join(chars)
                preview = _mask_password(current_pw)
                sys.stdout.write(f"\r\033[K{prompt_text}{preview}")
                sys.stdout.flush()
        print()
        return "".join(chars).strip()
    except Exception:
        pw = getpass.getpass(prompt_text).strip()
        if pw:
            print(f"  │   Masked Password : {_mask_password(pw)}")
        return pw


def prompt_login_rtps(force_prompt: bool = True):
    """Collect EPM Login URL, Login ID (Username), and Password at runtime (RTPs)."""
    curr_url = _clean_url(os.getenv("EPM_URL", ""))
    curr_user = os.getenv("EPM_USERNAME", "").strip()
    curr_pass = os.getenv("EPM_PASSWORD", "").strip()

    if not force_prompt and curr_url and curr_user and curr_pass:
        os.environ["EPM_URL"] = curr_url
        return

    print("\n  ┌─ [Runtime Prompts (RTP): EPM Login Credentials] ────────────")

    while True:
        prompt_lbl = f"  │ Enter EPM Login URL [{curr_url}]: " if curr_url else "  │ Enter EPM Login URL: "
        url_in = input(prompt_lbl).strip()
        final_url = _clean_url(url_in) if url_in else curr_url
        if final_url:
            os.environ["EPM_URL"] = final_url
            break
        print("  │ ! EPM Login URL is required (e.g., https://pod.epm.us6.oraclecloud.com).")

    while True:
        prompt_lbl = f"  │ Enter EPM Login ID  [{curr_user}]: " if curr_user else "  │ Enter EPM Login ID : "
        user_in = input(prompt_lbl).strip()
        final_user = user_in if user_in else curr_user
        if final_user:
            os.environ["EPM_USERNAME"] = final_user
            break
        print("  │ ! EPM Login ID (Username) is required.")

    while True:
        masked_default = f" [{_mask_password(curr_pass)}]" if curr_pass else ""
        prompt_lbl = f"  └─ Enter EPM Password{masked_default}: "
        pass_in = _read_password_masked(prompt_lbl)
        final_pass = pass_in if pass_in else curr_pass
        if final_pass:
            os.environ["EPM_PASSWORD"] = final_pass
            print(f"  ✓ Credentials set (User: {final_user} | Password: {_mask_password(final_pass)})")
            break
        print("  │ ! EPM Password is required.")


def _choose_option(label: str, options: list, preferred: str = None, interactive: bool = True) -> str:
    """Auto-select if 1 option exists, or show numbered menu if >1."""
    if not options:
        if interactive:
            return input(f"  ▸ Enter {label} manually: ").strip()
        return preferred or ""

    if preferred:
        for opt in options:
            if opt.lower() == preferred.lower():
                print(f"  ✓ Selected {label}: '{opt}'")
                return opt

    if len(options) == 1:
        only_item = options[0]
        print(f"  ✓ Only 1 {label} found — automatically selected: '{only_item}'")
        return only_item

    if not interactive:
        print(f"  ✓ Defaulting {label} to '{options[0]}' (Available: {', '.join(options)})")
        return options[0]

    print(f"\n  ┌─ [Choose {label}] ──────────────────────────────────────────")
    for idx, item in enumerate(options, start=1):
        default_tag = " [Default]" if idx == 1 else ""
        print(f"  │ {idx}. {item}{default_tag}")

    while True:
        choice = input(f"  └─ Select {label} (1-{len(options)} or name) [1]: ").strip()
        if not choice:
            selected = options[0]
            print(f"  ✓ Selected {label}: '{selected}'")
            return selected
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(options):
                selected = options[idx - 1]
                print(f"  ✓ Selected {label}: '{selected}'")
                return selected
        for opt in options:
            if opt.lower() == choice.lower():
                print(f"  ✓ Selected {label}: '{opt}'")
                return opt
        print(f"  ! Invalid choice '{choice}'. Please enter 1-{len(options)} or a valid name.")


def _epm_request(
    method: str,
    endpoint: str,
    params: dict = None,
    payload: dict = None,
    user_action: str = None,
    silent: bool = False
) -> str:
    """Execute Basic Auth requests against Oracle Cloud EPM with clean progress output."""
    global _tool_called_in_turn
    _tool_called_in_turn = True

    base_url = _clean_url(os.getenv("EPM_URL", ""))
    username = os.getenv("EPM_USERNAME", "")
    password = os.getenv("EPM_PASSWORD", "")

    if not base_url or not username or not password:
        return json.dumps({
            "error": "Missing credentials. Run 'login' to provide EPM_URL, EPM_USERNAME, and EPM_PASSWORD."
        })

    clean_endpoint = "/" + endpoint.lstrip("/")
    url = f"{base_url}{clean_endpoint}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    if not silent:
        label = user_action or f"{method.upper()} {clean_endpoint}"
        print(f"  ▸ {label}...", end="", flush=True)

    t0 = time.perf_counter()
    try:
        response = session.request(
            method=method.upper(),
            url=url,
            headers=headers,
            auth=HTTPBasicAuth(username, password),
            params=params,
            json=payload,
            timeout=60
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if not silent:
            if response.ok:
                print(f" ✓ Done ({elapsed_ms:.0f} ms)")
            else:
                short_err = ""
                try:
                    err_j = response.json()
                    short_err = err_j.get("detail") or err_j.get("message") or err_j.get("title") or ""
                    if len(short_err) > 110:
                        short_err = short_err[:110] + "..."
                except Exception:
                    pass
                suffix = f" - {short_err}" if short_err else ""
                print(f" ✗ HTTP {response.status_code} ({elapsed_ms:.0f} ms){suffix}")

        try:
            data = response.json()
            if method.upper() == "GET" and isinstance(data, dict) and "rules" in data and "variables" in data:
                var_len = len(data.get("variables", []))
                data["variables"] = data["variables"][:1]
                data["_variables_note"] = f"Trimmed {var_len} variables so 'rules' schema is visible."
                if len(data.get("rules", [])) > 5:
                    data["rules"] = [
                        {
                            "name": r.get("name"),
                            "planType": r.get("planType"),
                            "script": r.get("script")
                        }
                        for r in data["rules"][:10]
                    ]
            out_text = json.dumps(data, indent=2)
        except Exception:
            out_text = response.text if response.text else json.dumps({
                "status": response.status_code,
                "message": f"HTTP {response.status_code} {response.reason} (Success / No Content)"
            })

        response.raise_for_status()
        return out_text

    except requests.exceptions.HTTPError as http_err:
        return json.dumps({
            "error": f"HTTP {response.status_code}",
            "url": url,
            "details": (response.text or str(http_err))[:800]
        })
    except Exception as err:
        if not silent:
            print(f" ✗ Failed ({err})")
        return json.dumps({"error": str(err), "url": url})


def _extract_cube_names(raw_json_str: str) -> list:
    """Safely parse string cube/planType names from any Oracle EPM response format."""
    cubes = []
    try:
        data = json.loads(raw_json_str)
        if isinstance(data, dict):
            for key in ("items", "planTypes", "cubes"):
                val = data.get(key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str) and not item.isdigit():
                            cubes.append(item.strip())
                        elif isinstance(item, dict):
                            for k in ("name", "planType", "cubeName", "planTypeName"):
                                candidate = item.get(k)
                                if isinstance(candidate, str) and candidate.strip() and not candidate.strip().isdigit():
                                    cubes.append(candidate.strip())
                                    break
                elif isinstance(val, dict):
                    for k in val.keys():
                        if isinstance(k, str) and not k.isdigit():
                            cubes.append(k.strip())

            if not cubes and isinstance(data.get("dimensions"), list):
                for dim in data["dimensions"]:
                    pt_map = dim.get("planTypes") or dim.get("validForPlanTypes")
                    if isinstance(pt_map, dict):
                        for k in pt_map.keys():
                            if isinstance(k, str) and not k.isdigit():
                                cubes.append(k.strip())
                    elif isinstance(pt_map, list):
                        for pt in pt_map:
                            if isinstance(pt, str) and not pt.isdigit():
                                cubes.append(pt.strip())
                            elif isinstance(pt, dict) and isinstance(pt.get("name"), str):
                                cubes.append(pt["name"].strip())
    except Exception:
        pass

    return list(dict.fromkeys(str(c) for c in cubes if c and not str(c).isdigit()))


def discover_live_environment(
    preferred_app: str = None,
    preferred_cube: str = None,
    interactive_select: bool = False
) -> tuple:
    """Auto-discover all applications on the EPM pod and all cubes in the active application."""
    global ACTIVE_APP, ALL_APPLICATIONS, ACTIVE_CUBES, ACTIVE_CUBE

    while True:
        print("  ▸ Connecting to Oracle Cloud EPM & discovering applications...", end="", flush=True)
        t0 = time.perf_counter()
        raw_apps = _epm_request("GET", "/HyperionPlanning/rest/v3/applications", silent=True)
        elapsed_app = (time.perf_counter() - t0) * 1000

        try:
            apps_data = json.loads(raw_apps)
        except Exception:
            apps_data = {}

        if isinstance(apps_data, dict) and "error" in apps_data:
            print(f" ✗ Connection/Auth failed: {apps_data.get('error')} ({elapsed_app:.0f} ms)")
            if interactive_select:
                print("  ! Please verify your Login URL, ID, and Password.")
                prompt_login_rtps(force_prompt=True)
                continue
            ALL_APPLICATIONS = []
            break

        app_items = apps_data.get("items", []) if isinstance(apps_data, dict) else []
        ALL_APPLICATIONS = [
            (a if isinstance(a, str) else a.get("name"))
            for a in app_items
            if (isinstance(a, str) or a.get("name"))
        ]
        print(f" ✓ Found {len(ALL_APPLICATIONS)} Application(s): {', '.join(ALL_APPLICATIONS)} ({elapsed_app:.0f} ms)")
        break

    ACTIVE_APP = _choose_option(
        label="EPM Application",
        options=ALL_APPLICATIONS,
        preferred=preferred_app,
        interactive=interactive_select
    )

    print(f"  ▸ Discovering cubes (plan types) in '{ACTIVE_APP}'...", end="", flush=True)
    t1 = time.perf_counter()
    raw_cubes = _epm_request("GET", f"/HyperionPlanning/rest/v3/applications/{ACTIVE_APP}/plantypes", silent=True)
    ACTIVE_CUBES = _extract_cube_names(raw_cubes)

    if not ACTIVE_CUBES:
        raw_sum = _epm_request("GET", f"/HyperionPlanning/rest/v3/applications/{ACTIVE_APP}/summary", silent=True)
        ACTIVE_CUBES = _extract_cube_names(raw_sum)

    elapsed_cubes = (time.perf_counter() - t1) * 1000
    if ACTIVE_CUBES:
        print(f" ✓ Found {len(ACTIVE_CUBES)} Cube(s): {', '.join(str(c) for c in ACTIVE_CUBES)} ({elapsed_cubes:.0f} ms)")
        ACTIVE_CUBE = _choose_option(
            label=f"Cube (Plan Type) in '{ACTIVE_APP}'",
            options=ACTIVE_CUBES,
            preferred=preferred_cube,
            interactive=interactive_select
        )
    else:
        print(f" ! Cubes will be discovered dynamically per request ({elapsed_cubes:.0f} ms)")
        ACTIVE_CUBE = preferred_cube or None

    return ACTIVE_APP, ACTIVE_CUBES, ACTIVE_CUBE


# =====================================================================
# 2. COMPLETE REGISTRY OF ALL 141 ORACLE EPM REST ENDPOINTS
# =====================================================================
BASE_V3 = "/HyperionPlanning/rest/v3/applications"
BASE_V2 = "/HyperionPlanning/rest/epm/v2/applications"

ENDPOINTS_141 = {
    1: ("GET", f"{BASE_V3}", "List applications"),
    2: ("GET", f"{BASE_V3}/{{appname}}", "Get application"),
    3: ("DELETE", f"{BASE_V3}/{{appname}}", "Delete application"),
    4: ("POST", f"{BASE_V3}/{{appname}}", "Update application metadata"),
    5: ("POST", f"{BASE_V3}/{{appname}}/formdefinitions", "Create form definition"),
    6: ("GET", f"{BASE_V3}/{{appname}}/formdefinitions/{{idorname}}", "Get form definition"),
    7: ("PATCH", f"{BASE_V3}/{{appname}}/formdefinitions/{{idorname}}", "Update form definition"),
    8: ("PUT", f"{BASE_V3}/{{appname}}/formdefinitions/{{idorname}}", "Replace form definition"),
    9: ("DELETE", f"{BASE_V3}/{{appname}}/formdefinitions/{{idorname}}", "Delete form definition"),
    10: ("GET", f"{BASE_V3}/{{appname}}/forms/{{idorname}}/data", "Export form data"),
    11: ("POST", f"{BASE_V3}/{{application}}/forms/{{idorname}}/exportdata", "Export form data and run load rules"),
    12: ("POST", f"{BASE_V3}/{{application}}/forms/{{idorname}}/importdata", "Import form data and run save rules"),
    13: ("GET", f"{BASE_V3}/{{appname}}/objects", "Query application objects"),
    14: ("POST", f"{BASE_V3}/{{appname}}/dashboarddefinitions", "Create Dashboard definition"),
    15: ("GET", f"{BASE_V3}/{{appname}}/dashboarddefinitions/{{idorname}}", "Get Dashboard definition"),
    16: ("PATCH", f"{BASE_V3}/{{appname}}/dashboarddefinitions/{{idorname}}", "Update Dashboard definition"),
    17: ("PUT", f"{BASE_V3}/{{appname}}/dashboarddefinitions/{{idorname}}", "Replace Dashboard definition"),
    18: ("DELETE", f"{BASE_V3}/{{appname}}/dashboarddefinitions/{{idorname}}", "Delete Dashboard definition"),
    19: ("POST", f"{BASE_V3}/{{appname}}/dashboards/{{idorname}}/exportdata", "Export Dashboard component data"),
    20: ("POST", f"{BASE_V3}/{{appname}}/dashboards/{{idorname}}/importdata", "Import Dashboard component data"),
    21: ("PATCH", f"{BASE_V3}/{{appname}}/dashboarddefinitions/{{idorname}}/components/{{componentkey}}", "Update Dashboard component"),
    22: ("GET", f"{BASE_V3}/{{application}}/summary", "Get an application summary"),
    23: ("GET", f"{BASE_V3}/{{application}}/businessconcepts", "List Business Concepts"),
    24: ("POST", f"{BASE_V3}/{{application}}/businessconcepts", "Create a Business Concept"),
    25: ("POST", f"{BASE_V3}/{{application}}/businessconcepts/action/search", "Search Business Concepts"),
    26: ("POST", f"{BASE_V3}/{{application}}/businessconcepts/action/resolve", "Resolve Business Concepts"),
    27: ("POST", f"{BASE_V3}/{{application}}/businessconcepts/action/coverage", "Audit Business Concept coverage"),
    28: ("POST", f"{BASE_V3}/{{application}}/businessconcepts/action/discover", "Discover Business Concept objects"),
    29: ("GET", f"{BASE_V3}/{{application}}/businessconceptassociations", "Query Business Concept associations"),
    30: ("GET", f"{BASE_V3}/{{application}}/businessconcepts/{{conceptIdOrCode}}", "Get a Business Concept"),
    31: ("PUT", f"{BASE_V3}/{{application}}/businessconcepts/{{conceptIdOrCode}}", "Update a Business Concept"),
    32: ("POST", f"{BASE_V3}/{{application}}/businessconcepts/{{conceptIdOrCode}}", "Partially update a Business Concept"),
    33: ("DELETE", f"{BASE_V3}/{{application}}/businessconcepts/{{conceptIdOrCode}}", "Delete a Business Concept"),
    34: ("GET", f"{BASE_V3}/{{application}}/calcmanager/rules", "List Calculation Manager rule packages"),
    35: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rules", "Create a Calculation Manager rule package"),
    36: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rules/validate", "Validate a rule package payload"),
    37: ("GET", f"{BASE_V3}/{{application}}/calcmanager/rules/{{idorname}}", "Retrieve a Calculation Manager rule package"),
    38: ("PUT", f"{BASE_V3}/{{application}}/calcmanager/rules/{{idorname}}", "Replace a Calculation Manager rule package"),
    39: ("DELETE", f"{BASE_V3}/{{application}}/calcmanager/rules/{{idorname}}", "Delete a Calculation Manager rule"),
    40: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rules/{{idorname}}/validate", "Validate an existing rule package"),
    41: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rules/{{idorname}}/deploy", "Deploy an existing rule package"),
    42: ("GET", f"{BASE_V3}/{{application}}/calcmanager/rulesets", "List Calculation Manager ruleset packages"),
    43: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rulesets", "Create a Calculation Manager ruleset package"),
    44: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rulesets/validate", "Validate a ruleset package payload"),
    45: ("GET", f"{BASE_V3}/{{application}}/calcmanager/rulesets/{{idorname}}", "Retrieve a Calculation Manager ruleset"),
    46: ("PUT", f"{BASE_V3}/{{application}}/calcmanager/rulesets/{{idorname}}", "Replace a Calculation Manager ruleset"),
    47: ("DELETE", f"{BASE_V3}/{{application}}/calcmanager/rulesets/{{idorname}}", "Delete a Calculation Manager ruleset"),
    48: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rulesets/{{idorname}}/validate", "Validate an existing ruleset"),
    49: ("POST", f"{BASE_V3}/{{application}}/calcmanager/rulesets/{{idorname}}/deploy", "Deploy an existing ruleset"),
    50: ("POST", f"{BASE_V3}", "Create a Planning application"),
    51: ("DELETE", f"{BASE_V3}/{{application}}", "Delete the specified Planning application"),
    52: ("GET", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/dimensions/{{dimname}}", "Get dimension and child members"),
    53: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/dimensions/{{dimname}}/members/batch", "Batch member operations"),
    54: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/dimensions/{{dimname}}/memberselectionmembers", "Evaluate member selection"),
    55: ("PATCH", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/dimensions/{{id}}", "Update dimension for a plan type"),
    56: ("POST", f"{BASE_V3}/{{appname}}/dimensions", "Create a dimension at the application level"),
    57: ("PATCH", f"{BASE_V3}/{{appname}}/dimensions/{{id}}", "Update a dimension at the application level"),
    58: ("POST", f"{BASE_V3}/{{appname}}/dimensions/{{dimname}}/members", "Create a dynamic child member"),
    59: ("GET", f"{BASE_V3}/{{appname}}/dimensions/{{dimname}}/members/{{membername}}", "Get a member within a dimension"),
    60: ("GET", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/dimensions", "List dimensions for a plan type"),
    61: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/dimensions", "Create a dimension for a plan type"),
    62: ("POST", f"{BASE_V3}/{{appname}}/dimensions", "Create a dimension at application level"),
    63: ("POST", f"{BASE_V3}/{{appname}}/members/action/search", "Search secured members across dimensions"),
    64: ("POST", f"{BASE_V3}/{{application}}/insights", "Get insights for a Planning cube data slice"),
    65: ("GET", f"{BASE_V3}/{{application}}/insights/{{insightId}}", "Get Insight details"),
    66: ("POST", f"{BASE_V3}/{{application}}/insights/{{insightId}}/action/analyze", "Execute Insight Analysis"),
    67: ("POST", f"{BASE_V3}/{{application}}/insights/summary", "Get an insight summary"),
    68: ("POST", f"{BASE_V3}/{{application}}/ipm/calendars", "Create an IPM calendar"),
    69: ("GET", f"{BASE_V3}/{{application}}/ipm/calendars/{{calendarUuid}}", "Get an IPM calendar by UUID"),
    70: ("POST", f"{BASE_V3}/{{application}}/jobs", "Execute an asynchronous Planning job"),
    71: ("GET", f"{BASE_V3}/{{application}}/jobdefinitions", "List available job definitions"),
    72: ("GET", f"{BASE_V3}/{{application}}/jobs/{{jobIdentifier}}", "Get job execution status"),
    73: ("GET", f"{BASE_V3}/{{application}}/jobs/{{jobIdentifier}}/details", "Get job execution details"),
    74: ("GET", f"{BASE_V3}/{{application}}/jobs/{{jobIdentifier}}/childjobs/{{childJobIdentifier}}/details", "Get child-job details"),
    75: ("GET", f"{BASE_V3}/{{application}}/objects/{{id}}/businessconceptassociations", "List object Business Concept associations"),
    76: ("PUT", f"{BASE_V3}/{{application}}/objects/{{id}}/businessconceptassociations", "Replace object Business Concept associations"),
    77: ("POST", f"{BASE_V3}/{{application}}/objects/{{id}}/businessconceptassociations", "Add object Business Concept associations"),
    78: ("POST", f"{BASE_V3}/{{application}}/objects/{{id}}/businessconceptassociations/action/delete", "Delete object Business Concept associations"),
    79: ("GET", f"{BASE_V3}/{{application}}/objects", "Search objects"),
    80: ("POST", f"{BASE_V3}/{{application}}/planningunits", "List planning units"),
    81: ("GET", f"{BASE_V3}/{{application}}/planningunit/{{puIdentifier}}", "Get a planning unit"),
    82: ("GET", f"{BASE_V3}/{{application}}/planningunits/{{puIdentifier}}/historyandannotations", "Get planning-unit history"),
    83: ("GET", f"{BASE_V3}/{{application}}/planningunits/{{puIdentifier}}/promotionpath", "Get planning-unit promotion path"),
    84: ("POST", f"{BASE_V3}/{{application}}/planningunits/{{puhIdentifier}}/actions", "Apply an approval action"),
    85: ("POST", f"{BASE_V3}/{{application}}/planningunits/{{puhIdentifier}}/availableactions", "Get available approval actions"),
    86: ("GET", f"{BASE_V3}/{{appname}}/plantypes", "Get plan types for the specified application"),
    87: ("POST", f"{BASE_V3}/{{appname}}/plantypes", "Create a plan type in the specified application"),
    88: ("GET", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}", "Get a single plan type"),
    89: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/exportdata", "Export ad hoc data slice"),
    90: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/exportdataslice", "Export a data slice"),
    91: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/importdataslice", "Import a data slice"),
    92: ("POST", f"{BASE_V3}/{{appname}}/plantypes/{{plantype}}/exportmdxdata", "Export custom MDX query data"),
    93: ("POST", f"{BASE_V3}/{{application}}/plantypes/{{plantype}}/cleardataslice", "Clear a data slice"),
    94: ("POST", f"{BASE_V3}/{{application}}/semanticfoundation/pov", "Resolve semantic point of view"),
    95: ("GET", f"{BASE_V3}/{{application}}/semanticfoundation/context", "Retrieve live Semantic Foundation context"),
    96: ("POST", f"{BASE_V3}/{{application}}/semanticfoundation/forms/action/findbyslices", "Find readable forms by member slices"),
    97: ("GET", f"{BASE_V3}/{{application}}/semanticcontextintersections", "List Semantic Context Intersections"),
    98: ("POST", f"{BASE_V3}/{{application}}/semanticcontextintersections", "Create a Semantic Context Intersection"),
    99: ("GET", f"{BASE_V3}/{{application}}/semanticcontextintersections/{{intersectionId}}", "Get a Semantic Context Intersection"),
    100: ("PUT", f"{BASE_V3}/{{application}}/semanticcontextintersections/{{intersectionId}}", "Replace a Semantic Context Intersection"),
    101: ("DELETE", f"{BASE_V3}/{{application}}/semanticcontextintersections/{{intersectionId}}", "Delete a Semantic Context Intersection"),
    102: ("POST", f"{BASE_V3}/{{application}}/semanticfoundation/workbooks/action/export", "Export governed semantic metadata"),
    103: ("POST", f"{BASE_V3}/{{application}}/semanticfoundation/workbooks/action/import", "Import governed semantic metadata"),
    104: ("GET", f"{BASE_V2}/{{application}}/connections", "Retrieve all service connections"),
    105: ("POST", f"{BASE_V2}/{{application}}/connections", "Create a service connection"),
    106: ("GET", f"{BASE_V2}/{{application}}/connections/{{connectionRef}}", "Retrieve a service connection"),
    107: ("PUT", f"{BASE_V2}/{{application}}/connections/{{connectionRef}}", "Update a service connection"),
    108: ("DELETE", f"{BASE_V2}/{{application}}/connections/{{connectionRef}}", "Delete a service connection"),
    109: ("GET", f"{BASE_V3}/{{application}}/substitutionvariables", "List application substitution variables"),
    110: ("POST", f"{BASE_V3}/{{application}}/substitutionvariables", "Create or update application substitution variables"),
    111: ("GET", f"{BASE_V3}/{{application}}/substitutionvariables/{{name}}", "Get an application substitution variable"),
    112: ("DELETE", f"{BASE_V3}/{{application}}/substitutionvariables/{{name}}", "Delete an application substitution variable"),
    113: ("GET", f"{BASE_V3}/{{application}}/plantypes/{{plantype}}/substitutionvariables", "List cube substitution variables"),
    114: ("POST", f"{BASE_V3}/{{application}}/plantypes/{{plantype}}/substitutionvariables", "Create or update cube substitution variables"),
    115: ("GET", f"{BASE_V3}/{{application}}/plantypes/{{plantype}}/substitutionvariables/{{name}}", "Get a cube substitution variable"),
    116: ("DELETE", f"{BASE_V3}/{{application}}/plantypes/{{plantype}}/substitutionvariables/{{name}}", "Delete a cube substitution variable"),
    117: ("GET", f"{BASE_V3}/{{appname}}/me/preferences", "Get user preferences for the current user"),
    118: ("POST", f"{BASE_V3}/{{appname}}/me/preferences", "Save user preferences for the current user"),
    119: ("GET", f"{BASE_V3}/{{application}}/userpreferences", "Get effective user formatting preferences"),
    120: ("GET", f"{BASE_V3}/{{application}}/me/stickypreferences", "Get current user sticky preferences"),
    121: ("POST", f"{BASE_V3}/{{application}}/me/stickypreferences", "Update current user sticky preferences"),
    122: ("GET", f"{BASE_V3}/{{application}}/users/{{username}}/stickypreferences", "Get named user sticky preferences"),
    123: ("POST", f"{BASE_V3}/{{application}}/users/{{username}}/stickypreferences", "Update named user sticky preferences"),
    124: ("GET", f"{BASE_V3}/{{application}}/users/{{username}}/preferences", "Get a named user's preferences"),
    125: ("POST", f"{BASE_V3}/{{application}}/users/{{username}}/preferences", "Update a named user's preferences"),
    126: ("GET", f"{BASE_V3}/{{application}}/uservariables", "Get user variable definitions"),
    127: ("POST", f"{BASE_V3}/{{application}}/uservariables", "Create or update user variable definitions"),
    128: ("GET", f"{BASE_V3}/{{application}}/uservariables/{{id}}", "Get a user variable definition by id"),
    129: ("DELETE", f"{BASE_V3}/{{application}}/uservariables/{{id}}", "Delete a user variable definition by id"),
    130: ("GET", f"{BASE_V3}/{{application}}/uservariablevalues", "List user variable values"),
    131: ("POST", f"{BASE_V3}/{{application}}/uservariablevalues", "Update user variable values"),
    132: ("GET", f"{BASE_V3}/{{application}}/uservariablevalues/{{variableName}}", "Get values for a user variable"),
    133: ("GET", f"{BASE_V3}/{{application}}/me", "Get current user profile"),
    134: ("GET", f"{BASE_V3}/{{application}}/me/pov", "Retrieve current user POV"),
    135: ("POST", f"{BASE_V3}/{{application}}/me/pov", "Merge current user POV"),
    136: ("PUT", f"{BASE_V3}/{{application}}/me/pov", "Replace current user POV"),
    137: ("GET", f"{BASE_V3}/{{application}}/users/{{username}}", "Get a named user profile"),
    138: ("GET", f"{BASE_V3}/{{application}}/users/{{username}}/pov", "Retrieve named user POV"),
    139: ("POST", f"{BASE_V3}/{{application}}/users/{{username}}/pov", "Merge named user POV"),
    140: ("PUT", f"{BASE_V3}/{{application}}/users/{{username}}/pov", "Replace named user POV"),
    141: ("GET", f"{BASE_V3}/{{application}}/users/{{userId}}/photo", "Get a planning-unit owner photo"),
}


# =====================================================================
# 3. CORE METADATA, HIERARCHY & MEMBER MANAGEMENT TOOLS
# =====================================================================
def _fetch_cached_hierarchy(app: str, cube: str, dimension_name: str, force_refresh: bool = False) -> dict:
    """Fetch dimension hierarchy once and cache in memory to avoid redundant EPM calls."""
    key = f"{app.upper()}:{cube.upper()}:{dimension_name.upper()}"
    if not force_refresh and key in _HIERARCHY_CACHE:
        return _HIERARCHY_CACHE[key]

    raw_hier = _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes/{cube}/dimensions/{urllib.parse.quote(dimension_name, safe='')}",
        params={"fields": "id,name,alias,path,children,dataStorage", "aliasTableName": "Default"},
        user_action=f"Reading '{dimension_name}' hierarchy in '{cube}'"
    )
    try:
        h_json = json.loads(raw_hier)
        if isinstance(h_json, dict) and "error" not in h_json:
            _HIERARCHY_CACHE[key] = h_json
        return h_json
    except Exception:
        return {}


def _prune_tree_for_llm(node: dict, max_depth: int = 2, current_depth: int = 0) -> dict:
    """Prune deep hierarchy trees so a single tool response never exceeds Gemini TPM limits."""
    children = node.get("children") or []
    summary = {
        "name": node.get("name"),
        "alias": node.get("alias"),
        "dataStorage": node.get("dataStorage"),
        "childCount": len(children)
    }
    if current_depth < max_depth and children:
        summary["children"] = [
            _prune_tree_for_llm(c, max_depth, current_depth + 1)
            for c in children[:25]
        ]
        if len(children) > 25:
            summary["note"] = f"Showing first 25 of {len(children)} children. Use search_dimension_members to drill deeper."
    elif children:
        summary["sampleChildren"] = [c.get("name") for c in children[:10]]
    return summary


@mcp.tool()
def get_applications() -> str:
    """Discover all Oracle EPM applications available on this instance (Endpoint #1)."""
    return _epm_request(
        "GET",
        "/HyperionPlanning/rest/v3/applications",
        user_action="Discovering available EPM applications on this instance"
    )


@mcp.tool()
def switch_active_application(application_name: str, preferred_cube: str = "") -> str:
    """Switch the active EPM application dynamically and discover/select its cubes."""
    app, cubes, active_cube = discover_live_environment(
        preferred_app=application_name,
        preferred_cube=preferred_cube or None,
        interactive_select=False
    )
    return json.dumps({
        "active_application": app,
        "discovered_cubes": cubes,
        "active_cube": active_cube,
        "message": f"Switched active application to '{app}' (Active Cube: '{active_cube}')."
    }, indent=2)


@mcp.tool()
def switch_active_cube(cube_name: str) -> str:
    """Switch the active cube (plan type) within the current EPM application."""
    global ACTIVE_CUBE
    if ACTIVE_CUBES and cube_name in ACTIVE_CUBES:
        ACTIVE_CUBE = cube_name
    else:
        ACTIVE_CUBE = _choose_option(
            label=f"Cube in '{ACTIVE_APP}'",
            options=ACTIVE_CUBES,
            preferred=cube_name,
            interactive=False
        )
    return json.dumps({
        "active_application": ACTIVE_APP,
        "active_cube": ACTIVE_CUBE,
        "available_cubes": ACTIVE_CUBES
    }, indent=2)


@mcp.tool()
def get_cubes(application: str = "") -> str:
    """Retrieve all plan types (cubes) for the specified EPM application (Endpoint #86)."""
    global ACTIVE_CUBES, ACTIVE_CUBE
    app = application or ACTIVE_APP
    res = _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes",
        user_action=f"Listing all cubes (plan types) in application '{app}'"
    )
    extracted = _extract_cube_names(res)
    if extracted and app == ACTIVE_APP:
        ACTIVE_CUBES = extracted
        if not ACTIVE_CUBE or ACTIVE_CUBE not in ACTIVE_CUBES:
            ACTIVE_CUBE = ACTIVE_CUBES[0]
    return res


@mcp.tool()
def get_dimensions_for_cube(application: str = "", cube_name: str = "") -> str:
    """Retrieve all dimensions and their Performance Settings (Dense/Sparse, Order) for a cube (Endpoint #60)."""
    app = application or ACTIVE_APP
    cube = cube_name or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")
    return _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes/{cube}/dimensions",
        params={"limit": -1},
        user_action=f"Reading dimensions & performance settings for cube '{cube}' in '{app}'"
    )


@mcp.tool()
def get_dimension_hierarchy(application: str, cube_name: str, dimension_name: str) -> str:
    """
    Retrieve the top-level member hierarchy, aliases, and dataStorage for a specific dimension within a cube (Endpoint #52).
    For deep member searches, prefer `search_dimension_members`.
    """
    app = application or ACTIVE_APP
    cube = cube_name or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")
    h_json = _fetch_cached_hierarchy(app, cube, dimension_name)
    if not h_json or "error" in h_json:
        return json.dumps(h_json or {"error": f"Unable to load hierarchy for {dimension_name}"})
    return json.dumps(_prune_tree_for_llm(h_json, max_depth=2), indent=2)


@mcp.tool()
def get_member_properties(application: str, dimension_name: str, member_name: str) -> str:
    """Fetch detailed properties (dataStorage, planType associations, formula, etc.) of a single member (Endpoint #59)."""
    app = application or ACTIVE_APP
    encoded_mbr = urllib.parse.quote(member_name, safe="")
    return _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/dimensions/{urllib.parse.quote(dimension_name, safe='')}/members/{encoded_mbr}",
        user_action=f"Inspecting properties of member '{member_name}' in dimension '{dimension_name}'"
    )


def _search_hierarchy_tree(node: dict, keyword: str, parent_name: str = None) -> list:
    """Recursively search a dimension hierarchy tree for members matching keyword in name or alias."""
    matches = []
    name = str(node.get("name") or "")
    alias = str(node.get("alias") or "")
    storage = str(node.get("dataStorage") or "")
    children = node.get("children") or []
    is_parent = len(children) > 0

    kw = keyword.lower().strip()
    if not kw or kw in name.lower() or kw in alias.lower():
        matches.append({
            "member": name,
            "alias": alias,
            "parent": parent_name,
            "dataStorage": storage,
            "isParent": is_parent,
            "childCount": len(children),
            "sampleChildren": [c.get("name") for c in children[:8]]
        })

    for child in children:
        matches.extend(_search_hierarchy_tree(child, keyword, parent_name=name))
    return matches


@mcp.tool()
def search_dimension_members(
    cube_name: str = "",
    dimension_name: str = "Account",
    search_keyword: str = "",
    application: str = ""
) -> str:
    """
    Search a dimension hierarchy (such as 'Account', 'Scenario', 'Version', 'Service', etc.) in a cube
    for real member names and aliases matching `search_keyword` (e.g., 'Revenue', 'Expense', 'Computer', 'Budget').
    If `search_keyword` is empty, returns the top-level hierarchy branches of the dimension.
    """
    app = application or ACTIVE_APP
    cube = cube_name or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")
    print(f"  ▸ Searching '{dimension_name}' hierarchy in '{cube}' for '{search_keyword or 'top-level rollups'}'...")
    h_json = _fetch_cached_hierarchy(app, cube, dimension_name)
    if not h_json or "error" in h_json:
        return json.dumps({"error": f"Failed to load hierarchy for {dimension_name}"})

    matches = _search_hierarchy_tree(h_json, search_keyword)
    top_branches = [
        {
            "member": c.get("name"),
            "alias": c.get("alias"),
            "dataStorage": c.get("dataStorage"),
            "isParent": bool(c.get("children")),
            "sampleChildren": [gc.get("name") for gc in (c.get("children") or [])[:6]]
        }
        for c in (h_json.get("children") or [])[:15]
    ]

    return json.dumps({
        "cube": cube,
        "dimension": dimension_name,
        "search_keyword": search_keyword,
        "match_count": len(matches),
        "matches": matches[:25],
        "top_level_branches": top_branches
    }, indent=2)


@mcp.tool()
def create_or_update_members_batch(
    dimension_name: str,
    members_json: str,
    application: str = "",
    cube_name: str = ""
) -> str:
    """
    Create, update, or move (re-parent) one or multiple dimension members in a single batch call (Endpoint #53).
    - To CREATE or MOVE a member under a parent (including root `"Account"`), specify `"parent"`.
    - To DELETE a member via Endpoint #53, pass `"operation": "delete member"` or use `delete_dimension_members`.
    """
    app = application or ACTIVE_APP
    cube = cube_name or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")

    try:
        parsed = json.loads(members_json)
        if isinstance(parsed, dict) and "items" in parsed:
            items = parsed["items"]
        elif isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            items = [parsed]
        else:
            return json.dumps({"error": "members_json must be a JSON array of member objects."})
    except Exception as e:
        return json.dumps({"error": f"Invalid JSON in members_json: {e}"})

    payload = {"items": items}
    res = _epm_request(
        "POST",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes/{cube}/dimensions/{urllib.parse.quote(dimension_name, safe='')}/members/batch",
        payload=payload,
        user_action=f"Batch creating/updating {len(items)} member(s) in '{dimension_name}' ({cube})"
    )

    _HIERARCHY_CACHE.pop(f"{app.upper()}:{cube.upper()}:{dimension_name.upper()}", None)
    return res


@mcp.tool()
def delete_dimension_members(
    dimension_name: str,
    member_names_json: str,
    application: str = "",
    cube_name: str = ""
) -> str:
    """
    Delete one or more dimension members in Oracle Cloud EPM using Endpoint #53's supported delete operations
    (`"delete member"` / `"delete descendants"` / `"delete children"`).
    Pass `member_names_json` as a JSON list of member names (children first, or parent with delete descendants).
    """
    app = application or ACTIVE_APP
    cube = cube_name or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")

    try:
        names = json.loads(member_names_json)
        if isinstance(names, str):
            names = [names]
    except Exception:
        names = [m.strip() for m in member_names_json.split(",") if m.strip()]

    enc_dim = urllib.parse.quote(dimension_name, safe="")
    items = [{"name": mbr, "operation": "delete member"} for mbr in names]
    res = _epm_request(
        "POST",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes/{cube}/dimensions/{enc_dim}/members/batch",
        payload={"items": items},
        user_action=f"Deleting {len(items)} member(s) from '{dimension_name}' ({cube})"
    )

    _HIERARCHY_CACHE.pop(f"{app.upper()}:{cube.upper()}:{dimension_name.upper()}", None)
    return res


# =====================================================================
# 4. DEDICATED CUBE REFRESH & DATA FORM TOOLS (ZERO-GUESSING)
# =====================================================================
@mcp.tool()
def refresh_cube(application: str = "", wait_for_completion: bool = True) -> str:
    """
    Execute an Oracle EPM Database Refresh (`CUBE_REFRESH`) job in a single call and optionally poll until finished.
    ALWAYS use this tool when the user asks for a DB Refresh / Cube Refresh!
    """
    app = application or ACTIVE_APP

    job_name = "RefreshCube"
    raw_defs = _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/jobdefinitions",
        silent=True
    )
    try:
        defs_json = json.loads(raw_defs)
        for jd in defs_json.get("items", []):
            if str(jd.get("jobType", "")).upper() in ("CUBE_REFRESH", "CUBEREFRESH", "REFRESH_CUBE"):
                job_name = jd.get("jobName") or job_name
                break
    except Exception:
        pass

    payload = {
        "jobType": "CUBE_REFRESH",
        "jobName": job_name
    }
    res_str = _epm_request(
        "POST",
        f"/HyperionPlanning/rest/v3/applications/{app}/jobs",
        payload=payload,
        user_action=f"Triggering Database Refresh (CUBE_REFRESH: '{job_name}') in '{app}'"
    )

    try:
        job_data = json.loads(res_str)
    except Exception:
        return res_str

    job_id = job_data.get("jobId")
    status = job_data.get("descriptiveStatus") or job_data.get("status")

    if wait_for_completion and job_id and status in ("Processing", -1, "-1"):
        print(f"  ▸ Waiting for Database Refresh Job #{job_id} to complete...", end="", flush=True)
        for _ in range(15):
            time.sleep(2)
            poll_raw = _epm_request(
                "GET",
                f"/HyperionPlanning/rest/v3/applications/{app}/jobs/{job_id}",
                silent=True
            )
            try:
                poll_j = json.loads(poll_raw)
                desc_status = str(poll_j.get("descriptiveStatus") or "")
                if desc_status.lower() not in ("processing", "running", "in progress"):
                    print(f" ✓ {desc_status}")
                    return json.dumps(poll_j, indent=2)
            except Exception:
                break
        print(" (Still running in background)")

    return res_str


@mcp.tool()
def list_forms(search_keyword: str = "", application: str = "") -> str:
    """
    List existing Data Forms in the active Oracle EPM application using the required `?q=` parameter on `/objects`.
    NEVER guess `/formdefinitions` GET URLs — ALWAYS call `list_forms` to find form names!
    """
    app = application or ACTIVE_APP
    found_forms = []

    query_variants = [
        {"q": json.dumps({"type": "FORM"})},
        {"q": "type eq 'FORM'"},
        {"q": "type=FORM"},
        {"type": "FORM", "q": "*"}
    ]

    for idx, q_params in enumerate(query_variants):
        raw_res = _epm_request(
            "GET",
            f"/HyperionPlanning/rest/v3/applications/{app}/objects",
            params=q_params,
            user_action=f"Listing Data Forms in '{app}'" if idx == 0 else None,
            silent=(idx > 0)
        )
        try:
            r_json = json.loads(raw_res)
            if isinstance(r_json, dict) and "items" in r_json:
                for item in r_json.get("items", []):
                    f_name = item.get("name")
                    if f_name:
                        found_forms.append({
                            "name": f_name,
                            "id": item.get("id"),
                            "folder": item.get("folderName") or item.get("parentPath") or "",
                            "planType": item.get("planType") or item.get("cubeName") or ""
                        })
                break
        except Exception:
            continue

    if not found_forms:
        sf_res = _epm_request(
            "POST",
            f"/HyperionPlanning/rest/v3/applications/{app}/semanticfoundation/forms/action/findbyslices",
            payload={"planType": ACTIVE_CUBE or "OEP_WFP", "slices": []},
            silent=True
        )
        try:
            sf_json = json.loads(sf_res)
            for item in (sf_json.get("items") or sf_json.get("forms") or []):
                f_name = item.get("name") or item.get("formName")
                if f_name:
                    found_forms.append({"name": f_name, "details": item})
        except Exception:
            pass

    kw = search_keyword.lower().strip()
    if kw:
        filtered = [f for f in found_forms if kw in str(f.get("name", "")).lower()]
    else:
        filtered = found_forms

    return json.dumps({
        "application": app,
        "total_forms_found": len(found_forms),
        "matching_forms": filtered[:50]
    }, indent=2)


@mcp.tool()
def get_form_definition(form_name: str, application: str = "") -> str:
    """Retrieve the full JSON definition of an existing Data Form by its name or ID (Endpoint #6)."""
    app = application or ACTIVE_APP
    enc_name = urllib.parse.quote(form_name.strip(), safe="")
    return _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/formdefinitions/{enc_name}",
        user_action=f"Reading form definition '{form_name}' in '{app}'"
    )


def _find_default_no_member(app: str, cube: str, dim_name: str) -> str:
    """Find the 'No_<Dim>' / 'OEP_No <Dim>' / default member for a POV dimension."""
    h_json = _fetch_cached_hierarchy(app, cube, dim_name)
    all_matches = _search_hierarchy_tree(h_json, "no")
    for m in all_matches:
        m_name = m.get("member") or ""
        if m_name.lower().startswith(("no_", "no ", "oep_no", "owp_no")) or "total" in m_name.lower():
            return m_name

    children = h_json.get("children") or []
    if children:
        first_child = children[0]
        grand_children = first_child.get("children") or []
        if grand_children:
            return grand_children[0].get("name") or first_child.get("name")
        return first_child.get("name") or dim_name
    return dim_name


@mcp.tool()
def create_data_form(
    form_name: str,
    row_account_expression: str = "ILvl0Descendants(Computer_Accessories)",
    column_period_expression: str = "ILvl0Descendants(YearTotal)",
    year_member: str = "",
    scenario_member: str = "",
    version_member: str = "",
    folder_name: str = "",
    template_form_name: str = "",
    application: str = "",
    cube_name: str = ""
) -> str:
    """
    Create a Data Form in Oracle Cloud EPM (`POST /formdefinitions`, Endpoint #5).
    - Automatically discovers all cube dimensions and assigns Rows, Columns, and POV defaults.
    - Never guesses or hallucinates: returns the real HTTP response from Endpoint #5.
    """
    app = application or ACTIVE_APP
    cube = cube_name or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "OEP_WFP")

    dims_raw = _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes/{cube}/dimensions",
        params={"limit": -1},
        silent=True
    )
    cube_dims = []
    try:
        for d in json.loads(dims_raw).get("items", []):
            if d.get("name"):
                cube_dims.append(d["name"])
    except Exception:
        cube_dims = ["Account", "Period", "Years", "Scenario", "Version", "Entity", "Employee", "Job", "Property"]

    yr_mbr = year_member.strip() or "&OEP_CurYr"
    scen_mbr = scenario_member.strip() or "&OEP_Scenario"
    ver_mbr = version_member.strip() or "&OEP_Version"

    sub_raw = _epm_request("GET", f"/HyperionPlanning/rest/v3/applications/{app}/substitutionvariables", silent=True)
    sub_map = {}
    try:
        for sv in json.loads(sub_raw).get("items", []):
            sub_map[sv.get("name", "").upper()] = sv.get("value", "")
    except Exception:
        pass

    if not year_member:
        yr_mbr = sub_map.get("OEP_CURYR") or sub_map.get("CURYR") or "FY25"
    if not scenario_member:
        scen_mbr = sub_map.get("OEP_SCENARIO") or "OEP_Plan"
    if not version_member:
        ver_mbr = sub_map.get("OEP_VERSION") or "OEP_Working"

    pov_dimensions = []
    for d_name in cube_dims:
        d_up = d_name.upper()
        if d_up in ("ACCOUNT", "PERIOD"):
            continue
        elif d_up in ("YEARS", "YEAR"):
            mbr_sel = yr_mbr
        elif d_up == "SCENARIO":
            mbr_sel = scen_mbr
        elif d_up == "VERSION":
            mbr_sel = ver_mbr
        else:
            mbr_sel = _find_default_no_member(app, cube, d_name)

        pov_dimensions.append({
            "dimensionName": d_name,
            "members": [{"name": mbr_sel}]
        })

    template_json = None
    if template_form_name:
        raw_tpl = get_form_definition(template_form_name, app)
        try:
            tj = json.loads(raw_tpl)
            if isinstance(tj, dict) and "error" not in tj:
                template_json = tj
        except Exception:
            pass

    if not template_json:
        forms_list_raw = list_forms("", app)
        try:
            fl = json.loads(forms_list_raw).get("matching_forms", [])
            if fl and fl[0].get("name"):
                raw_tpl = get_form_definition(fl[0]["name"], app)
                tj = json.loads(raw_tpl)
                if isinstance(tj, dict) and "error" not in tj:
                    template_json = tj
        except Exception:
            pass

    if template_json:
        form_payload = dict(template_json)
        form_payload.pop("id", None)
        form_payload.pop("links", None)
        form_payload["name"] = form_name
        form_payload["planType"] = cube
        if folder_name:
            form_payload["folderName"] = folder_name
        if "gridDefinition" in form_payload and isinstance(form_payload["gridDefinition"], dict):
            gd = form_payload["gridDefinition"]
            gd["pov"] = {"dimensions": pov_dimensions}
            gd["pages"] = {"dimensions": []}
            gd["columns"] = [{
                "segments": [{
                    "dimensionSlices": [{
                        "dimensionName": "Period",
                        "members": [{"name": column_period_expression}]
                    }]
                }]
            }]
            gd["rows"] = [{
                "segments": [{
                    "dimensionSlices": [{
                        "dimensionName": "Account",
                        "members": [{"name": row_account_expression}]
                    }]
                }]
            }]
    else:
        form_payload = {
            "name": form_name,
            "planType": cube,
            "gridDefinition": {
                "suppressMissingBlocks": False,
                "suppressMissingDataRows": False,
                "suppressMissingDataColumns": False,
                "pov": {
                    "dimensions": pov_dimensions
                },
                "pages": {
                    "dimensions": []
                },
                "columns": [
                    {
                        "segments": [
                            {
                                "dimensionSlices": [
                                    {
                                        "dimensionName": "Period",
                                        "members": [{"name": column_period_expression}]
                                    }
                                ]
                            }
                        ]
                    }
                ],
                "rows": [
                    {
                        "segments": [
                            {
                                "dimensionSlices": [
                                    {
                                        "dimensionName": "Account",
                                        "members": [{"name": row_account_expression}]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
        if folder_name:
            form_payload["folderName"] = folder_name

    res_str = _epm_request(
        "POST",
        f"/HyperionPlanning/rest/v3/applications/{app}/formdefinitions",
        payload=form_payload,
        user_action=f"Creating Data Form '{form_name}' in cube '{cube}' ({app})"
    )

    return json.dumps({
        "requested_form_name": form_name,
        "cube": cube,
        "row_accounts": row_account_expression,
        "column_periods": column_period_expression,
        "pov_configured": pov_dimensions,
        "epm_api_response": json.loads(res_str)
    }, indent=2)


# =====================================================================
# 5. ARCHITECTURE INSPECTION, BUSINESS RULES & UNIVERSAL ROUTER
# =====================================================================
def _walk_upper_parent_storage(node: dict, depth: int = 0) -> list:
    """Recursively collect dataStorage values of intermediate rollup parent members (depth >= 1)."""
    storages = []
    children = node.get("children") or []
    if children:
        st = (node.get("dataStorage") or "").strip()
        if depth >= 1 and st:
            storages.append((node.get("name"), st))
        for child in children:
            storages.extend(_walk_upper_parent_storage(child, depth + 1))
    return storages


@mcp.tool()
def inspect_cube_architecture(application: str = "", cube_name: str = "", force_refresh: bool = False) -> str:
    """
    Dynamically inspect ANY cube in ANY application to get 100% accurate Dense/Sparse settings,
    POV dimensions vs Rollup Sparse dimensions, and parent member Data Storage.
    """
    app = application or ACTIVE_APP
    if not cube_name:
        cube_name = ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "OEP_WFP")

    cache_key = f"{app.upper()}:{cube_name.upper()}"
    if not force_refresh and cache_key in _CUBE_ARCHITECTURE_CACHE:
        print(f"  ▸ Using cached architecture profile for cube '{cube_name}' in '{app}'... ✓ Ready")
        return json.dumps(_CUBE_ARCHITECTURE_CACHE[cache_key], indent=2)

    print(f"  ▸ Analyzing live Dense/Sparse settings & member storage for cube '{cube_name}' ('{app}')...")

    dims_raw = _epm_request(
        "GET",
        f"/HyperionPlanning/rest/v3/applications/{app}/plantypes/{cube_name}/dimensions",
        params={"limit": -1},
        silent=True
    )
    try:
        dims_data = json.loads(dims_raw)
        items = dims_data.get("items", [])
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch dimensions for {cube_name} in {app}: {e}"})

    architecture = {
        "application": app,
        "cube": cube_name,
        "dense_dimensions": [],
        "pov_slice_sparse_dimensions_put_in_FIX": [],
        "hierarchical_sparse_dims_with_stored_parents_put_in_AGG": [],
        "hierarchical_sparse_dims_with_dynamic_calc_parents_put_in_FIX_RELATIVE_0": [],
        "dimensions_detail": [],
        "active_substitution_variables": {}
    }

    POV_SLICE_DIMS = {"SCENARIO", "VERSION", "YEARS", "YEAR", "CURRENCY", "HSP_VIEW", "VIEW"}
    POV_DIM_TYPES = {"SCENARIO", "VERSION", "YEAR", "YEARS", "CURRENCY", "VIEW"}

    for d in items:
        dim_name = d.get("name")
        if not dim_name:
            continue

        print(f"    • Inspecting '{dim_name}' (Density & Parent Storage)...", end="", flush=True)
        t_dim = time.perf_counter()

        h_json = _fetch_cached_hierarchy(app, cube_name, dim_name)

        raw_density = d.get("density") or h_json.get("density") or ""
        is_dense_bool = d.get("isDense") if "isDense" in d else h_json.get("isDense")
        dim_type = (d.get("dimType") or h_json.get("dimType") or "").upper()

        if isinstance(is_dense_bool, bool):
            is_dense = is_dense_bool
        elif str(raw_density).strip().upper() in ("DENSE", "TRUE"):
            is_dense = True
        elif str(raw_density).strip().upper() in ("SPARSE", "FALSE"):
            is_dense = False
        else:
            is_dense = dim_name.upper() in ("ACCOUNT", "PERIOD", "PROPERTY") or dim_type in ("ACCOUNT", "TIME")

        density_str = "Dense" if is_dense else "Sparse"
        root_storage = (h_json.get("dataStorage") or d.get("dataStorage") or "").strip()
        parent_pairs = _walk_upper_parent_storage(h_json, depth=0)

        rollup_storages = [st for _, st in parent_pairs if "label" not in st.lower()]
        has_stored_upper = any(
            ("store" in st.lower() or "never" in st.lower()) and "dynamic" not in st.lower()
            for st in rollup_storages
        )
        upper_storage_label = "Store / Never Share" if has_stored_upper else "Dynamic Calc"

        sample_members = [c.get("name") for c in (h_json.get("children") or [])[:6] if c.get("name")]
        is_pov_slice = (dim_name.upper() in POV_SLICE_DIMS) or (dim_type in POV_DIM_TYPES)

        detail = {
            "dimension": dim_name,
            "density": density_str,
            "isPovSliceDimension": is_pov_slice,
            "rootDataStorage": root_storage,
            "upperLevelParentsStorage": upper_storage_label,
            "topLevelMembers": sample_members
        }
        architecture["dimensions_detail"].append(detail)

        if is_dense:
            architecture["dense_dimensions"].append(dim_name)
            role_msg = f"Dense -> 2nd (Inner) FIX (Top members: {', '.join(sample_members[:3])})"
        elif is_pov_slice:
            architecture["pov_slice_sparse_dimensions_put_in_FIX"].append(dim_name)
            role_msg = "Sparse POV Slice -> 1st (Outer) FIX, NEVER in AGG()"
        else:
            if has_stored_upper:
                architecture["hierarchical_sparse_dims_with_stored_parents_put_in_AGG"].append(dim_name)
                role_msg = "Sparse Rollup | Parents: Store -> Use in AGG() (or 1st FIX for Calc/Copy)"
            else:
                architecture["hierarchical_sparse_dims_with_dynamic_calc_parents_put_in_FIX_RELATIVE_0"].append(dim_name)
                role_msg = "Sparse Rollup | Parents: Dynamic Calc -> Lock @RELATIVE(..., 0) in 1st FIX"

        print(f" ✓ {role_msg} ({(time.perf_counter() - t_dim)*1000:.0f} ms)")

    try:
        subvars_raw = _epm_request(
            "GET",
            f"/HyperionPlanning/rest/v3/applications/{app}/substitutionvariables",
            silent=True
        )
        subvars_json = json.loads(subvars_raw)
        for sv in subvars_json.get("items", []):
            sv_plan = sv.get("planType", "ALL")
            if sv_plan.upper() in ("ALL", cube_name.upper()):
                architecture["active_substitution_variables"][f"&{sv.get('name')}"] = sv.get("value")
    except Exception:
        pass

    _CUBE_ARCHITECTURE_CACHE[cache_key] = architecture
    return json.dumps(architecture, indent=2)


@mcp.tool()
def run_deployed_business_rule(application: str, rule_name: str, runtime_prompts_json: str = "{}") -> str:
    """Execute an existing deployed Calculation Manager Business Rule via the Jobs REST API (Endpoint #70)."""
    app = application or ACTIVE_APP
    if rule_name.strip().lower() in ("refresh database", "refreshcube", "cube_refresh", "refresh"):
        return refresh_cube(app)

    try:
        rtp = json.loads(runtime_prompts_json) if runtime_prompts_json else {}
    except json.JSONDecodeError:
        rtp = {}

    payload = {
        "jobType": "Rules",
        "jobName": rule_name,
        "parameters": rtp
    }
    return _epm_request(
        "POST",
        f"/HyperionPlanning/rest/v3/applications/{app}/jobs",
        payload=payload,
        user_action=f"Launching deployed Business Rule '{rule_name}' in '{app}'"
    )


@mcp.tool()
def call_epm_endpoint_by_id(
    endpoint_id: int,
    path_params_json: str = "{}",
    query_params_json: str = "{}",
    payload_json: str = "{}"
) -> str:
    """Execute any of the 141 official Oracle EPM REST endpoints by its catalog ID (1 to 141)."""
    if endpoint_id not in ENDPOINTS_141:
        return json.dumps({"error": f"Invalid endpoint_id {endpoint_id}. Must be between 1 and 141."})

    method, path_template, desc = ENDPOINTS_141[endpoint_id]

    try:
        path_vars = json.loads(path_params_json) if path_params_json and path_params_json.strip() not in ("", "{}") else {}
    except Exception:
        path_vars = {}

    path_vars.setdefault("application", ACTIVE_APP)
    path_vars.setdefault("appname", ACTIVE_APP)
    if ACTIVE_CUBE:
        path_vars.setdefault("plantype", ACTIVE_CUBE)

    encoded_vars = {k: urllib.parse.quote(str(v), safe="") for k, v in path_vars.items()}
    try:
        resolved_path = path_template.format(**encoded_vars)
    except KeyError as missing_key:
        return json.dumps({
            "error": f"Missing required path parameter {missing_key} for Endpoint #{endpoint_id} ({desc})",
            "template": path_template
        })

    try:
        query_params = json.loads(query_params_json) if query_params_json and query_params_json.strip() not in ("", "{}") else None
    except Exception:
        query_params = None

    if endpoint_id in (13, 79):
        if not query_params:
            query_params = {"q": json.dumps({"type": "FORM"})}
        elif "type" in query_params and "q" not in query_params:
            query_params = {"q": json.dumps(query_params)}

    try:
        payload = json.loads(payload_json) if payload_json and payload_json.strip() not in ("", "{}") else None
    except Exception:
        payload = None

    if endpoint_id == 53 and isinstance(payload, list):
        payload = {"items": payload}

    return _epm_request(
        method,
        resolved_path,
        params=query_params,
        payload=payload,
        user_action=f"{desc} (Endpoint #{endpoint_id})"
    )


@mcp.tool()
def create_or_update_calc_rule(
    application: str,
    plan_type: str,
    rule_name: str,
    script_source: str,
    script_language: str = "ASK",
    deploy_after_save: bool = True
) -> str:
    """Create or update a Business Rule in Calculation Manager (#35, #38, #41)."""
    app = application or ACTIVE_APP
    cube = plan_type or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")
    reserved = ("FCCS_", "OCX_", "OEP_", "OFS_", "OPF_", "OWP_", "TRCS_")
    clean_name = rule_name.strip()
    if clean_name.upper().startswith(reserved):
        clean_name = f"Custom_{clean_name}"

    clean_name = clean_name.replace("(AI)", "_AI").replace("(", "_").replace(")", "_").strip()
    while "__" in clean_name:
        clean_name = clean_name.replace("__", "_")
    if not clean_name.upper().endswith("_AI") and not clean_name.upper().endswith("_AI_"):
        clean_name = f"{clean_name}_AI"

    lang = (script_language or "ASK").strip().upper()
    if lang not in ("CALCSCRIPT", "CALC", "GROOVY", "ESSBASE"):
        print("\n  ┌─ [Choose Rule Script Type] ─────────────────────────────────")
        print("  │ 1. Essbase Calculation Script [Default]")
        print("  │ 2. Groovy Business Rule")
        choice = input("  └─ Select (1 or 2) [1]: ").strip()
        lang = "GROOVY" if choice == "2" else "CALCSCRIPT"
    elif lang in ("CALC", "ESSBASE", "CALC_SCRIPT"):
        lang = "CALCSCRIPT"

    lang_label = "Groovy" if lang == "GROOVY" else "Essbase Calc Script"

    payload = {
        "primaryArtifact": {
            "type": "RULE",
            "name": clean_name,
            "planType": cube
        },
        "variables": [],
        "rules": [
            {
                "name": clean_name,
                "planType": cube,
                "script": {
                    "language": lang,
                    "source": script_source
                },
                "executionOptions": {},
                "variableReferences": []
            }
        ],
        "rulesets": []
    }

    res_str = _epm_request(
        "POST",
        f"/HyperionPlanning/rest/v3/applications/{app}/calcmanager/rules",
        params={"planType": cube},
        payload=payload,
        user_action=f"Creating {lang_label} rule '{clean_name}' in cube '{cube}' ({app})"
    )

    if '"error":' in res_str:
        encoded_name = urllib.parse.quote(clean_name, safe="")
        put_res = _epm_request(
            "PUT",
            f"/HyperionPlanning/rest/v3/applications/{app}/calcmanager/rules/{encoded_name}",
            params={"planType": cube},
            payload=payload,
            user_action=f"Updating existing rule '{clean_name}' in cube '{cube}' ({app})"
        )
        if '"error":' not in put_res:
            res_str = put_res

    saved_rule_name = clean_name
    try:
        saved_json = json.loads(res_str)
        if isinstance(saved_json, dict) and saved_json.get("name"):
            saved_rule_name = saved_json["name"]
    except Exception:
        pass

    if deploy_after_save and '"error":' not in res_str:
        encoded_deploy_name = urllib.parse.quote(saved_rule_name, safe="")
        deploy_res = _epm_request(
            "POST",
            f"/HyperionPlanning/rest/v3/applications/{app}/calcmanager/rules/{encoded_deploy_name}/deploy",
            params={"planType": cube},
            user_action=f"Deploying rule '{saved_rule_name}' to Planning ({cube})"
        )
        return json.dumps({
            "status": "SUCCESS",
            "application": app,
            "cube": cube,
            "rule_name": saved_rule_name,
            "language": lang,
            "script_created": script_source,
            "save_result": json.loads(res_str),
            "deploy_result": json.loads(deploy_res)
        }, indent=2)

    return res_str


@mcp.tool()
def delete_calc_rule(application: str, plan_type: str, rule_name: str) -> str:
    """Delete a Calculation Manager rule package (#39)."""
    app = application or ACTIVE_APP
    cube = plan_type or ACTIVE_CUBE or (ACTIVE_CUBES[0] if ACTIVE_CUBES else "")
    encoded_name = urllib.parse.quote(rule_name.strip(), safe="")
    return _epm_request(
        "DELETE",
        f"/HyperionPlanning/rest/v3/applications/{app}/calcmanager/rules/{encoded_name}",
        params={"planType": cube},
        user_action=f"Deleting rule '{rule_name}' from cube '{cube}' ({app})"
    )


# =====================================================================
# 5B. SELF-LEARNING INTERNET SEARCH & PERSISTENT MEMORY TOOLS
# =====================================================================
def _load_learned_memory() -> list:
    """Load previously learned solutions from disk."""
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


@mcp.tool()
def search_internet(query: str) -> str:
    """
    Search the internet for Oracle EPM REST API payloads, Essbase calc syntax,
    Groovy examples, or error troubleshooting when the answer is not already known.
    """
    global _tool_called_in_turn
    _tool_called_in_turn = True

    print(f"  ▸ Searching the internet for: '{query}'...", end="", flush=True)
    if DDGS is None:
        print(" ✗ 'duckduckgo-search' package not installed")
        return json.dumps({
            "error": "duckduckgo-search is not installed. Run: pip install duckduckgo-search"
        })

    try:
        results = DDGS().text(query, max_results=5)
        print(f" ✓ Found {len(results)} result(s)")
        return json.dumps(results, indent=2)
    except Exception as e:
        print(f" ✗ Search failed ({e})")
        return json.dumps({"error": str(e)})


@mcp.tool()
def save_learned_solution(problem_topic: str, working_solution: str) -> str:
    """
    Save a newly discovered solution, REST payload format, or calculation fix to permanent memory
    (`learned_memory.json`) so the agent immediately knows how to solve it next time without searching again.
    """
    global _tool_called_in_turn
    _tool_called_in_turn = True

    memories = _load_learned_memory()

    updated = False
    for item in memories:
        if item.get("topic", "").lower() == problem_topic.strip().lower():
            item["solution"] = working_solution.strip()
            item["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            updated = True
            break

    if not updated:
        memories.append({
            "topic": problem_topic.strip(),
            "solution": working_solution.strip(),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        })

    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memories, f, indent=2)

    print(f"  ✓ Saved learned solution to '{os.path.basename(MEMORY_FILE)}': '{problem_topic}'")
    return json.dumps({
        "status": "SAVED",
        "memory_file": MEMORY_FILE,
        "topic": problem_topic
    }, indent=2)


# =====================================================================
# 6. LEAN SYSTEM INSTRUCTION (OPTIMIZED FOR LOW TOKENS & ZERO GUESSING)
# =====================================================================
def build_system_instruction() -> str:
    cubes_str = ", ".join(str(c) for c in ACTIVE_CUBES) if ACTIVE_CUBES else "Discover via get_cubes"
    apps_str = ", ".join(str(a) for a in ALL_APPLICATIONS) if ALL_APPLICATIONS else str(ACTIVE_APP)

    past_lessons = _load_learned_memory()
    if past_lessons:
        lessons_str = "\n".join(
            f"- [{m.get('topic')}]: {m.get('solution')}" for m in past_lessons[-20:]
        )
    else:
        lessons_str = "None yet."

    return f"""
You are an autonomous Oracle Cloud EPM & Essbase Solution Architect connected live to {os.getenv("EPM_URL")}.
Discovered Applications on Instance: {apps_str}
Current Active Application: '{ACTIVE_APP}'
Discovered Cubes (Plan Types) in '{ACTIVE_APP}': {cubes_str}
Current Active Cube (Plan Type): '{ACTIVE_CUBE}'

PREVIOUSLY LEARNED SOLUTIONS (FROM MEMORY):
{lessons_str}

SELF-LEARNING & WEB SEARCH RULES:
1. Check PREVIOUSLY LEARNED SOLUTIONS first. If a solution is already listed there, apply it directly without searching the web.
2. If you encounter an unknown EPM error, unfamiliar REST payload requirement, or new calculation logic not in your memory, call `search_internet(query="...")` to research the exact documentation or fix.
3. Once you verify that the researched solution works, ALWAYS call `save_learned_solution(problem_topic="...", working_solution="...")` so you permanently remember it for future runs!

STRICT EFFICIENCY & ANTI-HALLUCINATION RULES (MANDATORY):
1. MINIMUM API CALLS ONLY — NEVER GUESS OR RETRY FAILED URLS IN A LOOP:
   - Call ONLY the single dedicated tool that matches the user's request.
   - If an EPM REST endpoint returns an HTTP error (such as HTTP 400/404/405), DO NOT guess random URLs in a blind loop. You may call `search_internet` to check the exact Oracle EPM documentation if needed, otherwise report the exact HTTP error and reason honestly to the user!
   - NEVER claim a Form, Member, or Rule was created if the EPM API returned `"error": "HTTP 400"`!

2. DATABASE / CUBE REFRESH:
   - ALWAYS call `refresh_cube()` (1 tool call). NEVER call `run_deployed_business_rule` for a database refresh.

3. DATA FORMS (LISTING, INSPECTING, CREATING):
   - To list or search forms: Call `list_forms(search_keyword="...")` ONCE.
   - To inspect a form: Call `get_form_definition(form_name="...")` ONCE.
   - To create a data form: Call `create_data_form(form_name="...", row_account_expression="...", column_period_expression="...", ...)` ONCE. It automatically configures all POV/No_Member dimensions and clones a valid blueprint if available.

4. DIMENSION MEMBERS (SEARCHING, CREATING, MOVING, DELETING):
   - Search: Call `search_dimension_members(cube_name="{ACTIVE_CUBE or '<cube>'}", dimension_name="Account", search_keyword="...")` ONCE.
   - Create or Move (re-parent): Call `create_or_update_members_batch` ONCE with `members_json` as a JSON list of objects.
   - Delete: Call `delete_dimension_members` ONCE with `member_names_json` as a JSON list of member names (list child members before parent members).

5. ESSBASE CALCULATION SCRIPTS & BUSINESS RULES:
   - First call `inspect_cube_architecture()` to verify Dense vs Sparse dimensions.
   - Use 2-level nested FIX: 1st (Outer) FIX = Sparse dimensions ONLY; 2nd (Inner) FIX = Dense dimensions ONLY.
   - Never put Scenario, Version, or Years inside `AGG(...)`.
   - Call `create_or_update_calc_rule` ONCE when ready. Every created rule ends with `_AI`.
"""


AGENT_TOOLS = [
    get_applications,
    switch_active_application,
    switch_active_cube,
    get_cubes,
    get_dimensions_for_cube,
    get_dimension_hierarchy,
    get_member_properties,
    search_dimension_members,
    create_or_update_members_batch,
    delete_dimension_members,
    refresh_cube,
    list_forms,
    get_form_definition,
    create_data_form,
    inspect_cube_architecture,
    run_deployed_business_rule,
    create_or_update_calc_rule,
    delete_calc_rule,
    call_epm_endpoint_by_id,
    search_internet,
    save_learned_solution,
]


def _trim_history_safely(hist: list, max_items: int = 12) -> list:
    """Safely trim chat history ONLY at clean user text message boundaries."""
    if len(hist) <= max_items:
        return hist

    start_idx = len(hist) - max_items
    while start_idx < len(hist):
        msg = hist[start_idx]
        role = getattr(msg, "role", "")
        parts = getattr(msg, "parts", []) or []
        is_plain_user_turn = (
            role == "user"
            and all(getattr(p, "function_response", None) is None for p in parts)
        )
        if is_plain_user_turn:
            return hist[start_idx:]
        start_idx += 1
    return []


# =====================================================================
# 7. MAIN LOOP (RUNTIME LOGIN RTPs + AUTO/MENU APP & CUBE SELECTION)
# =====================================================================
if __name__ == "__main__":
    if "--mcp" in sys.argv:
        prompt_login_rtps(force_prompt=False)
        discover_live_environment(interactive_select=False)
        mcp.run()
        sys.exit(0)

    print("=================================================================")
    print(" Oracle Cloud EPM Autonomous AI Architect")
    print("=================================================================")
    prompt_login_rtps(force_prompt=True)
    discover_live_environment(interactive_select=True)
    print("=================================================================")

    client = genai.Client(
        api_key=os.getenv("GOOGLE_API_KEY"),
        http_options={"timeout": 60000}
    )

    FALLBACK_MODELS = [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-3.8-flash"
    ]

    history = []

    while True:
        cube_badge = f"/{ACTIVE_CUBE}" if ACTIVE_CUBE else ""
        cmd = input(f"\nEPM ({ACTIVE_APP}{cube_badge}) > ").strip()
        if not cmd:
            continue
        if cmd.lower() in ("exit", "quit"):
            break

        # Fast-path 0a: Re-enter Login RTPs at any time
        if cmd.lower() == "login":
            _CUBE_ARCHITECTURE_CACHE.clear()
            _HIERARCHY_CACHE.clear()
            prompt_login_rtps(force_prompt=True)
            discover_live_environment(interactive_select=True)
            history = []
            continue

        # Fast-path 0b: Switch application
        if cmd.lower().startswith("switch app"):
            target_app = cmd[10:].strip() or None
            discover_live_environment(preferred_app=target_app, interactive_select=(target_app is None))
            history = []
            continue

        # Fast-path 0c: Switch active cube
        if cmd.lower().startswith("switch cube"):
            target_cube = cmd[11:].strip() or None
            ACTIVE_CUBE = _choose_option(
                label=f"Cube (Plan Type) in '{ACTIVE_APP}'",
                options=ACTIVE_CUBES,
                preferred=target_cube,
                interactive=(target_cube is None)
            )
            history = []
            continue

        # Fast-path 0d: Clear conversation history manually if desired
        if cmd.lower() in ("clear", "reset"):
            history = []
            _HIERARCHY_CACHE.clear()
            print("  ✓ Conversation history & hierarchy cache cleared.")
            continue

        # Fast-path 0e: View saved learned solutions from terminal
        if cmd.lower() in ("memory", "learned"):
            print(json.dumps(_load_learned_memory(), indent=2))
            continue

        # Fast-path 1: Direct endpoint ID execution from terminal, e.g. "#109" or "#86"
        if cmd.startswith("#") and cmd[1:].split()[0].isdigit():
            parts = cmd.split(" ", 2)
            eid = int(parts[0][1:])
            p_vars = parts[1] if len(parts) > 1 else "{}"
            body_vars = parts[2] if len(parts) > 2 else "{}"
            print(call_epm_endpoint_by_id(eid, path_params_json=p_vars, payload_json=body_vars))
            continue

        # Fast-path 2: Raw REST commands typed directly (GET, POST, PUT, PATCH, DELETE)
        first_word = cmd.split(" ", 1)[0].upper()
        if first_word in ("GET", "POST", "PUT", "PATCH", "DELETE") and "/" in cmd:
            parts = cmd.split(" ", 2)
            payload = json.loads(parts[2]) if len(parts) == 3 else None
            print(_epm_request(parts[0], parts[1], payload=payload))
            continue

        _tool_called_in_turn = False
        print("  ▸ Understanding request & planning EPM actions...")

        history = _trim_history_safely(history, max_items=12)

        for model_name in FALLBACK_MODELS:
            try:
                chat = client.chats.create(
                    model=model_name,
                    history=history,
                    config=types.GenerateContentConfig(
                        system_instruction=build_system_instruction(),
                        tools=AGENT_TOOLS,
                        temperature=0,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            maximum_remote_calls=6
                        )
                    )
                )
                response = chat.send_message(cmd)
                history = chat.get_history()
                if response.text:
                    print(f"\n{response.text}")
                break
            except Exception as e:
                err_str = str(e)

                # Auto-recover from corrupted tool-call history (HTTP 400 INVALID_ARGUMENT)
                if "400" in err_str and "function response turn" in err_str.lower():
                    print("  ! Resetting tool-call history alignment and retrying...")
                    history = []
                    try:
                        chat = client.chats.create(
                            model=model_name,
                            history=[],
                            config=types.GenerateContentConfig(
                                system_instruction=build_system_instruction(),
                                tools=AGENT_TOOLS,
                                temperature=0,
                                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                                    maximum_remote_calls=6
                                )
                            )
                        )
                        response = chat.send_message(cmd)
                        history = chat.get_history()
                        if response.text:
                            print(f"\n{response.text}")
                        break
                    except Exception as retry_err:
                        err_str = str(retry_err)

                # Auto-recover from 429 Quota / Rate Limits
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait_match = re.search(r"retry in ([0-9.]+)s", err_str, re.IGNORECASE)
                    wait_sec = min(int(float(wait_match.group(1))) + 2, 30) if wait_match else 5
                    print(f"\n  ! Rate limit on '{model_name}'. Waiting {wait_sec}s before switching model...")
                    time.sleep(wait_sec)
                    continue

                # Auto-recover from transient DNS / socket errors ([Errno 11001] getaddrinfo failed)
                if any(k in err_str for k in ("11001", "getaddrinfo", "Name or service not known", "ConnectionReset", "Connection aborted")):
                    print("\n  ! Transient network/DNS glitch detected. Retrying in 3s...")
                    time.sleep(3)
                    continue

                if _tool_called_in_turn:
                    print(f"\n  ✗ Stopped after EPM execution error: {e}")
                    break

                if any(k in err_str for k in ("503", "504", "404", "NOT_FOUND", "UNAVAILABLE", "DEADLINE", "timed out", "Timeout")):
                    continue
                else:
                    print(f"\n  ✗ Error: {e}")
                    break