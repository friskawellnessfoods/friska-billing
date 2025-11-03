#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing Web App — Streamlit Cloud + Service Account
# - Counts meals/snacks/juices/breakfast/seafood + paused
# - Delivery from sheet (col G) with your custom rules
# - Date blocks from H, 6 columns per date
# - Debug panel shows delivery mode decisions by month
# =========================================================

import streamlit as st
import os, re, json, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"

# layout constants
COL_A = 0
COL_B_CLIENT = 1
COL_C_TYPE = 2
COL_G_DELIVERY = 6          # G (0-based indexing)
START_DATA_COL_IDX = 7      # H
COLUMNS_PER_BLOCK  = 6      # Meal1, Meal2, Snack, J1, J2, Breakfast

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "price_nutri": 180.0,
    "price_high_protein": 200.0,
    "price_seafood_addon": 80.0,
    "price_juice": 0.0,
    "price_snack": 0.0,
    "price_breakfast": 0.0,
    "gst_percent": 5.0,
}

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

# --------- Secrets -> session (accepts JSON string OR TOML fields) ----------
def get_service_account_session() -> AuthorizedSession:
    try:
        sec = st.secrets["gcp_credentials"]
    except Exception:
        st.error("Secrets missing: Add your Service Account in **Settings → Secrets** as [gcp_credentials].")
        st.stop()

    try:
        if isinstance(sec, dict) and "value" in sec and isinstance(sec["value"], str):
            sa_info = json.loads(sec["value"])
        else:
            sa_info = dict(sec)
        creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
        return AuthorizedSession(creds)
    except json.JSONDecodeError:
        st.error("Secrets format error: your JSON under gcp_credentials.value is not valid. Make sure it starts with '{' and ends with '}'.")
        st.stop()
    except Exception as e:
        st.error(f"Could not initialize Google credentials: {e}")
        st.stop()

# --------- Helpers ----------
WEEKDAY_PREFIX = re.compile(
    r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*",
    re.I
)

def get_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        st.error("Invalid SHEET_URL.")
        st.stop()
    return m.group(1)

def fetch_values(session: AuthorizedSession, spid: str, a1_range: str) -> List[List[str]]:
    from urllib.parse import quote
    enc = quote(a1_range, safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{enc}"
    r = session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=30)
    if r.status_code == 403:
        st.error("Permission denied by Google Sheets API.\nShare the Sheet with the **service account email** (Viewer).")
        st.stop()
    r.raise_for_status()
    return r.json().get("values", []) or []

def to_dt(v) -> Optional[datetime]:
    if isinstance(v, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=float(v))
    s = str(v or "").strip()
    if not s:
        return None
    s = WEEKDAY_PREFIX.sub("", s)
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y", "%d %b %y"):
        try:
            return datetime.strptime(s, fmt)
        except:
            pass
    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s*$", s)   # e.g. '2 Nov'
    if m:
        try:
            month = m.group(2).title()
            y = date.today().year
            return datetime.strptime(f"{m.group(1)}-{month}-{y}", "%d-%b-%Y")
        except:
            return None
    return None

def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def get_clientlist_sheet_title(session: AuthorizedSession, spid: str, month_full: str) -> Tuple[Optional[str], List[str]]:
    desired1 = f"clientlist {month_full}".lower()
    desired2 = f"clientlist {month_full[:3]}".lower()
    meta = session.get(f"https://sheets.googleapis.com/v4/spreadsheets/{spid}", timeout=30).json()
    titles = [sh["properties"]["title"] for sh in meta.get("sheets", [])]
    for t in titles:
        tl = t.lower().strip()
        if tl == desired1 or tl == desired2:
            return t, titles
    for t in titles:
        if t.lower().startswith("clientlist "):
            return t, titles
    return None, titles

def month_span_inclusive(a: date, b: date) -> List[Tuple[int, int]]:
    out = []
    y, m = a.year, a.month
    while (y < b.year) or (y == b.year and m <= b.month):
        out.append((y, m))
        if m == 12:
            y += 1; m = 1
        else:
            m += 1
    return out

