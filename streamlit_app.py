#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing – Usage + Next Cycle + Admin Panel + BillingCycle write (update/append)
# UI flow:
#   1) Enter Client
#   2) App reads previous cycle from 'BillingCycle' (Client|Start|End)
#   3) Shows Usage Summary + Next Cycle (Mon–Sat; pauses first)
#   4) Admin Panel (right): editable fields for upcoming bill (no PDF yet)
#   5) Save next cycle -> Update latest row or Append new row (toggle)
# =========================================================

import streamlit as st
import re, json, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

# ---------- CONFIG ----------
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"
BILLING_TAB = "BillingCycle"   # must exist with headers: Client | Start | End

# Data tabs layout (clientlist)
COL_B_CLIENT = 1
COL_C_TYPE   = 2
COL_G_DELIVERY = 6
START_DATA_COL_IDX = 7   # H
COLUMNS_PER_BLOCK  = 6   # Meal1, Meal2, Snack, J1, J2, Breakfast

# Need WRITE scope
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
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

# ---------- Secrets / Auth ----------
def get_service_account_session() -> AuthorizedSession:
    try:
        sec = st.secrets["gcp_credentials"]
    except Exception:
        st.error("Secrets missing: Add Service Account JSON under [gcp_credentials].")
        st.stop()
    try:
        sa_info = json.loads(sec["value"]) if isinstance(sec, dict) and "value" in sec else dict(sec)
        creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
        return AuthorizedSession(creds)
    except Exception as e:
        st.error(f"Could not initialize Google credentials: {e}")
        st.stop()

def get_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        st.error("Invalid SHEET_URL.")
        st.stop()
    return m.group(1)

# ---------- Settings I/O ----------
def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

# ---------- Helpers ----------
WEEKDAY_PREFIX = re.compile(
    r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*",
    re.I
)
def to_dt(v) -> Optional[datetime]:
    if isinstance(v, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=float(v))
    s = str(v or "").strip()
    if not s: return None
    s = WEEKDAY_PREFIX.sub("", s)
    for fmt in ("%d-%b-%y","%d-%b-%Y","%d/%m/%Y","%d/%m/%y","%Y-%m-%d","%d %b %Y","%d %b %y"):
        try: return datetime.strptime(s, fmt)
        except: pass
    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s*$", s)
    if m:
        try:
            month = m.group(2).title()
            y = date.today().year
            return datetime.strptime(f"{m.group(1)}-{month}-{y}", "%d-%b-%Y")
        except: return None
    return None

def dtstr(d: date) -> str:
    return d.strftime("%d-%b-%Y")

def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def fetch_values(session: AuthorizedSession, spid: str, a1_range: str) -> List[List[str]]:
    from urllib.parse import quote
    enc = quote(a1_range, safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{enc}"
    r = session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=30)
    if r.status_code == 403:
        st.error("Google Sheets permission denied. Share the file with the service account (Editor).")
        st.stop()
    r.raise_for_status()
    return r.json().get("values", []) or []

