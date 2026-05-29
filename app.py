"""
Capital Infusion — MCA Lender Match Backend
============================================
Pure HTTP API server, no UI. Gemini analysis runs in a background thread.

Endpoints
---------
POST /application          Body: application OCR JSON
                           Returns: {"job_id": "...", "status": "received"}

POST /bank-statement       Body: bank-statement OCR JSON (optionally include "job_id" to
                           pair with a specific application; otherwise pairs with oldest
                           pending app automatically)
                           Returns: {"job_id": "...", "status": "processing"}
                           Gemini analysis runs in background; poll GET /job/<id> for result.

GET  /job/<id>             Returns: {"job_id": "...", "status": "...", "result": {...}}
                           status values: waiting_for_bank_statement | processing | complete | error

GET  /queue                Returns: {"waiting": N, "processing": N, "complete": N, "error": N}
GET  /health               Returns: {"status": "ok"}

Usage
-----
    python app.py [--port 8503]

API key
-------
Set GEMINI_API_KEY environment variable, or place it in .streamlit/secrets.toml:
    GEMINI_API_KEY = "..."
"""

import json
import os
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from google import genai

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

API_PORT = int(
    os.environ.get("PORT")
    or (sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else 8503)
)

POWER_AUTOMATE_URL = (
    "https://default87067de17bff468994aa610cdb27ba.92.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/workflows/3d829b0d2eae471f8bdf60e3c6b31dc2"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0"
    "&sig=TQff7Z25WkFLxtu8rqh-AVzkmtIEP6JlLvLkec-UvgQ"
)

# Load API key — env var takes priority, fall back to secrets.toml
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    _secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    if _secrets.exists():
        for _line in _secrets.read_text().splitlines():
            if _line.strip().startswith("GEMINI_API_KEY"):
                GEMINI_API_KEY = _line.split("=", 1)[1].strip().strip('"').strip("'")
                break

