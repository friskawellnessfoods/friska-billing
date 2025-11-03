#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing Web App — Streamlit Cloud + Service Account
# - Debug panel to diagnose tab names, date headers, client rows
# - Counts meals/snacks/juices/breakfast/seafood + paused
# - Column G = delivery price (override used for now)
# - Date blocks from H, 6 columns per date
# =========================================================

import streamlit as st
import os, re, json, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"

# layout constants
DELIVERY_PRICE_COL_IDX = 6   # G
START_DATA_COL_IDX     = 7   # H
COLUMNS_PER_BLOCK      = 6   # Meal1, Meal2, Snack, J1, J2, Breakfast

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "price_nutri": 180.0,
    "price_high_protein": 200.0,
    "price_seafood_addon": 80.0,
    "price_delivery_override": 80.0,
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

# --------- Counting logic (with diagnostics) ----------
def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str, debug: bool=False):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0, paused=0)
    delivery_days = 0
    diag = {"months": [], "tabs_checked": [], "dates_seen": {}, "client_found_rows": {}}

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
            name = norm_name(row[1]) if len(row) > 1 else ""
            if name:
                lut.setdefault(name, []).append(r)
        diag["client_found_rows"][sheet_title] = lut.get(client_key, [])

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
        # store sample of parsed dates
        diag["dates_seen"][sheet_title] = [d.strftime("%d-%b-%y") for d in sorted(date_to_block.keys())[:10]]

        # iterate this month's slice
        first_day = date(yy, mm, 1)
        last_day  = date(yy, mm, calendar.monthrange(yy, mm)[1])
        cur = max(first_day, start)
        stop = min(last_day, end)

        while cur <= stop:
            block = date_to_block.get(cur)
            rows = lut.get(client_key, [])
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
                delivery_days += 1

            totals["meal1"] += m1
            totals["meal2"] += m2
            totals["snack"] += sn
            totals["j1"]    += j1
            totals["j2"]    += j2
            totals["brk"]   += bk
            totals["seafood"] += sf

            cur += timedelta(days=1)

    totals["meals_total"]  = totals["meal1"] + totals["meal2"]
    totals["juices_total"] = totals["j1"] + totals["j2"]
    return totals, delivery_days, diag

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
    settings["gst_percent"] = st.number_input("GST % (food only)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0)
    settings["price_delivery_override"] = st.number_input("Delivery (₹/service day)", value=float(settings["price_delivery_override"]), step=5.0)
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
            totals, delivery_days, diag = count_usage(session, spid, start, end, client, debug=debug)
        except Exception as e:
            st.error(f"Processing failed: {e}")
            st.stop()

        # If nothing found, give helpful hints
        if sum([totals[k] for k in ["meals_total","snack","juices_total","brk"]]) == 0 and delivery_days == 0:
            st.warning(
                "No usage found for this client and date range.\n\n"
                "Tips:\n"
                "• Check the exact spelling of the client (column B)\n"
                "• Make sure the tab is named like 'clientlist November' or 'clientlist Nov'\n"
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

        price_meal     = settings["price_nutri"]      # we’ll auto-detect later
        price_seafood  = settings["price_seafood_addon"]
        gst_pct        = settings["gst_percent"]
        delivery_price = settings["price_delivery_override"]

        food_amount     = totals["meals_total"] * price_meal
        seafood_amount  = totals["seafood"] * price_seafood
        taxable         = food_amount + seafood_amount
        gst_amt         = round(taxable * (gst_pct/100.0), 2) if gst_pct else 0.0
        delivery_amount = delivery_days * delivery_price
        grand_total     = round(taxable + gst_amt + delivery_amount)

        st.subheader("Draft Bill")
        st.write({
            "Food base": f"{totals['meals_total']} × ₹{price_meal} = ₹{food_amount:.2f}",
            "Seafood add-on": f"{totals['seafood']} × ₹{price_seafood} = ₹{seafood_amount:.2f}",
            "GST": f"₹{gst_amt:.2f} (@ {gst_pct}%)",
            "Delivery": f"{delivery_days} × ₹{delivery_price} = ₹{delivery_amount:.2f}",
            "TOTAL": f"₹ {grand_total}",
        })
        st.success("Draft ready. (PDF button comes next.)")

        if debug:
            st.divider()
            st.subheader("🔎 Debug details")
            st.write(diag)
