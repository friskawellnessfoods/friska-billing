#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#########################################################
#  Friska Billing Web App (Streamlit Cloud Version)
#  - Secure: uses Streamlit Secrets for Google creds
#  - Reads Google Sheets (clientlist tabs)
#  - Counts meals, snacks, juices, breakfast, seafood
#  - Supports cross-month ranges
#  - Per-client delivery cost from column G
#  - Price settings saved locally (as json)
#  - Generates draft billing breakdown
#########################################################

import streamlit as st
import os, re, json, pickle, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.transport.requests import AuthorizedSession
import requests

#########################################################
# SET THIS TO YOUR SHEET URL!!!
#########################################################
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"

# Where to store Google token
CREDS_PATH = "/tmp/credentials.json"
TOKEN_PATH = "/tmp/token.pickle"

# Billing settings file (Streamlit cloud persistent)
SETTINGS_FILE = "settings.json"

# Google scopes (READ ONLY!)
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

#########################################################
# SHEET LAYOUT SETTINGS
#########################################################
# Column G = delivery price
DELIVERY_PRICE_COL = "G"
DELIVERY_PRICE_COL_IDX = 6  # Zero-based index (A=0)

# Date columns start from **H**
START_DATA_COL_LETTER = "H"
START_DATA_COL_IDX = 7  # zero-based index (A=0) so H=7

COLUMNS_PER_BLOCK = 6  # each date has 6 meal columns

# Accepted month formats
MONTHS = {
    'jan':1,'january':1,'feb':2,'february':2,'mar':3,'march':3,'apr':4,'april':4,
    'may':5,'jun':6,'june':6,'jul':7,'july':7,'aug':8,'august':8,'sep':9,'sept':9,'september':9,
    'oct':10,'october':10,'nov':11,'november':11,'dec':12,'december':12
}

WEEKDAY_PREFIX = re.compile(r"^\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*", re.I)

#########################################################
# LOAD / SAVE BILLING SETTINGS
#########################################################
DEFAULT_SETTINGS = {
    "price_nutri": 180.0,
    "price_high_protein": 200.0,
    "price_seafood_addon": 80.0,
    "price_delivery_override": None,
    "gst_percent": 5.0
}

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
    except:
        return DEFAULT_SETTINGS.copy()

def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

settings = load_settings()

#########################################################
# GOOGLE AUTH FUNCTIONS
#########################################################
def write_creds_from_secrets():
    """Writes Google JSON creds from Streamlit Secrets to /tmp file on cloud"""
    creds_json = st.secrets["gcp_credentials"]
    with open(CREDS_PATH, "w") as f:
        f.write(creds_json)

def get_creds():
    write_creds_from_secrets()

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "rb") as f:
                creds = pickle.load(f)
        except:
            creds = None

    if creds and getattr(creds, "expired", False) and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, "wb") as f:
                pickle.dump(creds, f)
            return creds
        except:
            creds = None

    if not creds:
        flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    return creds

def get_spreadsheet_id(url):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        st.error("Invalid sheet URL")
        st.stop()
    return m.group(1)

#########################################################
# SHEET HELPERS
#########################################################
def normalize_date(input_date: str) -> str:
    s = input_date.strip()
    fmts = ["%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"]
    for f in fmts:
        try:
            return datetime.strptime(s, f).strftime("%d-%b-%y")
        except:
            pass
    # Try smart parser (e.g. "2 Nov")
    parts = s.split()
    if len(parts) == 2:
        d, m = parts
        m = m.lower()
        if m in MONTHS:
            y = datetime.now().year
            dt = datetime(y, MONTHS[m], int(d))
            return dt.strftime("%d-%b-%y")
    raise ValueError(f"Cannot parse date: {s}")

def to_dt(v) -> Optional[datetime]:
    if isinstance(v, (int,float)):
        return datetime(1899,12,30) + timedelta(days=v)
    s=str(v).strip()
    if not s: return None
    s = WEEKDAY_PREFIX.sub("", s)
    try:
        return datetime.strptime(normalize_date(s), "%d-%b-%y")
    except:
        return None

def col_letter_to_idx(col):
    n=0
    for c in col:
        n = n * 26 + (ord(c.upper())-64)
    return n-1

def idx_to_col_letter(i):
    s=""
    i+=1
    while i>0:
        i,r = divmod(i-1, 26)
        s = chr(65+r) + s
    return s

def fetch_values(session: AuthorizedSession, spid, rng):
    from urllib.parse import quote
    rng_enc = quote(rng, safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{rng_enc}"
    r = session.get(url, params={"valueRenderOption":"UNFORMATTED_VALUE"})
    r.raise_for_status()
    return r.json().get("values",[])

def get_clientlist_sheet(session, spid, month):
    desired1 = f"clientlist {month}".lower().strip()
    desired2 = f"clientlist {month[:3]}".lower().strip()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}"
    meta = session.get(url).json()
    for sh in meta.get("sheets",[]):
        title = sh["properties"]["title"]
        t = title.lower().strip()
        if t == desired1 or t == desired2:
            return title
    # fallback: any sheet starting clientlist
    for sh in meta.get("sheets",[]):
        title = sh["properties"]["title"]
        if title.lower().startswith("clientlist"):
            return title
    return None

#########################################################
# MEAL COUNT LOGIC
#########################################################
def date_range(a,b):
    cur = a
    while cur<=b:
        yield cur
        cur += timedelta(days=1)