# ---------------------------------------------------------------------------
# Lender pool
# ---------------------------------------------------------------------------
LENDERS = {
    "Idea": {
        "full_name": "Idea Financial",
        "rev_req_monthly": 15_000,
        "fico_min": 650,
        "nsfs": "3 per month",
        "tib_min_years": 3.0,
        "pos": "1-2",
        "min_deposits": 8,
        "ownership_min_pct": 50,
        "restrictions": "NO REAL ESTATE, TRUCKING, No SD, ND, VT",
        "submission_email": "(Portal)",
        "contact": "Sebastian / Darwin — 305-680-3779",
    },
    "Channel Partner": {
        "full_name": "Channel Partner",
        "rev_req_monthly": 15_000,
        "fico_min": 650,
        "nsfs": "6 total",
        "tib_min_years": 3.0,
        "pos": "1",
        "min_deposits": 5,
        "ownership_min_pct": None,
        "restrictions": "NO TRUCKING / 650 credit 3 years TIB / EMAIL ONLY FOR RENEWALS",
        "submission_email": "leland.white@channelpartnersllc.com",
        "contact": "Leland White — 952-960-8399",
    },
    "CAN": {
        "full_name": "CAN Capital",
        "rev_req_monthly": 10_000,
        "fico_min": 600,
        "nsfs": None,
        "tib_min_years": 3.0,
        "pos": "1",
        "min_deposits": 3,
        "ownership_min_pct": None,
        "restrictions": "AR (accounts receivable note)",
        "submission_email": "canprequal@cancapital.com",
        "contact": "Lizzy / Mark — (678)239-3748 / mcisco@cancapital.com",
    },
    "Wall": {
        "full_name": "Wall Funding",
        "rev_req_monthly": 50_000,
        "fico_min": 575,
        "nsfs": None,
        "tib_min_years": 1.0,
        "pos": "1-2",
        "min_deposits": 5,
        "ownership_min_pct": None,
        "restrictions": "$50k minimum revenue required",
        "submission_email": "ISO@wallfunding.com",
        "contact": "Jason — 646-518-2488",
    },
    "Fund So Fast": {
        "full_name": "Fund So Fast",
        "rev_req_monthly": 25_000,
        "fico_min": 550,
        "nsfs": None,
        "tib_min_years": 1.0,
        "pos": "1-5",
        "min_deposits": None,
        "ownership_min_pct": 51,
        "restrictions": "NO CALI, MD, DAKOTAS / Ask me",
        "submission_email": "frank@fundsofast.com",
        "contact": "Frank — (347)248-7204",
    },
    "LG": {
        "full_name": "LG Funding",
        "rev_req_monthly": 20_000,
        "fico_min": 600,
        "nsfs": None,
        "tib_min_years": 1.5,
        "pos": "2-4",
        "min_deposits": 1,
        "ownership_min_pct": 50,
        "restrictions": None,
        "submission_email": "chaim@lgfunding.com",
        "contact": "Chaim Zagelbaum — (718)362-2264",
    },
    "Legend": {
        "full_name": "Legend Funding",
        "rev_req_monthly": 25_000,
        "fico_min": 600,
        "nsfs": "3 per month",
        "tib_min_years": 1.0,
        "pos": "1-3",
        "min_deposits": None,
        "ownership_min_pct": None,
        "restrictions": None,
        "submission_email": "apps@LegendFunding.com",
        "contact": "Kevin Duffy / Bianca — (609)221-5386",
    },
    "Pinnacle": {
        "full_name": "Pinnacle Business Funding",
        "rev_req_monthly": 15_000,
        "fico_min": 550,
        "nsfs": None,
        "tib_min_years": 1.0,
        "pos": "2-5",
        "min_deposits": 3,
        "ownership_min_pct": 51,
        "restrictions": "NO CALI, UTAH. UTAH NO TRUCKING",
        "submission_email": "submissions@pbffunding.com",
        "contact": "Brandon Hochman — (443)835-5475",
    },
    "Specialty": {
        "full_name": "Specialty Capital",
        "rev_req_monthly": 20_000,
        "fico_min": None,
        "nsfs": "3 per month",
        "tib_min_years": 1.0,
        "pos": None,
        "min_deposits": None,
        "ownership_min_pct": None,
        "restrictions": None,
        "submission_email": "boris@specialtycapital.com",
        "contact": "Boris Kalendarev — 917-353-5795",
    },
    "Britecap": {
        "full_name": "Britecap Financial",
        "rev_req_monthly": 25_000,
        "fico_min": 660,
        "nsfs": None,
        "tib_min_years": 3.0,
        "pos": None,
        "min_deposits": None,
        "ownership_min_pct": None,
        "restrictions": "No NV ND RI SD VT CA, No Real Estate, Insurance, Auto Dealers / 660+ Fico, 3+ TIB, 300k Annual revenue",
        "submission_email": "(Portal)",
        "contact": "Daniel Padilla — 818-338-0747 ext 150",
    },
}

INDUSTRY_KEYWORDS = {
    "Real Estate": ["real estate", "property", "realty", "realtor", "rental", "mortgage", "landlord"],
    "Trucking": ["truck", "trucking", "freight", "hauling", "transport", "logistics", "delivery", "carrier"],
    "Insurance": ["insurance", "insurer", "brokerage", "underwriting", "claims"],
    "Auto Dealers": ["auto dealer", "car dealer", "dealership", "automotive", "used car", "vehicle sales"],
}

# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------
def evaluate_pos(lender_pos, applicant_position: int) -> str:
    if lender_pos is None:
        return "PASS — any position accepted"
    s = str(lender_pos).strip()
    try:
        if "-" in s:
            lo, hi = (int(x.strip()) for x in s.split("-", 1))
            if lo <= applicant_position <= hi:
                return f"PASS — position {applicant_position} is within [{lo}–{hi}]"
            return f"FAIL — position {applicant_position} is NOT within [{lo}–{hi}]"
        else:
            req = int(s)
            if applicant_position == req:
                return f"PASS — position {applicant_position} matches {req}"
            return f"FAIL — position {applicant_position} does not match {req}"
    except Exception:
        return f"UNKNOWN — could not parse pos value '{s}'"


