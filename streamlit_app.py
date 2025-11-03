#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing Web App — Streamlit Cloud + Service Account
# - Counts meals/snacks/juices/breakfast/seafood + delivery
# - Delivery from sheet (col G) with custom shift rules
# - GST settings kept (not used here since Draft Bill removed)
# - Usage Summary:
#     * Meals total only (no Meal1/Meal2 split)
#     * Show Seafood/Snacks/Juices/Breakfast only if > 0
#     * Show Active, Paused, Total days (from headers) and Paused dates list
# - Next Cycle Planner (calendar-based, Mon–Sat only):
#     * First paused_days = Adjustment days (listed)
#     * Next 26 days = New billing window (show only start & end, not the full list)
# =========================================================

import streamlit as st
import re, json, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"

# column indexes
COL_B_CLIENT = 1
COL_C_TYPE   = 2
COL_G_DELIVERY = 6            # G (0-based)
START_DATA_COL_IDX = 7        # H
COLUMNS_PER_BLOCK  = 6        # Meal1, Meal2, Snack, J1, J2, Breakfast

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

# --------- Secrets -> session ----------
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
        st.error("Secrets format error under gcp_credentials.value.")
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

# --------- Delivery pricing logic ----------
def compute_delivery_per_day_for_rows(rows: List[int], data: List[List[str]]):
    """
    Returns (per_day_price, mode, details)
    mode ∈ {"single_identical", "sum_shifts", "single_mismatch", "none"}
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

    has_morning = any("morning" in t for t in norm_types)
    has_evening = any("evening" in t for t in norm_types)

    if all_equal(norm_types):
        per_day = max(prices) if prices else 0.0
        return per_day, "single_identical", details

    if has_morning or has_evening:
        if all(t == "morning delivery" for t in norm_types) or all(t == "evening delivery" for t in norm_types):
            per_day = max(prices) if prices else 0.0
            return per_day, "single_identical", details
        per_day = sum(prices) if prices else 0.0
        return per_day, "sum_shifts", details

    per_day = max(prices) if prices else 0.0
    return per_day, "single_mismatch", details

# --------- Count usage in given range (header-driven) ----------
def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str, debug: bool=False):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0)
    total_days = 0      # header dates present in sheet within range
    active_days = 0
    paused_dates: List[date] = []
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

        # dates on row 1 -> map & collect dates in range for this tab
        row1 = data[0] if data else []
        date_to_block = {}
        header_dates_in_range = []
        c = START_DATA_COL_IDX
        while c < len(row1):
            dt = to_dt(row1[c]) if c < len(row1) else None
            if dt:
                d = dt.date()
                date_to_block[d] = c
                if start <= d <= end:
                    header_dates_in_range.append(d)
            else:
                if date_to_block and (c >= len(row1) or not str(row1[c]).strip()):
                    break
            c += COLUMNS_PER_BLOCK
        diag["dates_seen"][sheet_title] = [d.strftime("%d-%b-%y") for d in sorted(header_dates_in_range)]

        # delivery per-day price & mode for this tab
        per_day_delivery, delivery_mode, delivery_details = compute_delivery_per_day_for_rows(client_rows, data)

        # iterate ONLY header dates that are in range
        service_days_this_month = 0
        for d in header_dates_in_range:
            block = date_to_block.get(d)
            rows = client_rows
            if block is None or not rows:
                continue

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

            if (m1+m2+sn+j1+j2+bk) > 0:
                service_days_this_month += 1
            else:
                paused_dates.append(d)

            totals["meal1"] += m1
            totals["meal2"] += m2
            totals["snack"] += sn
            totals["j1"]    += j1
            totals["j2"]    += j2
            totals["brk"]   += bk
            totals["seafood"] += sf

        # accumulate per month
        total_days += len(header_dates_in_range)
        active_days += service_days_this_month
        delivery_amount_total += per_day_delivery * service_days_this_month
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
    paused_days = max(0, total_days - active_days)  # should match len(paused_dates)
    return totals, active_days, paused_days, total_days, paused_dates, delivery_amount_total, diag

# --------- Calendar-based future dates (skip Sundays) ----------
def next_service_calendar_dates(after_day: date, needed: int) -> List[date]:
    """
    Returns the next `needed` calendar dates after `after_day`,
    skipping Sundays (weekday() == 6). Mon=0 ... Sun=6.
    """
    out: List[date] = []
    cur = after_day + timedelta(days=1)
    while len(out) < needed:
        if cur.weekday() != 6:  # skip Sundays
            out.append(cur)
        cur += timedelta(days=1)
    return out

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

    settings["gst_percent"] = st.number_input("GST % (food only; delivery excluded)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0)

    debug = st.checkbox("Show debug details")
    if st.button("💾 Save settings", use_container_width=True):
        save_settings(settings)
        st.success("Saved.")

cA, cB = st.columns(2)
client = cA.text_input("Client name")
today = date.today()
start = cB.date_input("Previous bill: Start date", value=today.replace(day=1))
end   = st.date_input("Previous bill: End date", value=today)

if st.button("📊 Fetch Usage", use_container_width=True):
    if not client.strip():
        st.error("Enter client name.")
    elif end < start:
        st.error("End date must be on/after start date.")
    else:
        try:
            (totals, active_days, paused_days, total_days,
             paused_dates, delivery_amount, diag) = count_usage(
                 session, spid, start, end, client, debug=debug
             )
        except Exception as e:
            st.error(f"Processing failed: {e}")
            st.stop()

        # ----- Usage Summary (formatted with conditional parts) -----
        st.subheader("Usage Summary")
        lines = []
        lines.append(f"- **Meals total:** {totals['meals_total']}")
        if totals["seafood"] > 0:
            lines.append(f"- **Seafood add-on (count):** {totals['seafood']}")
        if totals["snack"] > 0:
            lines.append(f"- **Snacks total:** {totals['snack']}")
        if totals["juices_total"] > 0:
            lines.append(f"- **Juices total:** {totals['juices_total']} (J1: {totals['j1']}, J2: {totals['j2']})")
        if totals["brk"] > 0:
            lines.append(f"- **Breakfast total:** {totals['brk']}")
        lines.append(f"- **Active days:** {active_days}")
        lines.append(f"- **Paused days:** {paused_days}")
        lines.append(f"- **Total days in range (from headers):** {total_days}")

        st.markdown("\n".join(lines))

        # Paused dates list
        st.markdown("**Paused dates:** " + (", ".join(sorted({d.strftime('%d-%b-%Y') for d in paused_dates})) if paused_dates else "None"))

        # ---------- Next Cycle Planner (calendar-based, skip Sundays) ----------
        st.subheader("Next Cycle Planner (26 days; Sundays excluded)")
        needed_adjust = paused_days
        needed_bill   = 26
        future_needed = needed_adjust + needed_bill

        future_dates = next_service_calendar_dates(end, future_needed)
        adj_dates  = future_dates[:needed_adjust]
        bill_dates = future_dates[needed_adjust:needed_adjust+needed_bill]

        st.write({
            "Previous bill range": f"{start.strftime('%d-%b-%Y')} → {end.strftime('%d-%b-%Y')}",
            "Paused days to adjust": needed_adjust,
            "Adjustment dates": [d.strftime("%d-%b-%Y") for d in adj_dates],
            "New bill (26 days) start": bill_dates[0].strftime("%d-%b-%Y") if bill_dates else "—",
            "New bill (26 days) end": bill_dates[-1].strftime("%d-%b-%Y") if bill_dates else "—",
            # Intentionally NOT listing the 26 dates
        })

        st.success("Usage and next cycle planned.")

        if debug:
            st.divider()
            st.subheader("🔎 Delivery decision by month")
            st.write(diag.get("delivery_by_month", []))
