#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing – Dual consoles (Left=Prices, Right=Admin),
# Center stage (Usage/Planner + Buttons + Full-width Preview)
# Stable state + BillingCycle update/append
# =========================================================

import streamlit as st
import re, json, io, os, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from PIL import Image, ImageDraw, ImageFont

# ---------- CONFIG ----------
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"
BILLING_TAB = "BillingCycle"   # headers row1: Client | Start | End

# Clientlist layout
COL_B_CLIENT = 1
COL_C_TYPE   = 2
COL_G_DELIVERY = 6
START_DATA_COL_IDX = 7   # H
COLUMNS_PER_BLOCK  = 6   # Meal1, Meal2, Snack, J1, J2, Breakfast

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

# ---------- Template + layout ----------
TEMPLATE_CANDIDATES = [
    "invoice_template_a4.png",
    "invoice_template.png",
    "assets/invoice_template_a4.png",
    "assets/invoice_template.png",
]

LAYOUT = {
    "fonts": {
        "regular": ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"],
        "bold":    ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"],
        "scale": 0.010,
    },
    "header": {
        "invoice_no":   {"xy": (78, 7),  "size": 2.0, "bold": True,  "align": "left"},
        "bill_date":    {"xy": (78, 11), "size": 1.8, "bold": False, "align": "left"},
        "client":       {"xy": (22, 20), "size": 2.2, "bold": True,  "align": "left"},
        "duration":     {"xy": (22, 24), "size": 1.8, "bold": False, "align": "left"},
    },
    "table": {
        "top_y": 30,
        "row_gap": 3.5,
        "cols": {
            "desc":  {"x": 8,  "w": 56, "align": "left"},
            "qty":   {"x": 66, "w": 8,  "align": "right"},
            "rate":  {"x": 76, "w": 8,  "align": "right"},
            "price": {"x": 86, "w": 8,  "align": "right"},
        },
        "font_size": 1.8,
        "bold_desc": False
    },
    "totals": {
        "gst":   {"xy": (86, 78), "size": 2.0, "bold": True, "align": "right"},
        "total": {"xy": (86, 84), "size": 2.4, "bold": True, "align": "right"},
        "label_x": 66,
    }
}

# ---------- Auth ----------
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

        rows_by_client = {}
        for r, row in enumerate(data):
            nm = norm_name(row[COL_B_CLIENT]) if len(row) > COL_B_CLIENT else ""
            if nm: rows_by_client.setdefault(nm, []).append(r)
        client_rows = rows_by_client.get(client_key, [])

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

        per_day_delivery, _, _ = compute_delivery_per_day_for_rows(client_rows, data)
        last_per_day_delivery = per_day_delivery or last_per_day_delivery

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
    last_row_index = None
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
    return sd.date(), ed.date(), last_row_index + 1  # actual sheet row number

def append_cycle_row(session: AuthorizedSession, spid: str, client, start, end):
    row = [client, dtstr(start), dtstr(end)]
    return append_values(session, spid, BILLING_TAB, [row])

def update_cycle_row(session: AuthorizedSession, spid: str, sheet_row_number: int, client, start, end):
    range_a1 = f"{BILLING_TAB}!A{sheet_row_number}:C{sheet_row_number}"  # exact row
    rows = [[client, dtstr(start), dtstr(end)]]
    return update_values(session, spid, range_a1, rows)