def parse_ocr_app_json(raw: dict) -> dict:
    inner = raw.get("data", raw)
    result = {}

    val = inner.get("estimated_fico_score")
    if val is not None:
        try:
            result["fico"] = int(float(str(val).replace(",", "").strip()))
        except (ValueError, TypeError):
            pass

    ownership_raw = (
        inner.get("ownership_percentage") or
        inner.get("Percent_Ownership") or
        inner.get("Principle_Ownership") or ""
    )
    if ownership_raw:
        try:
            result["ownership"] = int(float(str(ownership_raw).replace("%", "").strip()))
        except (ValueError, TypeError):
            pass

    tib_raw = inner.get("time_in_business_years") or inner.get("Time_in_Business")
    if tib_raw is not None:
        try:
            result["tib"] = float(tib_raw)
        except (ValueError, TypeError):
            pass

    biz = inner.get("business_description") or inner.get("Industry_App") or ""
    if biz:
        result["industry"] = str(biz)

    state_raw = (
        inner.get("state") or inner.get("business_state") or
        inner.get("Business_State") or inner.get("address_state") or
        inner.get("state_code") or ""
    )
    if state_raw:
        result["state"] = str(state_raw).strip()

    zip_raw = (
        inner.get("zip") or inner.get("zip_code") or
        inner.get("business_zip") or inner.get("Business_Zip") or
        inner.get("postal_code") or ""
    )
    if zip_raw:
        result["zip"] = str(zip_raw).strip()

    return result


_STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

def _normalize_state(raw: str) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_ABBREVS.get(s.lower())