def parse_float(x) -> float:
    try:
        if x is None: return 0.0
        s = str(x).strip()
        if not s: return 0.0
        num = re.sub(r"[^0-9\.\-]", "", s)
        return float(num) if num else 0.0
    except:
        return 0.0

# --------- Delivery pricing logic (FIXED) ----------
def compute_delivery_per_day_for_rows(rows: List[int], data: List[List[str]]) -> Tuple[float, str, List[Dict[str, str]]]:
    """
    Returns (per_day_price, mode, details)
    mode ∈ {"single_identical", "sum_shifts", "single_mismatch", "none"}
    Rules:
      - If ALL row "Type" (col C) are exactly the same -> charge ONCE (per-day = max price).
      - Else if ANY row mentions morning/evening AND not all same shift -> SUM prices.
      - Else (types differ but no morning/evening) -> charge ONCE (per-day = max price).
    """
    if not rows:
        return 0.0, "none", []

    types, prices, details = [], [], []
    for r in rows:
        row = data[r] if r < len(data) else []
        typ = str(row[COL_C_TYPE]).strip() if len(row) > COL_C_TYPE else ""
        price = parse_float(row[COL_G_DELIVERY] if len(row) > COL_G_DELIVERY else "")
        types.append(typ)
        prices.append(price)
        details.append({"row": str(r+1), "type": typ, "price": f"{price:.2f}"})

    norm_types = [norm_name(t) for t in types]

    def all_equal(vals: List[str]) -> bool:
        return len(vals) > 0 and len(set(vals)) == 1

    # Detect shifts
    has_morning = any("morning" in t for t in norm_types)
    has_evening = any("evening" in t for t in norm_types)

    if all_equal(norm_types):
        # all identical (even if all blank) -> once
        per_day = max(prices) if prices else 0.0
        return per_day, "single_identical", details

    if has_morning or has_evening:
        # if all rows same shift -> once, else sum
        if all(t == "morning delivery" for t in norm_types):
            per_day = max(prices) if prices else 0.0
            return per_day, "single_identical", details
        if all(t == "evening delivery" for t in norm_types):
            per_day = max(prices) if prices else 0.0
            return per_day, "single_identical", details
        # Mixed (morning/evening/blank/others) -> sum
        per_day = sum(prices) if prices else 0.0
        return per_day, "sum_shifts", details

    # Types differ but no explicit shift words -> once
    per_day = max(prices) if prices else 0.0
    return per_day, "single_mismatch", details