def append_values(session: AuthorizedSession, spid: str, sheet: str, rows: List[List[str]]):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{sheet}!A:C:append"
    params = {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
    body = {"values": rows}
    r = session.post(url, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

def update_values(session: AuthorizedSession, spid: str, range_a1: str, rows: List[List[str]]):
    # values.update replaces the given range
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{range_a1}"
    params = {"valueInputOption": "RAW"}
    body = {"range": range_a1, "values": rows, "majorDimension": "ROWS"}
    r = session.put(url, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

# ---------- ClientList helpers ----------
def get_clientlist_sheet_title(session: AuthorizedSession, spid: str, month_full: str) -> Tuple[Optional[str], List[str]]:
    desired1 = f"clientlist {month_full}".lower()
    desired2 = f"clientlist {month_full[:3]}".lower()
    meta = session.get(f"https://sheets.googleapis.com/v4/spreadsheets/{spid}", timeout=30).json()
    titles = [sh["properties"]["title"] for sh in meta.get("sheets", [])]
    for t in titles:
        tl = t.lower().strip()
        if tl == desired1 or tl == desired2: return t, titles
    for t in titles:
        if t.lower().startswith("clientlist "): return t, titles
    return None, titles

def month_span_inclusive(a: date, b: date) -> List[Tuple[int, int]]:
    out = []; y, m = a.year, a.month
    while (y < b.year) or (y == b.year and m <= b.month):
        out.append((y, m))
        if m == 12: y += 1; m = 1
        else: m += 1
    return out

def parse_float(x) -> float:
    try:
        s = "" if x is None else str(x).strip()
        num = re.sub(r"[^0-9\.\-]", "", s)
        return float(num) if num else 0.0
    except: return 0.0

# ---------- Delivery pricing ----------
def compute_delivery_per_day_for_rows(rows: List[int], data: List[List[str]]):
    if not rows:
        return 0.0, "none", []
    types, prices = [], []
    for r in rows:
        row = data[r] if r < len(data) else []
        typ = str(row[COL_C_TYPE]).strip() if len(row) > COL_C_TYPE else ""
        price = parse_float(row[COL_G_DELIVERY] if len(row) > COL_G_DELIVERY else "")
        types.append(norm_name(typ)); prices.append(price)
    has_morning = any("morning" in t for t in types)
    has_evening = any("evening" in t for t in types)
    all_equal = len(set(types)) == 1 if types else False
    if all_equal:
        return (max(prices) if prices else 0.0), "single_identical", []
    if has_morning or has_evening:
        if all(t == "morning delivery" for t in types) or all(t == "evening delivery" for t in types):
            return (max(prices) if prices else 0.0), "single_identical", []
        return (sum(prices) if prices else 0.0), "sum_shifts", []
    return (max(prices) if prices else 0.0), "single_mismatch", []

# ---------- Usage counting ----------
def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0)
    total_days = 0
    active_days = 0
    paused_dates: List[date] = []
    delivery_amount_total = 0.0
    last_per_day_delivery = 0.0

    for (yy, mm) in month_span_inclusive(start, end):
        month_name = calendar.month_name[mm]
        sheet_title, _ = get_clientlist_sheet_title(session, spid, month_name)
        if not sheet_title: continue
        data = fetch_values(session, spid, f"{sheet_title}!A1:ZZ2000")
        if not data: continue

        # client rows
        rows_by_client = {}
        for r, row in enumerate(data):
            nm = norm_name(row[COL_B_CLIENT]) if len(row) > COL_B_CLIENT else ""
            if nm: rows_by_client.setdefault(nm, []).append(r)
        client_rows = rows_by_client.get(client_key, [])

        # header dates in range
        row1 = data[0] if data else []
        date_to_block = {}; header_dates = []
        c = START_DATA_COL_IDX
        while c < len(row1):
            dt = to_dt(row1[c]) if c < len(row1) else None
            if dt:
                d = dt.date()
                date_to_block[d] = c
                if start <= d <= end:
                    header_dates.append(d)
            else:
                if date_to_block and (c >= len(row1) or not str(row1[c]).strip()): break
            c += COLUMNS_PER_BLOCK

        # delivery per-day price for this month
        per_day_delivery, _, _ = compute_delivery_per_day_for_rows(client_rows, data)
        last_per_day_delivery = per_day_delivery or last_per_day_delivery

        # iterate days
        service_days_this_month = 0
        for d in header_dates:
            block = date_to_block.get(d)
            if block is None or not client_rows: continue
            m1=m2=sn=j1=j2=bk=sf=0
            for r in client_rows:
                row = data[r]
                def cell(ci): return row[ci] if ci < len(row) else ""
                v1 = str(cell(block)).strip(); v2 = str(cell(block+1)).strip()
                if v1: m1 += 1;  sf += 1 if norm_name(v1)=="seafood 1" else 0
                if v2: m2 += 1;  sf += 1 if norm_name(v2)=="seafood 2" else 0
                if str(cell(block+2)).strip(): sn += 1
                if str(cell(block+3)).strip(): j1 += 1
                if str(cell(block+4)).strip(): j2 += 1
                if str(cell(block+5)).strip(): bk += 1
            if (m1+m2+sn+j1+j2+bk) > 0: service_days_this_month += 1
            else: paused_dates.append(d)
            totals["meal1"]+=m1; totals["meal2"]+=m2; totals["snack"]+=sn
            totals["j1"]+=j1; totals["j2"]+=j2; totals["brk"]+=bk; totals["seafood"]+=sf

        total_days += len(header_dates)
        active_days += service_days_this_month
        delivery_amount_total += per_day_delivery * service_days_this_month

    totals["meals_total"]  = totals["meal1"] + totals["meal2"]
    totals["juices_total"] = totals["j1"] + totals["j2"]
    paused_days = max(0, total_days - active_days)
    return totals, active_days, paused_days, total_days, paused_dates, delivery_amount_total, last_per_day_delivery

# ---------- Next dates (Mon–Sat only) ----------
def next_service_calendar_dates(after_day: date, needed: int) -> List[date]:
    out: List[date] = []
    cur = after_day + timedelta(days=1)
    while len(out) < needed:
        if cur.weekday() != 6:  # Sunday=6
            out.append(cur)
        cur += timedelta(days=1)
    return out

# ---------- BillingCycle I/O ----------
def get_prev_cycle_for_client(session: AuthorizedSession, spid: str, client_name: str) -> Tuple[date, date, int]:
    vals = fetch_values(session, spid, f"{BILLING_TAB}!A1:C10000")
    if not vals or len(vals) < 2:
        raise RuntimeError(f"'{BILLING_TAB}' has no data. Create headers 'Client | Start | End' and at least one row.")

    headers = [x.strip().lower() for x in vals[0]]
    try:
        ci = headers.index("client"); si = headers.index("start"); ei = headers.index("end")
    except ValueError:
        raise RuntimeError(f"'{BILLING_TAB}' must have headers: Client | Start | End in row 1.")

    key = norm_name(client_name)
    last_row = None
    last_row_index = None  # 0-based within vals
    for idx, r in enumerate(vals[1:], start=1):
        if len(r) <= max(ci, si, ei): continue
        if norm_name(r[ci]) == key:
            last_row = r
            last_row_index = idx

    if last_row is None:
        raise RuntimeError(f"No row found in '{BILLING_TAB}' for client '{client_name}'. Add one: Client | Start | End.")

    sd = to_dt(last_row[si]); ed = to_dt(last_row[ei])
    if not sd or not ed:
        raise RuntimeError(f"Invalid Start/End for '{client_name}' in '{BILLING_TAB}'. Use dates like 02-Nov-2025.")
    # Convert to 1-based sheet row number
    sheet_row_number = last_row_index + 1  # since vals[0] is header (row 1 in sheet)
    return sd.date(), ed.date(), sheet_row_number

def save_next_cycle_update_or_append(session: AuthorizedSession, spid: str, client_name: str,
                                     next_start: date, next_end: date,
                                     mode: str, last_row_number: Optional[int]):
    # mode: "update" or "append"
    if mode == "update" and last_row_number:
        # Update row N (A..C)
        range_a1 = f"{BILLING_TAB}!A{last_row_number+1}:C{last_row_number+1}"
        rows = [[client_name, dtstr(next_start), dtstr(next_end)]]
        return update_values(session, spid, range_a1, rows)
    else:
        row = [client_name, dtstr(next_start), dtstr(next_end)]
        return append_values(session, spid, BILLING_TAB, [row])

# ---------------- UI ----------------
st.set_page_config(page_title="Friska Billing", page_icon="💼", layout="wide")
st.title("🥗 Friska Wellness — Billing System")

session = get_service_account_session()
spid = get_spreadsheet_id(SHEET_URL)

# ---------- LEFT: Prices console ----------
settings = load_settings()
with st.sidebar:
    st.header("⚙️ Prices (saved)")
    c1, c2 = st.columns(2)
    settings["price_nutri"] = c1.number_input("Nutri (₹)", value=float(settings["price_nutri"]), step=5.0)
    settings["price_high_protein"] = c2.number_input("High Protein (₹)", value=float(settings["price_high_protein"]), step=5.0)
    settings["price_seafood_addon"] = st.number_input("Seafood add-on (₹)", value=float(settings["price_seafood_addon"]), step=5.0)

    st.markdown("**Add-ons**")
    c3, c4, c5 = st.columns(3)
    settings["price_juice"] = c3.number_input("Juice (₹)", value=float(settings["price_juice"]), step=5.0)
    settings["price_snack"] = c4.number_input("Snack (₹)", value=float(settings["price_snack"]), step=5.0)
    settings["price_breakfast"] = c5.number_input("Breakfast (₹)", value=float(settings["price_breakfast"]), step=5.0)

    settings["gst_percent"] = st.number_input("GST % (food only; delivery excluded)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0)

    if st.button("💾 Save settings", use_container_width=True):
        save_settings(settings)
        st.success("Saved.")

# ---------- MAIN: client input + fetch ----------
cA, cB = st.columns([2, 1])
client = cA.text_input("Client name (must exist in BillingCycle)")
save_mode = cB.selectbox("Save mode", ["Update latest row", "Append new row"])

fetch_btn = st.button("📊 Fetch Usage & Plan", use_container_width=True)

# Placeholders to share between sections
next_start = None
next_end = None
last_row_number = None
autodetected_plan = "Nutri"  # default fallback

if fetch_btn:
    if not client.strip():
        st.error("Enter client name.")
        st.stop()

    try:
        prev_start, prev_end, last_row_number = get_prev_cycle_for_client(session, spid, client)
    except Exception as e:
        st.error(str(e)); st.stop()

    totals, active_days, paused_days, total_days, paused_dates, _delivery_amount, last_per_day_delivery = count_usage(
        session, spid, prev_start, prev_end, client
    )

    # ----- Auto-detect plan from last cycle Type signal -----
    # Simple heuristic: if seafood count>0 or meal counts exist, we don't actually know plan; we keep default.
    # If you want strict detection from column C per active day (majority voting), we can add; for now keep manual override in Admin panel with a preview guess.
    autodetected_plan = "High Protein" if settings.get("price_high_protein", 0) > settings.get("price_nutri", 0) and totals["meals_total"] else "Nutri"

    # ----- Usage Summary -----
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
    lines.append(f"- **Total days:** {total_days}")
    st.markdown("\n".join(lines))
    st.markdown("**Paused dates:** " + (", ".join(sorted({d.strftime('%d-%b-%Y') for d in paused_dates})) if paused_dates else "None"))

    # ----- Next Cycle Planner -----
    st.subheader("Next Cycle Planner")
    needed_adjust = paused_days
    needed_bill   = 26
    future_needed = needed_adjust + needed_bill
    future_dates  = next_service_calendar_dates(prev_end, future_needed)
    adj_dates     = future_dates[:needed_adjust]
    bill_dates    = future_dates[needed_adjust:needed_adjust+needed_bill]

    next_start = bill_dates[0] if bill_dates else None
    next_end   = bill_dates[-1] if bill_dates else None

    nl = []
    nl.append(f"- **Previous bill range:** {dtstr(prev_start)} → {dtstr(prev_end)}")
    nl.append(f"- **Paused days to adjust:** {needed_adjust}")
    nl.append(f"- **Adjustment dates:** " + (", ".join(dtstr(d) for d in adj_dates) if adj_dates else "None"))
    nl.append(f"- **New bill start:** {dtstr(next_start) if next_start else '—'}")
    nl.append(f"- **New bill end:** {dtstr(next_end) if next_end else '—'}")
    st.markdown("\n".join(nl))

    st.info(f"Per-day delivery (from last cycle logic): ₹{last_per_day_delivery:.2f} (editable in Admin panel)")

    # ----- Save next cycle buttons -----
    if next_start and next_end:
        colx, coly = st.columns(2)
        if colx.button("✅ Save next cycle to BillingCycle", use_container_width=True):
            try:
                mode = "update" if save_mode == "Update latest row" else "append"
                save_next_cycle_update_or_append(session, spid, client, next_start, next_end, mode, last_row_number)
                st.success(f"Saved ({mode}) in '{BILLING_TAB}': {client} | {dtstr(next_start)} → {dtstr(next_end)}")
            except Exception as e:
                st.error(f"Failed to save: {e}")

# ---------- RIGHT: Admin panel (sticky) ----------
st.markdown("---")
st.subheader("🛠️ Admin — Upcoming Bill Overrides (Preview Only)")
with st.container():
    c1, c2, c3 = st.columns([2,1,1])
    admin_client_label = c1.text_input("Client label (print as)", value=client or "")
    admin_billing_date = c2.date_input("Billing date", value=date.today())
    admin_plan = c3.selectbox("Plan", ["Nutri", "High Protein"], index=(0 if (autodetected_plan=="Nutri") else 1))

    c4, c5 = st.columns(2)
    admin_bill_start = c4.text_input("Bill start (dd-MMM-YYYY)", value=(dtstr(next_start) if next_start else ""))
    admin_bill_end   = c5.text_input("Bill end (dd-MMM-YYYY)",   value=(dtstr(next_end) if next_end else ""))

    st.markdown("**Qty / Rate (defaults)**")
    q1, r1 = st.columns(2)
    meals_qty = q1.number_input("Meals qty", value=26, step=1, min_value=0)
    meals_rate = r1.number_input("Meals rate (₹)", value=float(settings["price_high_protein"] if admin_plan=="High Protein" else settings["price_nutri"]), step=5.0)

    q2, r2 = st.columns(2)
    seafood_qty = q2.number_input("Seafood qty", value=26, step=1, min_value=0)
    seafood_rate = r2.number_input("Seafood rate (₹)", value=float(settings["price_seafood_addon"]), step=5.0)

    q3, r3 = st.columns(2)
    juice_qty = q3.number_input("Juice qty", value=26, step=1, min_value=0)
    juice_rate = r3.number_input("Juice rate (₹)", value=float(settings["price_juice"]), step=5.0)

    q4, r4 = st.columns(2)
    snack_qty = q4.number_input("Snack qty", value=26, step=1, min_value=0)
    snack_rate = r4.number_input("Snack rate (₹)", value=float(settings["price_snack"]), step=5.0)

    q5, r5 = st.columns(2)
    breakfast_qty = q5.number_input("Breakfast qty", value=26, step=1, min_value=0)
    breakfast_rate = r5.number_input("Breakfast rate (₹)", value=float(settings["price_breakfast"]), step=5.0)

    q6, r6 = st.columns(2)
    delivery_days = q6.number_input("Delivery days", value=26, step=1, min_value=0)
    delivery_per_day = r6.number_input("Delivery per day (₹)", value=0.0, step=5.0, help="Prefill from last cycle shown above")

    gst_pct = st.number_input("GST % (food items only)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0)

    st.caption("Preview actions coming next: Generate on-page invoice image (for screenshot) and optional PDF + log.")