def _format_start_date(raw: str) -> str | None:
    """Convert MM/YYYY or MM/DD/YYYY to MM-DD-YYYY for Orbit."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        parts = raw.replace("-", "/").split("/")
        if len(parts) == 2:          # MM/YYYY
            return f"{parts[1]}-{parts[0].zfill(2)}-01"
        if len(parts) == 3:          # MM/DD/YYYY
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    except Exception:
        pass
    return raw


def _normalize_entity_type(raw_entity: str) -> str | None:
    """Normalize Entity_Type1 to a standard code."""
    if not raw_entity:
        return None
    e = raw_entity.upper().strip()
    if "LLLP" in e or "LIMITED LIABILITY LIMITED" in e:
        return "LLLP"
    if "PLLC" in e or "PROFESSIONAL LIMITED LIABILITY" in e:
        return "PLLC"
    if "LLP" in e or "LIMITED LIABILITY PARTNERSHIP" in e:
        return "LLP"
    if "LLC" in e or ("LIMITED LIABILITY COMPANY" in e):
        return "LLC"
    if "LP" in e or "LIMITED PARTNERSHIP" in e:
        return "LP"
    if "S CORP" in e or "S CORPORATION" in e:
        return "S_CORP"
    if "C CORP" in e or "C CORPORATION" in e:
        return "C_CORP"
    if "SOLE PROPRIETOR" in e:
        return "SOLE_PROP"
    if "GENERAL PARTNERSHIP" in e or e == "GP":
        return "GP"
    if "CORP" in e:
        return "CORP"
    if "PARTNERSHIP" in e:
        return "PARTNERSHIP"
    if "NON PROFIT" in e or "NONPROFIT" in e or "NOT FOR PROFIT" in e:
        return "NONPROFIT"
    return None


_KAPITUS_ENTITY = {
    "LLC":         "Limited Liability Company (LLC)",
    "PLLC":        "Professional Limited Liability Company (PLLC)",
    "LLLP":        "Limited Liability Limited Partnership (LLLP)",
    "LLP":         "Limited Liability Partnership (LLP)",
    "LP":          "Limited Partnership (LP)",
    "S_CORP":      "S Corporation (S Corp)",
    "C_CORP":      "C Corporation (C Corp)",
    "CORP":        "C Corporation (C Corp)",
    "SOLE_PROP":   "Sole Proprietorship",
    "GP":          "General Partnership (GP)",
    "PC":          "Professional Corporation (PC)",
    "NONPROFIT":   None,
    "PARTNERSHIP": None,
}

_IDEA_ENTITY = {
    "LLC":         "limited-liability-company",
    "PLLC":        "limited-liability-company",
    "LLLP":        None,
    "LLP":         "legal-partnership",
    "LP":          "limited-partnership",
    "S_CORP":      "corporation",
    "C_CORP":      "corporation",
    "CORP":        "corporation",
    "SOLE_PROP":   "sole-proprietorship",
    "GP":          "general-partnership",
    "PARTNERSHIP": "legal-partnership",
    "NONPROFIT":   "not-for-profit",
}

_CAN_ENTITY = {
    "LLC":         "LLC",
    "PLLC":        "LLC",
    "LLLP":        None,
    "LLP":         "LLP",
    "LP":          "Limited Partnership",
    "S_CORP":      "Corporation",
    "C_CORP":      "Corporation",
    "CORP":        "Corporation",
    "SOLE_PROP":   "Sole Proprietorship",
    "GP":          "Partnership",
    "PARTNERSHIP": "Partnership",
    "NONPROFIT":   "Other",
}

_CHANNEL_ENTITY = {
    "LLC":         "LLC",
    "PLLC":        "LLC",
    "LLLP":        None,
    "LLP":         "Partnership",
    "LP":          "Partnership",
    "S_CORP":      "S Corp",
    "C_CORP":      "C Corp",
    "CORP":        "C Corp",
    "SOLE_PROP":   None,
    "GP":          "Partnership",
    "PARTNERSHIP": "Partnership",
    "NONPROFIT":   "Non Profit",
}

_FORWARD_ENTITY = {
    "LLC":         "Limited Liability Company (LLC)",
    "PLLC":        "Limited Liability Company (LLC)",
    "LLLP":        None,
    "LLP":         "Limited Liability Partnership (LLP)",
    "LP":          "Limited Partnership (LP)",
    "S_CORP":      "Corporation",
    "C_CORP":      "Corporation",
    "CORP":        "Corporation",
    "SOLE_PROP":   "Sole Proprietor",
    "GP":          "General Partnership",
    "PARTNERSHIP": None,
    "NONPROFIT":   None,
}


def _map_orbit_fields(raw: dict) -> dict:
    """Map raw OCR field names to Orbit API names."""
    def _get(*keys):
        for k in keys:
            v = raw.get(k)
            if v is not None:
                return v
        return None

    entity_code = _normalize_entity_type(_get("Entity_Type1") or "")
    def _entity(lender_map):
        return lender_map.get(entity_code) if entity_code else None

    return {
        # Business
        "businessName":      _get("Business_Legal_Name"),
        "dba":               _get("Doing_Business_As_DBA"),
        "address":           _get("Business_Address"),
        "businessCity":      _get("Business_City"),
        "state":             _normalize_state(_get("Business_State", "business_state") or ""),
        "zip":               _get("Business_Zip", "business_zip"),
        "phone":             _get("Business_Phone"),
        "businessEmail":     _get("Business_Email"),
        "businessStartDate": _format_start_date(_get("Date_Current_Ownership_Started") or ""),
        "ein":               _get("Federal_Tax_ID"),
        # Principal Owner
        "ownerName":         _get("Principle_Owner_Name"),
        "ownershipPercent":  _get("Principle_Ownership", "Percent_Ownership", "ownership_percentage"),
        "ownerPhone":        _get("Principle_Phone"),
        "email":             _get("Principle_Email"),
        "ownerAddress":      _get("Principle_Address"),
        "ownerCity":         _get("Principle_City"),
        "ownerState":        _get("Principle_State"),
        "ownerZip":          _get("Principle_Zip"),
        "ownerSSN":          _get("Principle_SSN"),
        "ownerDOB":          _get("Principle_DOB"),
        # Secondary Owner
        "secondaryOwnerName":        _get("Secondary_Owner_Name"),
        "secondaryOwnershipPercent": _get("Secondary_Ownership"),
        "secondaryPhone":            _get("Secondary_Phone"),
        "secondaryEmail":            _get("Secondary_Email1"),
        "secondaryAddress":          _get("Secondary_Address"),
        "secondaryCity":             _get("Secondary_City"),
        "secondaryState":            _get("Secondary_State"),
        "secondaryZip":              _get("Secondary_Zip"),
        "secondarySSN":              _get("Secondary_SSN"),
        "secondaryDOB":              _get("Secondary_DOB"),
        # Financial
        "portalMonthlyRev":   _get("Portal_Monthly_Rev", "Average_Monthly_Deposits"),
        "portalMobile":       _get("Portal_Mobile"),
        "portalEmail":        _get("Portal_Email"),
        "requestedAmount":    _get("Requested_Funding_Amount"),
        "timeInBusiness":     _get("Time_in_Business", "time_in_business_years"),
        "industry":           _get("Industry_App", "business_description"),
        # Entity types per lender
        "kapitusEntityType":    _entity(_KAPITUS_ENTITY),
        "ideaEntityType":       _entity(_IDEA_ENTITY),
        "canEntityType":        _entity(_CAN_ENTITY),
        "channelPartnersEntity": _entity(_CHANNEL_ENTITY),
        "forwardEntityType":    _entity(_FORWARD_ENTITY),
    }


def extract_monthly_rev(bs: dict) -> float | None:
    metrics = bs.get("summary_metrics", bs)
    rev = metrics.get("total_revenue") or metrics.get("total_credits")
    return float(rev) if isinstance(rev, (int, float)) else None


def extract_bs_metrics(bs: dict) -> dict:
    metrics = bs.get("summary_metrics", bs)
    return {
        "nsf_count": metrics.get("nsf_count"),
        "pos_count": metrics.get("pos_count"),
        "deposit_count": metrics.get("deposit_count"),
        "total_revenue": metrics.get("total_revenue") or metrics.get("total_credits"),
        "avg_daily_balance": metrics.get("avg_daily_balance"),
        "cash_flow": metrics.get("cash_flow"),
    }


# ---------------------------------------------------------------------------
# Gemini analysis
# ---------------------------------------------------------------------------
def build_prompt(app: dict, bs: dict) -> str:
    stacking = app.get("stacking_positions", 0)
    loan_position = stacking + 1
    monthly_revenue = app.get("monthly_revenue", 0)
    industry = app.get("industry", "Not specified")

    lenders_augmented = {
        code: {**d, "_pos_check": evaluate_pos(d.get("pos"), loan_position)}
        for code, d in LENDERS.items()
    }

    return f"""You are an expert MCA (Merchant Cash Advance) underwriting advisor at Capital Infusion.