# --------- Counting logic (with diagnostics, includes delivery) ----------
def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str, debug: bool=False):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0, paused=0)
    grand_delivery_days = 0
    delivery_amount_total = 0.0

    diag = {
        "months": [],
        "tabs_checked": [],
        "dates_seen": {},
        "client_found_rows": {},
        "delivery_by_month": []
    }

    for (yy, mm) in month_span_inclusive(start, end):
        month_name = calendar.month_name[mm]
        sheet_title, all_titles = get_clientlist_sheet_title(session, spid, month_name)
        diag["months"].append(month_name)
        diag["tabs_checked"].append({"month": month_name, "resolved_tab": sheet_title, "all_tabs": all_titles})

        if not sheet_title:
            continue

        data = fetch_values(session, spid, f"{sheet_title}!A1:ZZ2000")
        if not data:
            continue

        # map client rows
        lut = {}
        for r, row in enumerate(data):
            name = norm_name(row[COL_B_CLIENT]) if len(row) > COL_B_CLIENT else ""
            if name:
                lut.setdefault(name, []).append(r)
        client_rows = lut.get(client_key, [])
        diag["client_found_rows"][sheet_title] = client_rows

        # dates on row 1
        row1 = data[0] if data else []
        date_to_block = {}
        c = START_DATA_COL_IDX
        while c < len(row1):
            dt = to_dt(row1[c]) if c < len(row1) else None
            if dt:
                date_to_block[dt.date()] = c
            else:
                if date_to_block and (c >= len(row1) or not str(row1[c]).strip()):
                    break
            c += COLUMNS_PER_BLOCK
        diag["dates_seen"][sheet_title] = [d.strftime("%d-%b-%y") for d in sorted(date_to_block.keys())[:10]]

        # iterate this month's slice
        first_day = date(yy, mm, 1)
        last_day  = date(yy, mm, calendar.monthrange(yy, mm)[1])
        cur = max(first_day, start)
        stop = min(last_day, end)

        # delivery per-day price & mode for this tab
        per_day_delivery, delivery_mode, delivery_details = compute_delivery_per_day_for_rows(client_rows, data)

        service_days_this_month = 0

        while cur <= stop:
            block = date_to_block.get(cur)
            rows = client_rows
            if block is None or not rows:
                cur += timedelta(days=1); continue

            m1=m2=sn=j1=j2=bk=sf=0
            for r in rows:
                row = data[r]
                def cell(ci): return row[ci] if ci < len(row) else ""
                v1 = str(cell(block)).strip()
                v2 = str(cell(block+1)).strip()
                if v1:
                    m1 += 1
                    if norm_name(v1) == "seafood 1": sf += 1
                if v2:
                    m2 += 1
                    if norm_name(v2) == "seafood 2": sf += 1
                if str(cell(block+2)).strip(): sn += 1
                if str(cell(block+3)).strip(): j1 += 1
                if str(cell(block+4)).strip(): j2 += 1
                if str(cell(block+5)).strip(): bk += 1

            if (m1+m2+sn+j1+j2+bk) == 0:
                totals["paused"] += 1
            else:
                service_days_this_month += 1

            totals["meal1"] += m1
            totals["meal2"] += m2
            totals["snack"] += sn
            totals["j1"]    += j1
            totals["j2"]    += j2
            totals["brk"]   += bk
            totals["seafood"] += sf

            cur += timedelta(days=1)

        # accumulate delivery
        delivery_amount_total += per_day_delivery * service_days_this_month
        grand_delivery_days   += service_days_this_month
        diag["delivery_by_month"].append({
            "month": month_name,
            "tab": sheet_title,
            "mode": delivery_mode,
            "per_day": per_day_delivery,
            "service_days": service_days_this_month,
            "row_details": delivery_details
        })

    totals["meals_total"]  = totals["meal1"] + totals["meal2"]
    totals["juices_total"] = totals["j1"] + totals["j2"]
    return totals, grand_delivery_days, delivery_amount_total, diag

# ---------------- UI ----------------
st.set_page_config(page_title="Friska Billing", page_icon="💼", layout="centered")
st.title("🥗 Friska Wellness — Billing System")

session = get_service_account_session()
spid = get_spreadsheet_id(SHEET_URL)

