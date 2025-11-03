#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing Web App — Simple BillingCycle tab workflow
# UI: enter Client only
# Reads previous Start/End from Sheet:'BillingCycle' (Client|Start|End)
# Computes Usage Summary + Next Cycle (Mon–Sat; paused days first)
# Button "Save next cycle" appends Client|NextStart|NextEnd to BillingCycle
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

# Data tabs layout (already in your file)
COL_B_CLIENT = 1
COL_C_TYPE   = 2
COL_G_DELIVERY = 6
START_DATA_COL_IDX = 7   # H
COLUMNS_PER_BLOCK  = 6   # Meal1, Meal2, Snack, J1, J2, Breakfast

# Need WRITE scope now (to append next cycle)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ---------- Secrets / Auth ----------
def get_service_account_session() -> AuthorizedSession:
    try:
        sec = st.secrets["gcp_credentials"]
    except Exception:
        st.error("Secrets missing: Add your Service Account JSON under [gcp_credentials].")
        st.stop()
    try:
        sa_info = json.loads(sec["value"]) if isinstance(sec, dict) and "value" in sec else dict(sec)
        creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
        return AuthorizedSession(creds)
    except Exception as e:
        st.error(f"Could not initialize Google credentials: {e}")
        st.stop()

# ---------- Helpers ----------
WEEKDAY_PREFIX = re.compile(
    r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*", re.I
)
def get_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        st.error("Invalid SHEET_URL.")
        st.stop()
    return m.group(1)

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
        st.error("Permission denied by Google Sheets API. Share the Sheet with the service account (Viewer/Editor).")
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
    types, prices, details = [], [], []
    for r in rows:
        row = data[r] if r < len(data) else []
        typ = str(row[COL_C_TYPE]).strip() if len(row) > COL_C_TYPE else ""
        price = parse_float(row[COL_G_DELIVERY] if len(row) > COL_G_DELIVERY else "")
        types.append(typ); prices.append(price)
        details.append({"row": str(r+1), "type": typ, "price": f"{price:.2f}"})
    norm_types = [norm_name(t) for t in types]
    def all_equal(vals): return len(vals)>0 and len(set(vals))==1
    has_morning = any("morning" in t for t in norm_types)
    has_evening = any("evening" in t for t in norm_types)
    if all_equal(norm_types): return (max(prices) if prices else 0.0), "single_identical", details
    if has_morning or has_evening:
        if all(t == "morning delivery" for t in norm_types) or all(t == "evening delivery" for t in norm_types):
            return (max(prices) if prices else 0.0), "single_identical", details
        return (sum(prices) if prices else 0.0), "sum_shifts", details
    return (max(prices) if prices else 0.0), "single_mismatch", details

# ---------- Usage counting ----------
def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0)
    total_days = 0
    active_days = 0
    paused_dates: List[date] = []
    delivery_amount_total = 0.0

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
    return totals, active_days, paused_days, total_days, paused_dates, delivery_amount_total

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
def get_prev_cycle_for_client(session: AuthorizedSession, spid: str, client_name: str) -> Tuple[date, date]:
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
    for r in vals[1:]:
        if len(r) <= max(ci, si, ei): continue
        if norm_name(r[ci]) == key:
            last_row = r

    if not last_row:
        raise RuntimeError(f"No row found in '{BILLING_TAB}' for client '{client_name}'. Add one: Client | Start | End.")

    sd = to_dt(last_row[si]); ed = to_dt(last_row[ei])
    if not sd or not ed:
        raise RuntimeError(f"Invalid Start/End for '{client_name}' in '{BILLING_TAB}'. Use dates like 02-Nov-2025.")
    return sd.date(), ed.date()

def save_next_cycle(session: AuthorizedSession, spid: str, client_name: str, next_start: date, next_end: date):
    row = [client_name, dtstr(next_start), dtstr(next_end)]
    return append_values(session, spid, BILLING_TAB, [row])

# ---------------- UI ----------------
st.set_page_config(page_title="Friska Billing", page_icon="💼", layout="centered")
st.title("🥗 Friska Wellness — Billing System")

session = get_service_account_session()
spid = get_spreadsheet_id(SHEET_URL)

with st.sidebar:
    st.header("How it works")
    st.write(
        "1) Enter **Client**\n\n"
        "2) App reads **Start/End** from `BillingCycle` tab\n\n"
        "3) Shows **Usage Summary** + **Next Cycle**\n\n"
        "4) Click **Save next cycle** to append new dates to `BillingCycle`"
    )

client = st.text_input("Client name (must exist in BillingCycle)")

if st.button("📊 Fetch Usage & Plan", use_container_width=True):
    if not client.strip():
        st.error("Enter client name."); st.stop()

    try:
        prev_start, prev_end = get_prev_cycle_for_client(session, spid, client)
    except Exception as e:
        st.error(str(e)); st.stop()

    totals, active_days, paused_days, total_days, paused_dates, _ = count_usage(
        session, spid, prev_start, prev_end, client
    )

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

    # ----- Save next cycle -----
    if next_start and next_end:
        if st.button("✅ Save next cycle to BillingCycle", use_container_width=True):
            try:
                save_next_cycle(session, spid, client, next_start, next_end)
                st.success(f"Saved to '{BILLING_TAB}': {client} | {dtstr(next_start)} → {dtstr(next_end)}")
            except Exception as e:
                st.error(f"Failed to save: {e}")