Analyze the applicant profile against every lender. Output ONLY a single valid JSON object — no markdown, no prose, no code fences.

=== APPLICANT PROFILE ===
Industry          : {industry}
State             : {app.get("state", "Not provided")}
ZIP               : {app.get("zip", "Not provided")}
Monthly Revenue   : ${monthly_revenue:,.2f}
FICO Score        : {app.get("fico", "Not provided")}
Time in Business  : {app.get("tib", "Not provided")} years
Ownership         : {app.get("ownership", "Not provided")}%
Existing MCAs     : {stacking}
This Loan Position: {loan_position}  (existing MCAs + 1)

=== BANK STATEMENT DATA ===
{json.dumps(bs, indent=2)}

Key signals to extract from bank statement:
- nsf_count    : total NSF occurrences
- pos_count    : point-of-sale TRANSACTION volume (payment method signal — NOT stacking)
- deposit_count: deposits per month
- total_revenue / total_credits, avg_daily_balance, cash_flow

CRITICAL: Each lender has "_pos_check" — the stacking position result already computed.
Copy it verbatim into the criteria result. Do NOT recalculate. pos_count ≠ stacking positions.

=== LENDER POOL ===
{json.dumps(lenders_augmented, indent=2)}

=== FIELD GUIDE ===
rev_req_monthly   : Min monthly revenue. null = any.
fico_min          : Min FICO. null = any (auto-PASS).
nsfs              : NSF cap. null = any. "3 per month" / "6 total" are hard caps.
tib_min_years     : Min years in business.
pos / _pos_check  : Loan position. Use _pos_check result directly.
min_deposits      : Min deposits/month. null = any.
ownership_min_pct : Min ownership %. null = any.
restrictions      : Industry or state bans. null = none.