settings = load_settings()
with st.sidebar:
    st.header("⚙️ Prices (saved)")
    c1, c2 = st.columns(2)
    settings["price_nutri"] = c1.number_input("Nutri (₹)", value=float(settings["price_nutri"]), step=5.0)
    settings["price_high_protein"] = c2.number_input("High Protein (₹)", value=float(settings["price_high_protein"]), step=5.0)
    settings["price_seafood_addon"] = st.number_input("Seafood add-on (₹)", value=float(settings["price_seafood_addon"]), step=5.0)

    st.markdown("**Add-on prices**")
    c3, c4, c5 = st.columns(3)
    settings["price_juice"] = c3.number_input("Juice (₹)", value=float(settings["price_juice"]), step=5.0)
    settings["price_snack"] = c4.number_input("Snack (₹)", value=float(settings["price_snack"]), step=5.0)
    settings["price_breakfast"] = c5.number_input("Breakfast (₹)", value=float(settings["price_breakfast"]), step=5.0)

    settings["gst_percent"] = st.number_input("GST % (meals + seafood only)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0)

    debug = st.checkbox("Show debug details")
    if st.button("💾 Save settings", use_container_width=True):
        save_settings(settings)
        st.success("Saved.")

cA, cB = st.columns(2)
client = cA.text_input("Client name")
today = date.today()
start = cB.date_input("Start date", value=today.replace(day=1))
end   = st.date_input("End date", value=today)

if st.button("📊 Fetch Usage & Draft Bill", use_container_width=True):
    if not client.strip():
        st.error("Enter client name.")
    elif end < start:
        st.error("End date must be on/after start date.")
    else:
        try:
            totals, delivery_days, delivery_amount, diag = count_usage(session, spid, start, end, client, debug=debug)
        except Exception as e:
            st.error(f"Processing failed: {e}")
            st.stop()

        if sum([totals[k] for k in ["meals_total","snack","juices_total","brk"]]) == 0 and delivery_days == 0:
            st.warning(
                "No usage found for this client and date range.\n\n"
                "Tips:\n"
                "• Check exact spelling of the client (column B)\n"
                "• Ensure tab is 'clientlist <Month>' or 'clientlist <Mon>'\n"
                "• Row 1 must contain the date in H, then every 6 columns (H, N, T, …)"
            )

        st.subheader("Usage Summary")
        st.write({
            "Meals total": totals["meals_total"],
            "  - Meal1": totals["meal1"],
            "  - Meal2": totals["meal2"],
            "Snacks total": totals["snack"],
            "Juices total": totals["juices_total"],
            "  - Juice1": totals["j1"],
            "  - Juice2": totals["j2"],
            "Breakfast total": totals["brk"],
            "Paused days": totals["paused"],
            "Seafood add-on (count)": totals["seafood"],
            "Service (delivery) days": delivery_days,
        })

        # Billing numbers
        price_meal      = settings["price_nutri"]          # we'll auto-detect plan later
        price_seafood   = settings["price_seafood_addon"]
        price_juice     = settings["price_juice"]
        price_snack     = settings["price_snack"]
        price_breakfast = settings["price_breakfast"]
        gst_pct         = settings["gst_percent"]

        food_amount      = totals["meals_total"]   * price_meal
        seafood_amount   = totals["seafood"]       * price_seafood
        juices_amount    = totals["juices_total"]  * price_juice
        snacks_amount    = totals["snack"]         * price_snack
        breakfast_amount = totals["brk"]           * price_breakfast

        taxable   = food_amount + seafood_amount              # GST only on meals + seafood (for now)
        gst_amt   = round(taxable * (gst_pct/100.0), 2) if gst_pct else 0.0

        grand_total = round(taxable + gst_amt + juices_amount + snacks_amount + breakfast_amount + delivery_amount)

        st.subheader("Draft Bill")
        st.write({
            "Food base":       f"{totals['meals_total']} × ₹{price_meal} = ₹{food_amount:.2f}",
            "Seafood add-on":  f"{totals['seafood']} × ₹{price_seafood} = ₹{seafood_amount:.2f}",
            "GST":             f"₹{gst_amt:.2f} (@ {gst_pct}%)",
            "Juices":          f"{totals['juices_total']} × ₹{price_juice} = ₹{juices_amount:.2f}",
            "Snacks":          f"{totals['snack']} × ₹{price_snack} = ₹{snacks_amount:.2f}",
            "Breakfast":       f"{totals['brk']} × ₹{price_breakfast} = ₹{breakfast_amount:.2f}",
            "Delivery (from sheet)": f"₹{delivery_amount:.2f}",
            "TOTAL":           f"₹ {grand_total}",
        })
        st.success("Draft ready with corrected delivery logic.")

        if debug:
            st.divider()
            st.subheader("🔎 Delivery decision by month")
            st.write(diag.get("delivery_by_month", []))
            st.subheader("🔎 Other debug")
            st.write({k: v for k, v in diag.items() if k != "delivery_by_month"})