def count_usage(session, spid, start, end, client):
    client_key = re.sub(r"\s+"," ", client.lower().strip())
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0, paused=0)
    delivery_days = 0

    # iterate date blocks
    cur = start
    while cur <= end:
        y,m = cur.year, cur.month
        sheet = get_clientlist_sheet(session, spid, calendar.month_name[m])
        if not sheet:
            cur += timedelta(days=1)
            continue

        # Load rows
        data = fetch_values(session, spid, f"{sheet}!A1:ZZ2000")

        # Find matching client rows
        client_rows = []
        for r, row in enumerate(data):
            if len(row)>1 and row[1] and re.sub(r"\s+"," ", row[1].lower().strip()) == client_key:
                client_rows.append(r)

        if not client_rows:
            cur += timedelta(days=1)
            continue

        # Find the column block for the date (check row 1)
        row1 = data[0] if data else []
        col = START_DATA_COL_IDX
        found_block = None
        while col < len(row1):
            dt = to_dt(row1[col]) if col < len(row1) else None
            if dt and dt.date() == cur:
                found_block = col
                break
            col += COLUMNS_PER_BLOCK
        if found_block is None:
            cur += timedelta(days=1)
            continue

        m1=m2=sn=j1=j2=bk=sf=0
        for r in client_rows:
            row = data[r]
            def cell(c): return row[c] if c < len(row) else ""

            if str(cell(found_block)).strip():
                m1+=1
                if re.sub(r"\s+"," ",str(cell(found_block)).lower().strip()) == "seafood 1":
                    sf+=1

            if str(cell(found_block+1)).strip():
                m2+=1
                if re.sub(r"\s+"," ",str(cell(found_block+1)).lower().strip()) == "seafood 2":
                    sf+=1

            if str(cell(found_block+2)).strip(): sn+=1
            if str(cell(found_block+3)).strip(): j1+=1
            if str(cell(found_block+4)).strip(): j2+=1
            if str(cell(found_block+5)).strip(): bk+=1

        if (m1+m2+sn+j1+j2+bk)==0:
            totals["paused"] += 1
        else:
            delivery_days += 1

        totals["meal1"]+=m1
        totals["meal2"]+=m2
        totals["snack"]+=sn
        totals["j1"]+=j1
        totals["j2"]+=j2
        totals["brk"]+=bk
        totals["seafood"]+=sf

        cur += timedelta(days=1)

    totals["meals_total"] = totals["meal1"] + totals["meal2"]
    totals["juices_total"] = totals["j1"] + totals["j2"]

    return totals, delivery_days

#########################################################
# STREAMLIT UI
#########################################################
st.set_page_config(page_title="Friska Billing", page_icon="💼", layout="centered")
st.title("🥗 Friska Wellness — Billing System")

# Authenticate Google
spid = get_spreadsheet_id(SHEET_URL)
creds = get_creds()
session = AuthorizedSession(creds)

############################################
# Sidebar — Settings
############################################
with st.sidebar:
    st.header("⚙️ Billing Settings")

    settings["price_nutri"] = st.number_input("Nutri Meal Price ₹", value=settings["price_nutri"])
    settings["price_high_protein"] = st.number_input("High Protein Meal Price ₹", value=settings["price_high_protein"])
    settings["price_seafood_addon"] = st.number_input("Seafood Add-on ₹", value=settings["price_seafood_addon"])
    settings["gst_percent"] = st.number_input("GST %", value=settings["gst_percent"])

    override = st.checkbox("Override delivery price?")
    if override:
        settings["price_delivery_override"] = st.number_input("Delivery Price ₹", value=settings["price_delivery_override"] or 80.0)
    else:
        settings["price_delivery_override"] = None

    if st.button("💾 Save Settings"):
        save_settings(settings)
        st.success("Settings saved!")

############################################
# Main Form
############################################
client = st.text_input("Client Name")
col1, col2 = st.columns(2)
start = col1.date_input("Start Date", date.today().replace(day=1))
end = col2.date_input("End Date", date.today())

if st.button("📊 Generate Usage & Bill"):
    totals, delivery_days = count_usage(session, spid, start, end, client)

    st.subheader("📈 Usage Summary")
    st.write(totals)

    # Determine meal rate
    # ** TEMP: default Nutri — will auto-detect plan later **
    price_meal = settings["price_nutri"]

    food_amount = totals["meals_total"] * price_meal
    seafood_amount = totals["seafood"] * settings["price_seafood_addon"]
    taxable = food_amount + seafood_amount
    gst_amt = round(taxable * settings["gst_percent"]/100, 2)

    if settings["price_delivery_override"] is not None:
        delivery_price = settings["price_delivery_override"]
    else:
        # sheet per-client delivery (NOT IMPLEMENTED HERE YET)
        delivery_price = 0

    delivery_amount = delivery_days * delivery_price
    grand_total = round(taxable + gst_amt + delivery_amount)

    st.subheader("🧮 Bill Calculation")
    st.write({
        "Food Base": food_amount,
        "Seafood Add-on": seafood_amount,
        "GST": gst_amt,
        "Delivery": delivery_amount,
        "TOTAL BILL": f"₹ {grand_total}"
    })

    st.success("✅ Draft bill created. Next step: PDF invoice generation.")