=== INSTRUCTIONS ===
1. Restrictions first — industry OR state ban = DOES_NOT_QUALIFY immediately.
   Match applicant State to abbreviations/names in restrictions (e.g. "NO CALI" = California).
2. For remaining criteria: PASS, FAIL, or BORDERLINE (within 10% of threshold).
3. null = automatic PASS.
4. Rank qualifying lenders best-fit first (most criteria comfortably exceeded).
5. qualifying_lenders MUST contain every lender whose overall is QUALIFIES — no omissions. The count must match exactly.

=== REQUIRED JSON OUTPUT STRUCTURE ===
{{
  "lender_evaluations": [
    {{
      "code": "Idea",
      "full_name": "Idea Financial",
      "overall": "QUALIFIES",
      "rank": 1,
      "criteria": [
        {{"name": "Industry Restriction", "required": "No RE/Trucking/SD/ND/VT", "applicant": "{industry}", "result": "PASS"}},
        {{"name": "Monthly Revenue", "required": "$15,000/mo", "applicant": "$X,XXX", "result": "PASS"}},
        {{"name": "FICO", "required": "650", "applicant": "XXX", "result": "PASS"}},
        {{"name": "NSFs", "required": "3/month", "applicant": "X", "result": "PASS"}},
        {{"name": "TIB", "required": "3.0 yrs", "applicant": "X.X yrs", "result": "PASS"}},
        {{"name": "Stacking Position", "required": "1-2", "applicant": "{loan_position}", "result": "PASS"}},
        {{"name": "Min Deposits", "required": "8/mo", "applicant": "X", "result": "PASS"}},
        {{"name": "Ownership", "required": "50%", "applicant": "X%", "result": "PASS"}}
      ],
      "notes": "One sentence on fit quality.",
      "contact": "Sebastian / Darwin — 305-680-3779",
      "submission_email": "(Portal)"
    }}
  ],
  "qualifying_lenders": [
    {{
      "rank": 1,
      "code": "Idea",
      "full_name": "Idea Financial",
      "summary": "1-2 sentence fit summary with key numbers.",
      "contact": "Sebastian / Darwin — 305-680-3779",
      "submission_email": "(Portal)"
    }}
  ],
  "concerns": ["List any borderline numbers, missing data, state confirmations needed."],
  "no_qualifying_lenders": false,
  "closest_match_if_none": null
}}