# ---------- Drawing helpers ----------
def _pick_font(paths: List[str], px: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    return ImageFont.load_default()

def _draw_text(draw: ImageDraw.ImageDraw, text: str, xy_px: Tuple[int,int], font: ImageFont.FreeTypeFont, align="left"):
    x, y = xy_px
    if align == "right":
        w = draw.textlength(text, font=font)
        x = x - int(w)
    draw.text((x, y), text, fill=(0,0,0), font=font)

def try_load_template() -> Optional[Image.Image]:
    for p in TEMPLATE_CANDIDATES:
        try:
            return Image.open(p).convert("RGB")
        except Exception:
            continue
    return None

def percent_to_px(w, h, perc_xy):
    x = int(round((perc_xy[0] / 100.0) * w)); y = int(round((perc_xy[1] / 100.0) * h))
    return x, y

def render_invoice_image(template: Image.Image, fields: Dict[str, str], rows: List[Dict[str, str]]) -> Image.Image:
    img = template.copy()
    draw = ImageDraw.Draw(img)
    W, H = img.size
    base_px = max(12, int(LAYOUT["fonts"]["scale"] * H))
    for key, spec in LAYOUT["header"].items():
        val = fields.get(key, "")
        if not val: continue
        x, y = percent_to_px(W, H, spec["xy"])
        size_px = int(spec.get("size", 1.0) * base_px)
        font = _pick_font(LAYOUT["fonts"]["bold"] if spec.get("bold") else LAYOUT["fonts"]["regular"], size_px)
        _draw_text(draw, val, (x, y), font, align=spec.get("align","left"))
    t = LAYOUT["table"]
    start_y_pct = t["top_y"]; row_gap_pct = t["row_gap"]
    fs_px = int(t.get("font_size", 1.0) * base_px)
    font_desc = _pick_font(LAYOUT["fonts"]["bold"] if t.get("bold_desc") else LAYOUT["fonts"]["regular"], fs_px)
    font_num = _pick_font(LAYOUT["fonts"]["regular"], fs_px)
    def col_x(col_key): return percent_to_px(W, H, (t["cols"][col_key]["x"], 0))[0]
    def col_right(col_key):
        x = t["cols"][col_key]["x"] + t["cols"][col_key]["w"]
        return percent_to_px(W, H, (x, 0))[0]
    for i, r in enumerate(rows):
        y_pct = start_y_pct + i*row_gap_pct
        y = percent_to_px(W, H, (0, y_pct))[1]
        _draw_text(draw, str(r.get("desc","")), (col_x("desc"), y), font_desc, align="left")
        _draw_text(draw, str(r.get("qty","")),  (col_right("qty"), y),  font_num, align="right")
        _draw_text(draw, str(r.get("rate","")), (col_right("rate"), y), font_num, align="right")
        _draw_text(draw, str(r.get("price","")),(col_right("price"), y),font_num, align="right")
    for key, spec in LAYOUT["totals"].items():
        if key not in ("gst","total"): continue
        label = "GST" if key=="gst" else "TOTAL"
        value = fields.get("gst_value" if key=="gst" else "total_value", "")
        label_x_pct = LAYOUT["totals"]["label_x"]
        lbl_x, lbl_y = percent_to_px(W, H, (label_x_pct, spec["xy"][1]))
        val_x, val_y = percent_to_px(W, H, spec["xy"])
        fs_px2 = int(spec.get("size", 1.0) * base_px)
        font2 = _pick_font(LAYOUT["fonts"]["bold"] if spec.get("bold") else LAYOUT["fonts"]["regular"], fs_px2)
        _draw_text(draw, label, (lbl_x, lbl_y), font2, align="left")
        _draw_text(draw, value, (val_x, val_y), font2, align="right")
    return img

def image_to_pdf_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF")
    return buf.getvalue()

# ---------------- APP ----------------
st.set_page_config(page_title="Friska Billing", page_icon="🥗", layout="wide")
st.markdown("<h2 style='margin-bottom:0'>Friska Wellness — Billing System</h2>", unsafe_allow_html=True)

session = get_service_account_session()
spid = get_spreadsheet_id(SHEET_URL)

# ---------- Init session state ----------
defaults = {
    "fetched": False,
    "client": "",
    "prev_start": None,
    "prev_end": None,
    "next_start": None,
    "next_end": None,
    "last_row_number": None,
    "delivery_per_day": 0.0,
    "totals": {},
    "active_days": 0,
    "paused_days": 0,
    "total_days": 0,
    "paused_dates": [],
    "admin_invoice_no": "",
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ---------- 3-column layout ----------
left_col, mid_col, right_col = st.columns([1, 2, 1])

# LEFT CONSOLE (Prices)
with left_col:
    st.markdown("### ⚙️ Prices")
    settings = load_settings()
    c1, c2 = st.columns(2)
    settings["price_nutri"] = c1.number_input("Nutri (₹)", value=float(settings["price_nutri"]), step=5.0, key="p_nutri")
    settings["price_high_protein"] = c2.number_input("High Protein (₹)", value=float(settings["price_high_protein"]), step=5.0, key="p_hp")
    settings["price_seafood_addon"] = st.number_input("Seafood add-on (₹)", value=float(settings["price_seafood_addon"]), step=5.0, key="p_sea")
    st.markdown("**Add-ons**")
    c3, c4, c5 = st.columns(3)
    settings["price_juice"] = c3.number_input("Juice (₹)", value=float(settings["price_juice"]), step=5.0, key="p_juice")
    settings["price_snack"] = c4.number_input("Snack (₹)", value=float(settings["price_snack"]), step=5.0, key="p_snack")
    settings["price_breakfast"] = c5.number_input("Breakfast (₹)", value=float(settings["price_breakfast"]), step=5.0, key="p_brk")
    settings["gst_percent"] = st.number_input("GST % (food only)", value=float(settings["gst_percent"]), step=1.0, min_value=0.0, key="gst")
    if st.button("💾 Save", use_container_width=True, key="save_prices"):
        save_settings(settings)
        st.success("Saved.")

# helpers for logic
def do_fetch(client_name: str):
    prev_start, prev_end, last_row_number = get_prev_cycle_for_client(session, spid, client_name)
    (totals, active_days, paused_days, total_days,
     paused_dates, _delivery_amount, last_per_day_delivery) = count_usage(session, spid, prev_start, prev_end, client_name)
    needed_adjust = paused_days
    needed_bill   = 26
    future_needed = needed_adjust + needed_bill
    future_dates  = next_service_calendar_dates(prev_end, future_needed)
    adj_dates     = future_dates[:needed_adjust]
    bill_dates    = future_dates[needed_adjust:needed_adjust+needed_bill]
    next_start = bill_dates[0] if bill_dates else None
    next_end   = bill_dates[-1] if bill_dates else None
    st.session_state.update({
        "fetched": True,
        "client": client_name,
        "prev_start": prev_start,
        "prev_end": prev_end,
        "next_start": next_start,
        "next_end": next_end,
        "last_row_number": last_row_number,
        "delivery_per_day": last_per_day_delivery,
        "totals": totals,
        "active_days": active_days,
        "paused_days": paused_days,
        "total_days": total_days,
        "paused_dates": paused_dates,
    })

def money(n): 
    try: return f"₹{round(float(n))}"
    except: return "₹0"

# CENTER STAGE (main content)
with mid_col:
    st.markdown("### Workflow")
    cA, cB = st.columns([3, 1])
    client_in = cA.text_input("Client (exists in BillingCycle)", value=st.session_state["client"])
    save_mode = cB.selectbox("Save mode", ["Update latest row", "Append new row"], key="save_mode")
    if st.button("📊 Fetch Usage & Plan", use_container_width=True, key="btn_fetch"):
        if not client_in.strip():
            st.error("Enter client name.")
        else:
            try:
                do_fetch(client_in.strip())
                st.success("Fetched.")
            except Exception as e:
                st.error(str(e))

    if st.session_state["fetched"]:
        totals = st.session_state["totals"]
        st.markdown("#### Usage Summary")
        lines = []
        lines.append(f"- **Meals total:** {totals.get('meals_total',0)}")
        if totals.get("seafood",0) > 0: lines.append(f"- **Seafood add-on (count):** {totals['seafood']}")
        if totals.get("snack",0) > 0: lines.append(f"- **Snacks total:** {totals['snack']}")
        if totals.get("juices_total",0) > 0:
            lines.append(f"- **Juices total:** {totals['juices_total']} (J1: {totals.get('j1',0)}, J2: {totals.get('j2',0)})")
        if totals.get("brk",0) > 0: lines.append(f"- **Breakfast total:** {totals['brk']}")
        lines.append(f"- **Active days:** {st.session_state['active_days']}")
        lines.append(f"- **Paused days:** {st.session_state['paused_days']}")
        lines.append(f"- **Total days:** {st.session_state['total_days']}")
        st.markdown("\n".join(lines))
        st.markdown("**Paused dates:** " + (", ".join(sorted({d.strftime('%d-%b-%Y') for d in st.session_state['paused_dates']})) if st.session_state['paused_dates'] else "None"))

        st.markdown("#### Next Cycle Planner")
        nl = []
        nl.append(f"- **Previous bill range:** {dtstr(st.session_state['prev_start'])} → {dtstr(st.session_state['prev_end'])}")
        nl.append(f"- **Paused days to adjust:** {st.session_state['paused_days']}")
        # show individual adjustment dates (count + list)
        adj_needed = st.session_state['paused_days']
        if adj_needed:
            future_dates = next_service_calendar_dates(st.session_state['prev_end'], adj_needed)
            nl.append(f"- **Adjustment dates:** " + ", ".join(dtstr(d) for d in future_dates))
        else:
            nl.append(f"- **Adjustment dates:** None")
        nl.append(f"- **New bill start:** {dtstr(st.session_state['next_start']) if st.session_state['next_start'] else '—'}")
        nl.append(f"- **New bill end:** {dtstr(st.session_state['next_end']) if st.session_state['next_end'] else '—'}")
        st.markdown("\n".join(nl))

        c1, c2 = st.columns(2)
        if st.session_state["next_start"] and st.session_state["next_end"]:
            if c1.button("✅ Save Next Cycle to BillingCycle", use_container_width=True, key="save_cycle"):
                try:
                    if st.session_state["save_mode"] == "Update latest row":
                        update_cycle_row(session, spid, st.session_state["last_row_number"],
                                         st.session_state["client"], st.session_state["next_start"], st.session_state["next_end"])
                        st.success(f"Updated: {st.session_state['client']} | {dtstr(st.session_state['next_start'])} → {dtstr(st.session_state['next_end'])}")
                    else:
                        append_cycle_row(session, spid, st.session_state["client"],
                                         st.session_state["next_start"], st.session_state["next_end"])
                        st.success(f"Appended: {st.session_state['client']} | {dtstr(st.session_state['next_start'])} → {dtstr(st.session_state['next_end'])}")
                except Exception as e:
                    st.error(f"Failed to save: {e}")

        # ===== Middle: Invoice buttons + full-width PREVIEW =====
        st.markdown("---")
        st.markdown("### Invoice")
        colp, cold = st.columns(2)
        do_preview = colp.button("🖼️ Generate Preview", use_container_width=True, key="btn_preview")
        do_pdf     = cold.button("⬇️ Download PDF", use_container_width=True, key="btn_pdf")

        # Build invoice rows from RIGHT admin and LEFT prices
        # (right admin values are in session_state keys set below)
        if st.session_state["fetched"]:
            price_meal = settings["price_high_protein"] if st.session_state.get("admin_plan","Nutri")=="High Protein" else settings["price_nutri"]
            lines_for_preview = []

            def add_line(desc, qty, rate):
                price = round(qty * rate)
                lines_for_preview.append({
                    "desc": desc,
                    "qty": qty if qty else "",
                    "rate": f"{int(rate)}" if rate else "",
                    "price": f"{price}" if price else ""
                })
                return price

            food_subtotal = 0
            food_subtotal += add_line("Meal Plan", st.session_state.get("q_meals", 26), price_meal)
            food_subtotal += add_line("Seafood add-on", st.session_state.get("q_sea", 0), settings["price_seafood_addon"])
            food_subtotal += add_line("Juice", st.session_state.get("q_juice", 0), settings["price_juice"])
            food_subtotal += add_line("Snack", st.session_state.get("q_snack", 0), settings["price_snack"])
            food_subtotal += add_line("Breakfast", st.session_state.get("q_brk", 0), settings["price_breakfast"])

            gst_amount = round(food_subtotal * (settings["gst_percent"]/100.0)) if settings["gst_percent"] else 0
            delivery_amount = round(st.session_state.get("q_delivdays", 0) * st.session_state.get("rate_deliv", 0.0))
            grand_total = round(food_subtotal + gst_amount + delivery_amount)

            fields = {
                "invoice_no": st.session_state.get("admin_invoice_no","").strip(),
                "bill_date": st.session_state.get("adm_bill_date", date.today()).strftime("%d-%b-%Y") if isinstance(st.session_state.get("adm_bill_date"), date) else date.today().strftime("%d-%b-%Y"),
                "client": st.session_state.get("adm_client_lbl", st.session_state["client"]) or st.session_state["client"],
                "duration": st.session_state.get("adm_dur", ""),
                "gst_value": money(gst_amount),
                "total_value": money(grand_total),
            }

            if (do_preview or do_pdf):
                template_img = try_load_template()
                if not template_img:
                    st.error("Template PNG not found. Place `invoice_template_a4.png` (or `invoice_template.png`) in the app folder.")
                else:
                    visible_rows = [r for r in lines_for_preview if (r["qty"] or r["price"])]
                    if not visible_rows:
                        visible_rows = [{"desc":"Meal Plan","qty":"","rate":"","price":""}]
                    inv_img = render_invoice_image(template_img, fields, visible_rows)
                    st.image(inv_img, caption="Invoice Preview", use_column_width=True)
                    if do_pdf:
                        pdf_bytes = image_to_pdf_bytes(inv_img)
                        st.download_button(
                            "Download Invoice PDF",
                            data=pdf_bytes,
                            file_name=f"Invoice_{(st.session_state['client'] or 'Client').replace(' ','_')}_{date.today().strftime('%Y%m%d')}.pdf",
                            mime="application/pdf",
                            use_container_width=True
                        )

# RIGHT CONSOLE (Admin) — mirrors left console look
with right_col:
    st.markdown("### 🛠️ Admin")
    # Plan + basic header info
    st.session_state["adm_client_lbl"] = st.text_input("Client label (print as)", value=(st.session_state["adm_client_lbl"] if st.session_state.get("adm_client_lbl") else (st.session_state["client"] or "")), key="adm_client_lbl")
    st.session_state["adm_bill_date"]  = st.date_input("Billing date", value=(st.session_state.get("adm_bill_date") or date.today()), key="adm_bill_date")
    st.session_state["admin_plan"]     = st.selectbox("Plan", ["Nutri", "High Protein"], index=(0 if st.session_state.get("admin_plan","Nutri")=="Nutri" else 1), key="admin_plan")

    # Prefill bill start/end from fetched next_start/end
    def _prefill_date_text(d: Optional[date]) -> str:
        return dtstr(d) if isinstance(d, date) else ""
    if st.session_state["fetched"]:
        default_start = _prefill_date_text(st.session_state["next_start"])
        default_end   = _prefill_date_text(st.session_state["next_end"])
    else:
        default_start = st.session_state.get("adm_start","")
        default_end   = st.session_state.get("adm_end","")

    st.session_state["adm_start"] = st.text_input("Bill start (dd-MMM-YYYY)", value=(st.session_state.get("adm_start") or default_start), key="adm_start")
    st.session_state["adm_end"]   = st.text_input("Bill end (dd-MMM-YYYY)",   value=(st.session_state.get("adm_end")   or default_end),   key="adm_end")

    # Invoice No + duration text
    st.session_state["admin_invoice_no"] = st.text_input("Invoice No.", value=st.session_state.get("admin_invoice_no",""), key="admin_invoice_no")
    # If both start/end are present, auto duration text (editable)
    auto_dur = ""
    if st.session_state.get("adm_start") and st.session_state.get("adm_end"):
        auto_dur = f"from {st.session_state['adm_start']} to {st.session_state['adm_end']}"
    st.session_state["adm_dur"] = st.text_input("Bill duration text", value=(st.session_state.get("adm_dur") or auto_dur), key="adm_dur")

    st.markdown("**Quantities (rates & GST in LEFT console)**")
    # quantities with sensible defaults
    st.session_state["q_meals"]     = st.number_input("Meals qty", value=st.session_state.get("q_meals", 26), step=1, min_value=0, key="q_meals")
    st.session_state["q_delivdays"] = st.number_input("Delivery days", value=st.session_state.get("q_delivdays", 26), step=1, min_value=0, key="q_delivdays")

    c3, c4, c5 = st.columns(3)
    st.session_state["q_sea"]   = c3.number_input("Seafood qty", value=st.session_state.get("q_sea", 0), step=1, min_value=0, key="q_sea")
    st.session_state["q_juice"] = c4.number_input("Juice qty", value=st.session_state.get("q_juice", 0), step=1, min_value=0, key="q_juice")
    st.session_state["q_snack"] = c5.number_input("Snack qty", value=st.session_state.get("q_snack", 0), step=1, min_value=0, key="q_snack")

    c6, c7 = st.columns(2)
    st.session_state["q_brk"]     = c6.number_input("Breakfast qty", value=st.session_state.get("q_brk", 0), step=1, min_value=0, key="q_brk")
    st.session_state["rate_deliv"] = c7.number_input("Delivery per day (₹)", value=float(st.session_state.get("rate_deliv", st.session_state.get("delivery_per_day", 0.0))), step=5.0, key="rate_deliv")
