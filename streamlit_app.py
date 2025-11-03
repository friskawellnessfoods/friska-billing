#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# =========================================================
# Friska Billing – Usage + Next Cycle + Admin Panel
# + Invoice Preview on your PNG template + PDF Download
#
# Put your PNG template in the repo (same folder) using one of:
#   - invoice_template_a4.png
#   - invoice_template.png
#   - assets/invoice_template_a4.png
#   - assets/invoice_template.png
#
# You can adjust text positions in LAYOUT (percent-based coordinates).
# =========================================================

import streamlit as st
import re, json, io, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

from PIL import Image, ImageDraw, ImageFont

# ---------- CONFIG ----------
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"
BILLING_TAB = "BillingCycle"   # headers: Client | Start | End

# Data tabs layout (clientlist)
COL_B_CLIENT = 1
COL_C_TYPE   = 2
COL_G_DELIVERY = 6
START_DATA_COL_IDX = 7   # H
COLUMNS_PER_BLOCK  = 6   # Meal1, Meal2, Snack, J1, J2, Breakfast

# Need WRITE scope (we update/append BillingCycle)
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
# Try a few common names; you can change this list or hard-code one path.
TEMPLATE_CANDIDATES = [
    "invoice_template_a4.png",
    "invoice_template.png",
    "assets/invoice_template_a4.png",
    "assets/invoice_template.png",
]

