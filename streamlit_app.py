#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#########################################################
#  Friska Billing Web App (Streamlit Cloud + Service Account)
#  - Uses Streamlit Secrets (service account JSON in TOML)
#  - Reads "clientlist <month>" tabs from your sheet
#  - Counts meals, snacks, juices, breakfast, paused, seafood
#  - Column G = Delivery price per client
#  - Date blocks start at H, 6 columns per date
#  - Sidebar: prices (saved to settings.json on Streamlit)
#  - Outputs draft bill amounts (PDF hook can be added later)
#########################################################

import streamlit as st
import os, re, json, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

#########################################################
# EDIT THIS: your sheet URL
#########################################################
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"

# Settings file (persists on Streamlit Cloud)
SETTINGS_FILE = "settings.json"

# Sheet layout
DELIVERY_PRICE_COL_IDX = 6   # G = zero-based 6
START_DATA_COL_IDX     = 7   # H = zero-based 7
COLUMNS_PER_BLOCK      = 6   # Meal1, Meal2, Snack, J1, J2, Breakfast

# Google scopes (read-only)
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# Month helpers and date parsing
MONTHS = {
    'jan':1,'january':1,'feb':2,'february':2,'mar':3,'march':3,'apr':4,'april':4,
    'may':5,'jun':6,'june':6,'jul':7,'july':7,'aug':8,'august':8,'sep':9,'sept':9,'september':9,
    'oct':10,'october':10,'nov':11,'november':11,'dec':12,'december':12,
}
WEEKDAY_PREFIX = re.compile(r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*", re.I)

#########################################################
# Settings persistence
#########################################################
DEFAULT_SETTINGS = {
    "price_nutri": 180.0,
    "price_high_protein": 200.0,
    "price_seafood_addon": 80.0,
    "price_delivery_override": None,   # if None, per-client price from column G
    "gst_percent": 5.0
}

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
    except:
        return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

#########################################################
# Google auth via Service Account (from Secrets)
#########################################################
def get_service_account_session() -> AuthorizedSession:
    # Read the JSON string you saved in Secrets at:
    # [gcp_credentials]
    # value = """ { ...FULL JSON... } """
    sa_json_str = st.secrets["gcp_credentials"]["value"]
    sa_info = json.loads(sa_json_str)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return AuthorizedSession(creds)

def get_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        st.error("Invalid SHEET_URL. Paste the full sheet link.")
        st.stop()
    return m.group(1)

def fetch_values(session: AuthorizedSession, spid: str, a1_range: str) -> List[List[str]]:
    from urllib.parse import quote
    enc = quote(a1_range, safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{enc}"
    r = session.get(url, params={"valueRenderOption":"UNFORMATTED_VALUE"})
    r.raise_for_status()
    return r.json().get("values", []) or []

#########################################################
# Sheet helpers
#########################################################
def to_dt(v) -> Optional[datetime]:
    if isinstance(v, (int, float)):
        # Excel serial date
        base = datetime(1899, 12, 30)
        return base + timedelta(days=float(v))
    s = str(v or "").strip()
    if not s:
        return None
    s = WEEKDAY_PREFIX.sub("", s)
    # try a few formats
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d %b %Y", "%d %b %y"):
        try:
            return datetime.strptime(s, fmt)
        except:
            pass
    # final attempt: "2 Nov" current year
    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s*$", s)
    if m and m.group(2).lower() in MONTHS:
        y = date.today().year
        return datetime(y, MONTHS[m.group(2).lower()], int(m.group(1)))
    return None

def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def get_clientlist_sheet_title(session: AuthorizedSession, spid: str, month_full: str) -> Optional[str]:
    # Try “clientlist <Full>” then “clientlist <Abbr>”
    desired1 = f"clientlist {month_full}".lower()
    desired2 = f"clientlist {month_full[:3]}".lower()

    meta = session.get(f"https://sheets.googleapis.com/v4/spreadsheets/{spid}").json()
    titles = [sh["properties"]["title"] for sh in meta.get("sheets", [])]

    for t in titles:
        tl = t.lower().strip()
        if tl == desired1 or tl == desired2:
            return t
    # fallback: any sheet starting with clientlist
    for t in titles:
        if t.lower().startswith("clientlist"):
            return t
    return None

def month_span_inclusive(a: date, b: date) -> List[Tuple[int,int]]:
    out = []
    y, m = a.year, a.month
    while (y < b.year) or (y == b.year and m <= b.month):
        out.append((y, m))
        m = 1 if m == 12 else m + 1
        if m == 1: y += 1
    return out

#########################################################
# Counting logic
#########################################################
def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0, paused=0)
    delivery_days = 0

    for (yy, mm) in month_span_inclusive(start, end):
        sheet_title = get_clientlist_sheet_title(session, spid, calendar.month_name[mm])
        if not sheet_title:
            continue

        data = fetch_values(session, spid, f"{sheet_title}!A1:ZZ2000")
        if not data:
            continue

        # build client rows map
        lut = {}
        for r, row in enumerate(data):
            name = norm_name(row[1]) if len(row) > 1 else ""
            if name:
                lut.setdefault(name, []).append(r)

        # row 1 dates mapping (H onwards, every 6 columns)
        row1 = data[0] if data else []
        date_to_block = {}
        c = START_DATA_COL_IDX
        while c < len(row1):
            dt = to_dt(row1[c]) if c < len(row1) else None
            if dt:
                date_to_block[dt.date()] = c
            else:
                # stop if we already saw dates and now it's blank
                if date_to_block and (c >= len(row1) or not str(row1[c]).strip()):
                    break
            c += COLUMNS_PER_BLOCK

        # iterate dates within this month
        cur = date(yy, mm, 1)
        # month end:
        last_day = calendar.monthrange(yy, mm)[1]
        month_end = date(yy, mm, last_day)
        # clamp to [start, end]
        cur = max(cur, start)
        stop = min(month_end, end)

        while cur <= stop:
            block = date_to_block.get(cur)
            if block is None:
                cur += timedelta(days=1); continue

            rows = lut.get(client_key, [])
            if not rows:
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

    totals["meals_total"] = totals["meal1"] + totals["meal2"]
    totals["juices_total"] = totals["j1"] + totals["j2"]
    return totals, delivery_days

#########################################################
# UI
#########################################################
st.set_page_config(page_title="Friska Billing", page_icon="💼", layout="centered")
st.title("🥗 Friska Wellness — Billing System")

# Auth session + spreadsheet id
session = get_service_account_session()
spid = get_spreadsheet_id(SHEET_URL)

# Sidebar settings
settings = load_settings()
with st.sidebar:
    st.header("⚙️ Prices (saved)")
    c1, c2 = st.columns(2)
    settings["price_nutri"] = c1.number_input("Nutri (₹)", value=float(settings["price_nutri"]), step=5.0)
    settings["price_high_protein"] = c2.number_input("High Protein (₹)", value=float(settings["price_high_protein"]), step=5.0)
    settings["price_seafood_addon"] = st.number_input("Seafood add-on (₹)", value=float(settings["price_seafood_addon"]), step=5.0)
    settings["gst_percent"] = st.number_input("GST % (food only)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0)

    override = st.checkbox("Override delivery price (else use column G)?", value=(settings["price_delivery_override"] is not None))
    if override:
        settings["price_delivery_override"] = st.number_input("Delivery (₹/service day)", value=float(settings["price_delivery_override"] or 80.0), step=5.0)
    else:
        settings["price_delivery_override"] = None

    if st.button("💾 Save settings", use_container_width=True):
        save_settings(settings)
        st.success("Saved.")

# Main form
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
        totals, delivery_days = count_usage(session, spid, start, end, client)

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

        # For now, default plan price = Nutri (we can auto-detect later from column C)
        price_meal = settings["price_nutri"]
        price_seafood = settings["price_seafood_addon"]
        gst_pct = settings["gst_percent"]
        delivery_price = settings["price_delivery_override"] or 0.0

        food_amount = totals["meals_total"] * price_meal
        seafood_amount = totals["seafood"] * price_seafood
        taxable = food_amount + seafood_amount
        gst_amt = round(taxable * (gst_pct/100.0), 2) if gst_pct else 0.0
        delivery_amount = delivery_days * delivery_price
        grand_total = round(taxable + gst_amt + delivery_amount)

        st.subheader("Draft Bill")
        st.write({
            "Food base": f"{totals['meals_total']} × ₹{price_meal} = ₹{food_amount:.2f}",
            "Seafood add-on": f"{totals['seafood']} × ₹{price_seafood} = ₹{seafood_amount:.2f}",
            "GST": f"₹{gst_amt:.2f} (@ {gst_pct}%)",
            "Delivery": f"{delivery_days} × ₹{delivery_price} = ₹{delivery_amount:.2f}",
            "TOTAL": f"₹ {grand_total}",
        })
        st.success("Draft ready. Next, we can add a button to generate the PDF invoice.")