Return ALL lenders in lender_evaluations. Use overall values: QUALIFIES, DOES_NOT_QUALIFY, or CONDITIONAL.
Set rank to null for non-qualifying lenders. Output only the JSON — nothing else.
"""


def _post_webhook(payload: dict):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        POWER_AUTOMATE_URL,
        data=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


def run_analysis(job_id: str, app_raw: dict, bs_raw: dict):
    """Runs in a background thread. Writes result.json or error.json."""
    job_dir = JOBS_DIR / job_id
    try:
        app = parse_ocr_app_json(app_raw)
        monthly_rev = extract_monthly_rev(bs_raw)
        if monthly_rev is not None:
            app["monthly_revenue"] = monthly_rev

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=build_prompt(app, bs_raw),
            config={"response_mime_type": "application/json"},
        )

        gemini_json = json.loads(response.text)

        # Fill in any missing full_name from the lender pool
        for entry in gemini_json.get("qualifying_lenders", []):
            if not entry.get("full_name"):
                entry["full_name"] = LENDERS.get(entry.get("code", ""), {}).get("full_name", entry.get("code", ""))
        for entry in gemini_json.get("lender_evaluations", []):
            if not entry.get("full_name"):
                entry["full_name"] = LENDERS.get(entry.get("code", ""), {}).get("full_name", entry.get("code", ""))

        result = {
            "clientCode": job_id,
            "status": "complete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "application_data": _map_orbit_fields(app_raw),
            "applicant": {
                "industry": app.get("industry", "Not specified"),
                "state": app.get("state", "Not provided"),
                "zip": app.get("zip", "Not provided"),
                "monthly_revenue": app.get("monthly_revenue", 0),
                "fico": app.get("fico"),
                "tib_years": app.get("tib"),
                "ownership_pct": app.get("ownership"),
                "stacking_positions": app.get("stacking_positions", 0),
                "loan_position": app.get("stacking_positions", 0) + 1,
            },
            "bank_statement_metrics": extract_bs_metrics(bs_raw),
            **gemini_json,
        }

        (job_dir / "result.json").write_text(json.dumps(result, indent=2))
        print(f"[job {job_id[:8]}] complete — {len(gemini_json.get('qualifying_lenders', []))} qualifying lenders")

        try:
            status_code = _post_webhook(result)
            print(f"[job {job_id[:8]}] webhook delivered → HTTP {status_code}")
        except Exception as wb_exc:
            print(f"[job {job_id[:8]}] webhook failed: {wb_exc}")

    except Exception as exc:
        error = {
            "clientCode": job_id,
            "status": "error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        (job_dir / "error.json").write_text(json.dumps(error, indent=2))
        print(f"[job {job_id[:8]}] ERROR: {exc}")


# ---------------------------------------------------------------------------
# Job directory helpers
# ---------------------------------------------------------------------------
def _job_status(job_dir: Path) -> str:
    has_app = (job_dir / "app.json").exists()
    has_bs  = (job_dir / "bs.json").exists()
    if (job_dir / "result.json").exists():
        return "complete"
    if (job_dir / "error.json").exists():
        return "error"
    if has_app and has_bs:
        return "processing"
    if has_bs and not has_app:
        return "waiting_for_application"
    if has_app and not has_bs:
        return "waiting_for_bank_statement"
    return "unknown"



def _queue_counts() -> dict:
    counts = {"waiting_for_bank_statement": 0, "processing": 0, "complete": 0, "error": 0}
    for job_dir in JOBS_DIR.iterdir():
        if job_dir.is_dir():
            s = _job_status(job_dir)
            if s in counts:
                counts[s] += 1
    return counts


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _send(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip("/")

        if path == "/health":
            self._send(200, {"status": "ok", "port": API_PORT, "lenders": len(LENDERS)})

        elif path == "/queue":
            self._send(200, _queue_counts())

        elif path.startswith("/job/"):
            client_id = path[len("/job/"):]
            job_dir = JOBS_DIR / client_id
            if not job_dir.is_dir():
                self._send(404, {"error": f"no job found for client_id '{client_id}'"})
                return
            status = _job_status(job_dir)
            if status == "complete":
                self._send(200, json.loads((job_dir / "result.json").read_text()))
            elif status == "error":
                self._send(200, json.loads((job_dir / "error.json").read_text()))
            else:
                self._send(200, {"client_id": client_id, "status": status})

        elif path == "/jobs":
            jobs = []
            for job_dir in sorted(JOBS_DIR.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
                if not job_dir.is_dir():
                    continue
                status = _job_status(job_dir)
                entry = {"client_id": job_dir.name, "status": status}
                if status == "complete":
                    try:
                        result = json.loads((job_dir / "result.json").read_text())
                        entry["qualifying_lenders"] = len(result.get("qualifying_lenders", []))
                        entry["timestamp"] = result.get("timestamp")
                        entry["industry"] = result.get("applicant", {}).get("industry")
                    except Exception:
                        pass
                jobs.append(entry)
            self._send(200, {"total": len(jobs), "jobs": jobs})

        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.rstrip("/")
        if path.startswith("/job/"):
            client_id = path[len("/job/"):]
            job_dir = JOBS_DIR / client_id
            if not job_dir.is_dir():
                self._send(404, {"error": f"no job found for client_id '{client_id}'"})
                return
            import shutil
            shutil.rmtree(job_dir)
            print(f"[{client_id}] job deleted")
            self._send(200, {"client_id": client_id, "deleted": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return

        path = self.path.rstrip("/")

        if path == "/application":
            if not GEMINI_API_KEY:
                self._send(500, {"error": "GEMINI_API_KEY not configured"})
                return
            client_id = data.pop("client_id", None) if isinstance(data, dict) else None
            if not client_id:
                self._send(400, {"error": "client_id is required"})
                return

            job_dir = JOBS_DIR / client_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "app.json").write_text(json.dumps(data))
            # Clear any previous result so a re-submission starts fresh
            for stale in ("result.json", "error.json"):
                (job_dir / stale).unlink(missing_ok=True)
            print(f"[{client_id}] application received")

            # If bank statement already arrived first, trigger analysis now
            if (job_dir / "bs.json").exists():
                print(f"[{client_id}] bank statement already present — launching analysis")
                bs_raw = json.loads((job_dir / "bs.json").read_text())
                threading.Thread(
                    target=run_analysis,
                    args=(client_id, data, bs_raw),
                    daemon=True,
                ).start()
                self._send(200, {"client_id": client_id, "status": "processing",
                                 "poll": f"GET /job/{client_id}"})
            else:
                self._send(200, {"client_id": client_id, "status": "received"})

        elif path == "/bank-statement":
            if not GEMINI_API_KEY:
                self._send(500, {"error": "GEMINI_API_KEY not configured"})
                return
            client_id = data.pop("client_id", None) if isinstance(data, dict) else None
            if not client_id:
                self._send(400, {"error": "client_id is required"})
                return

            job_dir = JOBS_DIR / client_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "bs.json").write_text(json.dumps(data))
            print(f"[{client_id}] bank statement received")

            # If application already arrived, trigger analysis now
            if (job_dir / "app.json").exists():
                print(f"[{client_id}] application already present — launching analysis")
                app_raw = json.loads((job_dir / "app.json").read_text())
                threading.Thread(
                    target=run_analysis,
                    args=(client_id, app_raw, data),
                    daemon=True,
                ).start()
                self._send(200, {"client_id": client_id, "status": "processing",
                                 "poll": f"GET /job/{client_id}"})
            else:
                self._send(200, {"client_id": client_id, "status": "waiting_for_application"})

        else:
            self._send(404, {"error": "unknown endpoint"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _recover_orphaned_jobs():
    """On startup, re-run any jobs that have both documents but no result yet."""
    recovered = 0
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        has_app = (job_dir / "app.json").exists()
        has_bs = (job_dir / "bs.json").exists()
        has_result = (job_dir / "result.json").exists()
        has_error = (job_dir / "error.json").exists()
        if has_app and has_bs and not has_result and not has_error:
            client_id = job_dir.name
            print(f"[{client_id}] recovering orphaned job — relaunching analysis")
            app_raw = json.loads((job_dir / "app.json").read_text())
            bs_raw = json.loads((job_dir / "bs.json").read_text())
            threading.Thread(
                target=run_analysis,
                args=(client_id, app_raw, bs_raw),
                daemon=True,
            ).start()
            recovered += 1
    if recovered:
        print(f"==> Recovered {recovered} orphaned job(s)")


if __name__ == "__main__":
    if not GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY not set. Requests will fail until it is configured.")

    _recover_orphaned_jobs()
    server = HTTPServer(("0.0.0.0", API_PORT), _Handler)
    print(f"Capital Infusion MCA Backend running on port {API_PORT}")
    print(f"  POST http://localhost:{API_PORT}/application")
    print(f"  POST http://localhost:{API_PORT}/bank-statement")
    print(f"  GET  http://localhost:{API_PORT}/job/<id>")
    print(f"  GET  http://localhost:{API_PORT}/queue")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