# Percent-based positions (x%, y%) relative to template width/height.
# Font sizes are in pixels (scaled by template height so it looks right at any DPI).
LAYOUT = {
    "fonts": {
        # App will try these; DejaVu works on Streamlit Cloud
        "regular": ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"],
        "bold":    ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"],
        "scale": 0.010,  # base font-size = scale * template_height
    },
    "header": {
        "invoice_no":   {"xy": (78, 7),  "size": 2.0, "bold": True,  "align": "left"},
        "bill_date":    {"xy": (78, 11), "size": 1.8, "bold": False, "align": "left"},
        "client":       {"xy": (22, 20), "size": 2.2, "bold": True,  "align": "left"},
        "duration":     {"xy": (22, 24), "size": 1.8, "bold": False, "align": "left"},  # "from X to Y"
    },
    # Table columns (as percentages across the page)
    "table": {
        "top_y": 30,  # start of rows area
        "row_gap": 3.5,  # percent height per row (tweak to line up)
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
        "label_x": 66,  # where "GST" / "TOTAL" labels start
    }
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
    # Convert to 1-based sheet row number
    sheet_row_number = last_row_index + 1
    return sd.date(), ed.date(), sheet_row_number

def append_values_simple(session: AuthorizedSession, spid: str, start, end, client):
    row = [client, dtstr(start), dtstr(end)]
    return append_values(session, spid, BILLING_TAB, [row])

def update_last_row(session: AuthorizedSession, spid: str, row_number: int, start, end, client):
    # update specific A..C row
    range_a1 = f"{BILLING_TAB}!A{row_number+1}:C{row_number+1}"
    rows = [[client, dtstr(start), dtstr(end)]]
    return update_values(session, spid, range_a1, rows)

# ---------- Drawing helpers ----------
def _pick_font(paths: List[str], px: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    # last resort: default PIL bitmap font (not ideal)
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
            img = Image.open(p).convert("RGB")
            return img
        except Exception:
            continue
    return None

def percent_to_px(w, h, perc_xy):
    x = int(round((perc_xy[0] / 100.0) * w))
    y = int(round((perc_xy[1] / 100.0) * h))
    return x, y

def render_invoice_image(template: Image.Image, fields: Dict[str, str], rows: List[Dict[str, str]]) -> Image.Image:
    """
    fields: {
      invoice_no, bill_date, client, duration,
      gst_label, gst_value, total_label, total_value
    }
    rows: list of {desc, qty, rate, price}
    """
    img = template.copy()
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Fonts
    base_px = max(12, int(LAYOUT["fonts"]["scale"] * H))
    f_reg = _pick_font(LAYOUT["fonts"]["regular"], base_px)
    f_bold = _pick_font(LAYOUT["fonts"]["bold"],    int(base_px*1.1))

    # Header
    for key, spec in LAYOUT["header"].items():
        val = fields.get(key, "")
        if not val: continue
        x, y = percent_to_px(W, H, spec["xy"])
        size_px = int(spec.get("size", 1.0) * base_px)
        font = _pick_font(LAYOUT["fonts"]["bold"] if spec.get("bold") else LAYOUT["fonts"]["regular"], size_px)
        _draw_text(draw, val, (x, y), font, align=spec.get("align","left"))

    # Table rows
    t = LAYOUT["table"]
    start_y_pct = t["top_y"]
    row_gap_pct = t["row_gap"]
    fs_px = int(t.get("font_size", 1.0) * base_px)
    font_desc = _pick_font(LAYOUT["fonts"]["bold"] if t.get("bold_desc") else LAYOUT["fonts"]["regular"], fs_px)
    font_num = _pick_font(LAYOUT["fonts"]["regular"], fs_px)

    def col_x(col_key):
        return percent_to_px(W, H, (t["cols"][col_key]["x"], 0))[0]
    def col_right(col_key):
        x = t["cols"][col_key]["x"] + t["cols"][col_key]["w"]
        return percent_to_px(W, H, (x, 0))[0]

    for i, r in enumerate(rows):
        y_pct = start_y_pct + i*row_gap_pct
        y = percent_to_px(W, H, (0, y_pct))[1]
        # desc
        _draw_text(draw, str(r.get("desc","")), (col_x("desc"), y), font_desc, align="left")
        # qty, rate, price (right aligned)
        _draw_text(draw, str(r.get("qty","")),  (col_right("qty"), y),  font_num, align="right")
        _draw_text(draw, str(r.get("rate","")), (col_right("rate"), y), font_num, align="right")
        _draw_text(draw, str(r.get("price","")),(col_right("price"), y),font_num, align="right")

    # Totals area
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
    img_rgb = img.convert("RGB")
    img_rgb.save(buf, format="PDF")
    return buf.getvalue()

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

# Placeholders shared
next_start = None
next_end = None
last_row_number = None
last_per_day_delivery = 0.0

# Core helpers for BillingCycle
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
    return sd.date(), ed.date(), last_row_index + 1

def append_values_simple(session: AuthorizedSession, spid: str, start, end, client):
    row = [client, dtstr(start), dtstr(end)]
    return append_values(session, spid, BILLING_TAB, [row])

def update_last_row(session: AuthorizedSession, spid: str, row_number: int, start, end, client):
    range_a1 = f"{BILLING_TAB}!A{row_number+1}:C{row_number+1}"
    rows = [[client, dtstr(start), dtstr(end)]]
    return update_values(session, spid, range_a1, rows)

# Bring in previously defined routines (count_usage, next_service_calendar_dates, etc.)
# ... (already defined above)

# Run if fetched
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

    # ----- Save next cycle buttons -----
    colx, coly = st.columns(2)
    if next_start and next_end:
        if colx.button("✅ Save next cycle to BillingCycle", use_container_width=True):
            try:
                if save_mode == "Update latest row":
                    update_last_row(session, spid, last_row_number, next_start, next_end, client)
                    st.success(f"Updated row in '{BILLING_TAB}': {client} | {dtstr(next_start)} → {dtstr(next_end)}")
                else:
                    append_values_simple(session, spid, next_start, next_end, client)
                    st.success(f"Appended in '{BILLING_TAB}': {client} | {dtstr(next_start)} → {dtstr(next_end)}")
            except Exception as e:
                st.error(f"Failed to save: {e}")

    st.info(f"Per-day delivery (from last cycle logic): ₹{last_per_day_delivery:.2f}")

# ---------- RIGHT: Admin panel (and invoice preview) ----------
st.markdown("---")
st.subheader("🛠️ Admin — Upcoming Bill (Preview)")

# Admin controls (always visible; will be prefilled if we've fetched)
c1, c2, c3 = st.columns([2,1,1])
admin_client_label = c1.text_input("Client label (print as)", value=(client or ""))
admin_billing_date = c2.date_input("Billing date", value=date.today())
admin_plan = c3.selectbox("Plan", ["Nutri", "High Protein"], index=0)

c4, c5 = st.columns(2)
admin_bill_start = c4.text_input("Bill start (dd-MMM-YYYY)", value=(dtstr(next_start) if next_start else ""))
admin_bill_end   = c5.text_input("Bill end (dd-MMM-YYYY)",   value=(dtstr(next_end) if next_end else ""))

c6, c7 = st.columns(2)
admin_invoice_no  = c6.text_input("Invoice No.", value="")
admin_duration    = c7.text_input("Bill duration text", value=(f"from {admin_bill_start} to {admin_bill_end}" if (admin_bill_start and admin_bill_end) else ""))

st.markdown("**Qty / Rate (you can pre-bill)**")
q1, r1 = st.columns(2)
meals_qty  = q1.number_input("Meals qty", value=26, step=1, min_value=0)
meals_rate = r1.number_input("Meals rate (₹)", value=float(DEFAULT_SETTINGS["price_nutri"]), step=5.0)

q2, r2 = st.columns(2)
seafood_qty  = q2.number_input("Seafood qty", value=26, step=1, min_value=0)
seafood_rate = r2.number_input("Seafood rate (₹)", value=float(DEFAULT_SETTINGS["price_seafood_addon"]), step=5.0)

q3, r3 = st.columns(2)
juice_qty  = q3.number_input("Juice qty", value=26, step=1, min_value=0)
juice_rate = r3.number_input("Juice rate (₹)", value=float(DEFAULT_SETTINGS["price_juice"]), step=5.0)

q4, r4 = st.columns(2)
snack_qty  = q4.number_input("Snack qty", value=26, step=1, min_value=0)
snack_rate = r4.number_input("Snack rate (₹)", value=float(DEFAULT_SETTINGS["price_snack"]), step=5.0)

q5, r5 = st.columns(2)
breakfast_qty  = q5.number_input("Breakfast qty", value=26, step=1, min_value=0)
breakfast_rate = r5.number_input("Breakfast rate (₹)", value=float(DEFAULT_SETTINGS["price_breakfast"]), step=5.0)

q6, r6 = st.columns(2)
delivery_days     = q6.number_input("Delivery days", value=26, step=1, min_value=0)
delivery_per_day  = r6.number_input("Delivery per day (₹)", value=float(0.0), step=5.0)

gst_pct = st.number_input("GST % (food items only)", value=float(DEFAULT_SETTINGS["gst_percent"]), step=1.0, min_value=0.0)

# Calculate line items
def money(n): 
    try:
        return f"₹{round(float(n))}"
    except:
        return "₹0"

base_rate = meals_rate
lines_for_preview = []
if admin_plan == "High Protein":
    desc_meal = "High Protein Meal"
else:
    desc_meal = "Nutri Balance Meal"

# Core lines (show even if qty 0 so you can see it)
def add_line(desc, qty, rate):
    price = round(qty * rate)
    lines_for_preview.append({"desc": desc, "qty": qty if qty else "", "rate": f"{int(rate)}" if rate else "", "price": f"{price}" if price else ""})
    return price

food_subtotal = 0
food_subtotal += add_line(desc_meal, meals_qty, meals_rate)
food_subtotal += add_line("Seafood add-on", seafood_qty, seafood_rate)
food_subtotal += add_line("Juice", juice_qty, juice_rate)
food_subtotal += add_line("Snack", snack_qty, snack_rate)
food_subtotal += add_line("Breakfast", breakfast_qty, breakfast_rate)

gst_amount = round(food_subtotal * (gst_pct/100.0)) if gst_pct else 0
delivery_amount = round(delivery_days * delivery_per_day) if (delivery_days and delivery_per_day) else 0
grand_total = round(food_subtotal + gst_amount + delivery_amount)

# ---- Preview / PDF buttons ----
cprev, cpdf = st.columns(2)
do_preview = cprev.button("🖼️ Generate Invoice Preview", use_container_width=True)
do_pdf     = cpdf.button("⬇️ Download PDF", use_container_width=True)

# Prepare common fields for drawing
fields = {
    "invoice_no": admin_invoice_no.strip(),
    "bill_date": date.today().strftime("%d-%b-%Y") if not isinstance(admin_billing_date, str) else admin_billing_date,
    "client": admin_client_label.strip() or (client or ""),
    "duration": admin_duration.strip(),
    "gst_value": money(gst_amount),
    "total_value": money(grand_total),
}

# Locate template
template_img = try_load_template()
if (do_preview or do_pdf) and template_img is None:
    st.error("Template PNG not found. Add one of these files next to the app: "
             "`invoice_template_a4.png`, `invoice_template.png`, or in `assets/` folder.")
elif (do_preview or do_pdf) and template_img is not None:
    # Build preview rows (remove fully empty lines from the end to keep it clean)
    visible_rows = [r for r in lines_for_preview if (r["qty"] or r["price"])]
    if not visible_rows:
        visible_rows = [{"desc": "Meal Plan", "qty":"", "rate":"", "price":""}]

    # Render image
    inv_img = render_invoice_image(template_img, fields, visible_rows)

    if do_preview:
        st.image(inv_img, caption="Invoice Preview (screenshot-friendly)", use_column_width=True)

    if do_pdf:
        pdf_bytes = image_to_pdf_bytes(inv_img)
        st.download_button(
            "Download Invoice PDF",
            data=pdf_bytes,
            file_name=f"Invoice_{(client or 'Client').replace(' ','_')}_{date.today().strftime('%Y%m%d')}.pdf",
            mime="application/pdf",
            use_container_width=True
        )
