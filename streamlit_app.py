#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import streamlit as st
import re, json, io, os, calendar
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from PIL import Image, ImageDraw, ImageFont

# ===================== CONFIG =====================
SHEET_URL = "https://docs.google.com/spreadsheets/d/1CsT6_oYsFjgQQ73pt1Bl1cuXuzKY8JnOTB3E4bDkTiA/edit?usp=sharing"
BILLING_TAB = "BillingCycle"   # headers: Client | Start | End

# clientlist sheet structure
COL_B_CLIENT = 1
COL_C_TYPE   = 2
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

TEMPLATE_CANDIDATES = [
    "invoice_template_a4.png",
    "invoice_template.png",
    "assets/invoice_template_a4.png",
    "assets/invoice_template.png",
]

# ======= Fine-tuned layout for your PNG (based on your last screenshot) =======
LAYOUT = {
    "fonts": {
        "regular": ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"],
        "bold":    ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"],
        "scale": 0.0078,
    },
    # Header cells — pushed further down/right
    "header": {
        "dur_start":  {"xy": (17.2, 18.8), "size": 1.55, "bold": False, "align": "left"},
        "dur_end":    {"xy": (43.0, 18.8), "size": 1.55, "bold": False, "align": "left"},
        "invoice_no": {"xy": (86.5, 18.4), "size": 1.55, "bold": True,  "align": "right"},
        "bill_date":  {"xy": (17.2, 25.2), "size": 1.45, "bold": False, "align": "left"},
        "days":       {"xy": (43.0, 25.2), "size": 1.45, "bold": False, "align": "left"},
        "client":     {"xy": (88.0, 25.2), "size": 1.85, "bold": True,  "align": "right"},
    },
    # Table — first row lower; columns more left
    "table": {
        "top_y": 34.0,     # lower so it sits under headings
        "row_gap": 5.4,
        "cols": {
            "desc":  {"x": 7.2,  "w": 55.0, "align": "left"},   # a touch left
            "qty":   {"x": 58.8, "w": 8.4,  "align": "right"},  # ~2.8 cells left
            "rate":  {"x": 70.8, "w": 8.4,  "align": "right"},  # ~1.8 cells left
            "price": {"x": 83.8, "w": 8.4,  "align": "right"},  # slightly left
        },
        "font_size_desc": 1.70,
        "font_size_num":  1.70,
        "bold_desc": False
    },
    # Totals — moved well above footer band
    "totals": {
        "gst_label":   {"xy": (74.8, 66.8), "size": 1.9, "bold": True, "align": "left"},
        "gst_value":   {"xy": (86.3, 66.8), "size": 1.9, "bold": True, "align": "right"},
        "total_label": {"xy": (74.8, 73.8), "size": 2.2, "bold": True, "align": "left"},
        "total_value": {"xy": (86.3, 73.8), "size": 2.2, "bold": True, "align": "right"},
    }
}

# ===================== Auth & Sheets helpers =====================
def get_service_account_session() -> AuthorizedSession:
    try:
        sec = st.secrets["gcp_credentials"]
    except Exception:
        st.error("Secrets missing: put your Service Account JSON under [gcp_credentials].")
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

def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

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
def detect_clientlist_structure(header_row: List):
    """
    Dynamically detects:
    - first date column (start of meal blocks)
    - delivery column (by header text)
    """
    first_date_col = None
    delivery_col = None

    for idx, cell in enumerate(header_row):
        if first_date_col is None and to_dt(cell):
            first_date_col = idx

        if isinstance(cell, str) and "delivery" in cell.lower():
            delivery_col = idx

    if first_date_col is None:
        raise ValueError("Could not detect date columns in clientlist header.")

    return first_date_col, delivery_col

def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def fetch_values(session: AuthorizedSession, spid: str, a1_range: str) -> List[List[str]]:
    from urllib.parse import quote
    enc = quote(a1_range, safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spid}/values/{enc}"
    r = session.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=30)
    if r.status_code == 403:
        st.error("Permission denied. Share the Sheet with the service account (Editor).")
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

def compute_delivery_per_day_for_rows(
    rows: List[int],
    data: List[List[str]],
    delivery_col: Optional[int]
):

    if not rows:
        return 0.0, "none", []
    types, prices = [], []
    for r in rows:
        row = data[r] if r < len(data) else []
        typ = str(row[COL_C_TYPE]).strip() if len(row) > COL_C_TYPE else ""
        price = parse_float(
    row[delivery_col]
    if delivery_col is not None and len(row) > delivery_col
    else ""
)
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

def count_usage(session: AuthorizedSession, spid: str, start: date, end: date, client_name: str):
    client_key = norm_name(client_name)
    totals = dict(meal1=0, meal2=0, snack=0, j1=0, j2=0, brk=0, seafood=0)
    total_days = 0; active_days = 0
    paused_dates: List[date] = []
    last_per_day_delivery = 0.0

        for (yy, mm) in month_span_inclusive(start, end):
        month_name = calendar.month_name[mm]
        sheet_title, _ = get_clientlist_sheet_title(session, spid, month_name)
        if not sheet_title:
            continue

        data = fetch_values(session, spid, f"{sheet_title}!A1:ZZ2000")
        if not data:
            continue

        rows_by_client = {}
        for r, row in enumerate(data):
            nm = norm_name(row[COL_B_CLIENT]) if len(row) > COL_B_CLIENT else ""
            if nm:
                rows_by_client.setdefault(nm, []).append(r)

        client_rows = rows_by_client.get(client_key, [])

        row1 = data[0]

        # 🔍 Detect structure dynamically
        first_date_col, delivery_col = detect_clientlist_structure(row1)

        date_to_block = {}
        header_dates = []

        c = first_date_col
        while c < len(row1):
            dt = to_dt(row1[c])
            if not dt:
                break

            d = dt.date()
            date_to_block[d] = c
            if start <= d <= end:
                header_dates.append(d)

            c += COLUMNS_PER_BLOCK

        # 🚚 Delivery calculation (dynamic column)
        per_day_delivery, _, _ = compute_delivery_per_day_for_rows(
            client_rows,
            data,
            delivery_col
        )

        if per_day_delivery:
            last_per_day_delivery = per_day_delivery

        service_days_this_month = 0

        for d in header_dates:
            block = date_to_block.get(d)
            if block is None or not client_rows:
                continue

            m1 = m2 = sn = j1 = j2 = bk = sf = 0

            for r in client_rows:
                row = data[r]

                def cell(ci):
                    return row[ci] if ci < len(row) else ""

                v1 = str(cell(block)).strip()
                v2 = str(cell(block + 1)).strip()

                if v1:
                    m1 += 1
                    if norm_name(v1) == "seafood 1":
                        sf += 1

                if v2:
                    m2 += 1
                    if norm_name(v2) == "seafood 2":
                        sf += 1

                if str(cell(block + 2)).strip():
                    sn += 1
                if str(cell(block + 3)).strip():
                    j1 += 1
                if str(cell(block + 4)).strip():
                    j2 += 1
                if str(cell(block + 5)).strip():
                    bk += 1

            if (m1 + m2 + sn + j1 + j2 + bk) > 0:
                service_days_this_month += 1
            else:
                paused_dates.append(d)

            totals["meal1"] += m1
            totals["meal2"] += m2
            totals["snack"] += sn
            totals["j1"] += j1
            totals["j2"] += j2
            totals["brk"] += bk
            totals["seafood"] += sf

        total_days += len(header_dates)
        active_days += service_days_this_month

    totals["meals_total"]  = totals["meal1"] + totals["meal2"]
    totals["juices_total"] = totals["j1"] + totals["j2"]
    paused_days = max(0, total_days - active_days)
    return totals, active_days, paused_days, total_days, paused_dates, last_per_day_delivery

def next_service_calendar_dates(after_day: date, needed: int) -> List[date]:
    out: List[date] = []
    cur = after_day + timedelta(days=1)
    while len(out) < needed:
        if cur.weekday() != 6:  # Sunday=6
            out.append(cur)
        cur += timedelta(days=1)
    return out

def get_prev_cycle_for_client(session: AuthorizedSession, spid: str, client_name: str) -> Tuple[Optional[date], Optional[date], Optional[int]]:
    vals = fetch_values(session, spid, f"{BILLING_TAB}!A1:C10000")
    if not vals or len(vals) < 2:
        return None, None, None
    headers = [x.strip().lower() for x in vals[0]]
    try:
        ci = headers.index("client"); si = headers.index("start"); ei = headers.index("end")
    except ValueError:
        return None, None, None
    key = norm_name(client_name)
    last_row = None; last_row_index = None
    for idx, r in enumerate(vals[1:], start=1):
        if len(r) <= max(ci, si, ei): continue
        if norm_name(r[ci]) == key:
            last_row = r; last_row_index = idx
    if last_row is None:
        return None, None, None
    sd = to_dt(last_row[si]); ed = to_dt(last_row[ei])
    if not sd or not ed: return None, None, None
    return sd.date(), ed.date(), last_row_index + 1

def append_cycle_row(session: AuthorizedSession, spid: str, client, start, end):
    row = [client, dtstr(start), dtstr(end)]
    return append_values(session, spid, BILLING_TAB, [row])

def update_cycle_row(session: AuthorizedSession, spid: str, sheet_row_number: int, client, start, end):
    range_a1 = f"{BILLING_TAB}!A{sheet_row_number}:C{sheet_row_number}"
    rows = [[client, dtstr(start), dtstr(end)]]
    return update_values(session, spid, range_a1, rows)

# ===================== Drawing helpers =====================
def percent_to_px(w, h, perc_xy):
    x = int(round((perc_xy[0] / 100.0) * w))
    y = int(round((perc_xy[1] / 100.0) * h))
    return x, y

def _pick_font(paths: List[str], px: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    return ImageFont.load_default()

def _draw_text(draw: ImageDraw.ImageDraw, text: str, xy_px: Tuple[int,int],
               font: ImageFont.FreeTypeFont, align="left"):
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

def render_invoice_image(template: Image.Image, fields: Dict[str, str], rows: List[Dict[str, str]]) -> Image.Image:
    img = template.copy()
    draw = ImageDraw.Draw(img)
    W, H = img.size

    base_px = max(12, int(LAYOUT["fonts"]["scale"] * H))
    for key, spec in LAYOUT["header"].items():
        val = fields.get(key, "") or ""
        x, y = percent_to_px(W, H, spec["xy"])
        size_px = int(spec.get("size", 1.0) * base_px)
        font = _pick_font(LAYOUT["fonts"]["bold"] if spec.get("bold") else LAYOUT["fonts"]["regular"], size_px)
        _draw_text(draw, val, (x, y), font, align=spec.get("align","left"))

    t = LAYOUT["table"]
    fs_desc = int(t.get("font_size_desc", 1.0) * base_px)
    fs_num  = int(t.get("font_size_num",  1.0) * base_px)
    font_desc = _pick_font(LAYOUT["fonts"]["bold"] if t.get("bold_desc") else LAYOUT["fonts"]["regular"], fs_desc)
    font_num  = _pick_font(LAYOUT["fonts"]["regular"], fs_num)

    def col_left(col_key):  return percent_to_px(W, H, (t["cols"][col_key]["x"], 0))[0]
    def col_right(col_key): return percent_to_px(W, H, (t["cols"][col_key]["x"] + t["cols"][col_key]["w"], 0))[0]

    for i, r in enumerate(rows):
        y_pct = t["top_y"] + i * t["row_gap"]
        y = percent_to_px(W, H, (0, y_pct))[1]
        _draw_text(draw, str(r.get("desc","")), (col_left("desc"),  y), font_desc, align="left")
        _draw_text(draw, str(r.get("qty","")),  (col_right("qty"),  y), font_num,  align="right")
        _draw_text(draw, str(r.get("rate","")), (col_right("rate"), y), font_num,  align="right")
        _draw_text(draw, str(r.get("price","")),(col_right("price"),y), font_num,  align="right")

    for key, spec in LAYOUT["totals"].items():
        label = "GST" if key == "gst_label" else ("TOTAL" if key == "total_label" else None)
        value = fields.get(key, "")
        x, y = percent_to_px(W, H, spec["xy"])
        size_px = int(spec.get("size", 1.0) * base_px)
        font = _pick_font(LAYOUT["fonts"]["bold"] if spec.get("bold") else LAYOUT["fonts"]["regular"], size_px)
        if label:
            _draw_text(draw, label, (x, y), font, align=spec.get("align","left"))
        else:
            _draw_text(draw, str(value or ""), (x, y), font, align=spec.get("align","right"))
    return img

def image_to_pdf_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF")
    return buf.getvalue()

# ===================== APP =====================
st.set_page_config(page_title="Friska Billing", page_icon="🥗", layout="wide")
st.markdown("<h2 style='margin-bottom:0'>Friska Wellness — Billing System</h2>", unsafe_allow_html=True)

session = get_service_account_session()
spid = get_spreadsheet_id(SHEET_URL)

# state
defaults = {
    "fetched": False, "client": "",
    "prev_start": None, "prev_end": None,
    "next_start": None, "next_end": None,
    "last_row_number": None,
    "delivery_per_day": 0.0,
    "totals": {}, "active_days": 0, "paused_days": 0, "total_days": 0,
    "paused_dates": [],
    "admin_invoice_no": "", "manual_override": False
}
for k,v in defaults.items():
    if k not in st.session_state: st.session_state[k]=v

left, mid, right = st.columns([1,2,1])

# LEFT — prices
with left:
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
        save_settings(settings); st.success("Saved.")

def compute_from_range(client_name: str, prev_start: date, prev_end: date):
    totals, active_days, paused_days, total_days, paused_dates, learned_delivery = count_usage(session, spid, prev_start, prev_end, client_name)
    needed_adjust = paused_days
    needed_bill   = 26
    future_needed = needed_adjust + needed_bill
    future_dates  = next_service_calendar_dates(prev_end, future_needed)
    bill_dates    = future_dates[needed_adjust:needed_adjust+needed_bill]
    next_start = bill_dates[0] if bill_dates else None
    next_end   = bill_dates[-1] if bill_dates else None
    st.session_state.update({
        "fetched": True, "client": client_name,
        "prev_start": prev_start, "prev_end": prev_end,
        "next_start": next_start, "next_end": next_end,
        "delivery_per_day": learned_delivery or st.session_state.get("delivery_per_day", 0.0),
        "totals": totals, "active_days": active_days,
        "paused_days": paused_days, "total_days": total_days,
        "paused_dates": paused_dates,
        # sensible Admin defaults
        "q_meals": 26,
        "q_delivdays": active_days,
        "rate_deliv": learned_delivery or 0.0,
    })

# MID — workflow
with mid:
    st.markdown("### Workflow")
    cA, cB = st.columns([3,1])
    client_in = cA.text_input("Client", value=st.session_state["client"], key="client_input")
    save_mode = cB.selectbox("Save mode", ["Update latest row", "Append new row"], key="save_mode")

    with st.expander("Manual date override (if client missing in BillingCycle / to force a range)", expanded=False):
        st.checkbox("Enable manual override", value=st.session_state.get("manual_override", False), key="manual_override")
        cc1, cc2 = st.columns(2)
        mo_start = cc1.text_input("Start (dd-MMM-YYYY)", value="", key="mo_start")
        mo_end   = cc2.text_input("End (dd-MMM-YYYY)",   value="", key="mo_end")
        def _parse_d(s):
            try: return datetime.strptime(s.strip(), "%d-%b-%Y").date() if s.strip() else None
            except: return None
        manual_start = _parse_d(mo_start); manual_end = _parse_d(mo_end)
        if st.session_state["manual_override"] and (not manual_start or not manual_end):
            st.info("Enter both Start and End to use manual override.")

    if st.button("📊 Fetch Usage & Plan", use_container_width=True, key="btn_fetch"):
        nm = (client_in or "").strip()
        if not nm: st.error("Enter client name.")
        else:
            ps, pe, row_num = get_prev_cycle_for_client(session, spid, nm)
            st.session_state["last_row_number"] = row_num
            if st.session_state["manual_override"]:
                if manual_start and manual_end:
                    compute_from_range(nm, manual_start, manual_end)
                    st.success("Computed from manual override.")
                else:
                    st.error("Manual override enabled. Please enter Start & End.")
            else:
                if ps and pe:
                    compute_from_range(nm, ps, pe)
                    st.success("Computed from BillingCycle.")
                else:
                    st.warning("Client not found in BillingCycle. Use manual override.")

    if st.session_state["fetched"]:
        totals = st.session_state["totals"]
        st.markdown("#### Usage Summary")
        lines = [f"- **Meals total:** {totals.get('meals_total',0)}"]
        if totals.get("seafood",0)>0: lines.append(f"- **Seafood add-on (count):** {totals['seafood']}")
        if totals.get("snack",0)>0:   lines.append(f"- **Snacks total:** {totals['snack']}")
        if totals.get("juices_total",0)>0:
            lines.append(f"- **Juices total:** {totals['juices_total']} (J1: {totals.get('j1',0)}, J2: {totals.get('j2',0)})")
        if totals.get("brk",0)>0:     lines.append(f"- **Breakfast total:** {totals['brk']}")
        lines += [
            f"- **Active days:** {st.session_state['active_days']}",
            f"- **Paused days:** {st.session_state['paused_days']}",
            f"- **Total days:** {st.session_state['total_days']}",
        ]
        st.markdown("\n".join(lines))
        st.markdown("**Paused dates:** " + (", ".join(sorted({d.strftime('%d-%b-%Y') for d in st.session_state['paused_dates']})) if st.session_state['paused_dates'] else "None"))

        st.markdown("#### Next Cycle Planner")
        adj_needed = st.session_state['paused_days']
        notes = [
            f"- **Previous bill range:** {dtstr(st.session_state['prev_start'])} → {dtstr(st.session_state['prev_end'])}",
            f"- **Paused days to adjust:** {adj_needed}",
        ]
        if adj_needed:
            adj_dates = next_service_calendar_dates(st.session_state['prev_end'], adj_needed)
            notes.append("- **Adjustment dates:** " + ", ".join(dtstr(d) for d in adj_dates))
        else:
            notes.append("- **Adjustment dates:** None")
        notes += [
            f"- **New bill start:** {dtstr(st.session_state['next_start'])}",
            f"- **New bill end:** {dtstr(st.session_state['next_end'])}",
        ]
        st.markdown("\n".join(notes))

        c1, c2 = st.columns(2)
        if st.session_state["next_start"] and st.session_state["next_end"]:
            if c1.button("✅ Save Next Cycle to BillingCycle", use_container_width=True, key="save_cycle"):
                try:
                    if st.session_state["save_mode"]=="Update latest row" and st.session_state.get("last_row_number"):
                        update_cycle_row(session, spid, st.session_state["last_row_number"],
                                         st.session_state["client"], st.session_state["next_start"], st.session_state["next_end"])
                        st.success("Updated BillingCycle.")
                    else:
                        append_cycle_row(session, spid, st.session_state["client"],
                                         st.session_state["next_start"], st.session_state["next_end"])
                        st.success("Appended to BillingCycle.")
                except Exception as e:
                    st.error(f"Save failed: {e}")
        st.info(f"Per-day delivery (learned): ₹{st.session_state['delivery_per_day']:.2f}")

        st.markdown("---")
        st.markdown("### Invoice")
        colp, cold = st.columns(2)
        do_preview = colp.button("🖼️ Generate Preview", use_container_width=True, key="btn_preview")
        do_pdf     = cold.button("⬇️ Download PDF", use_container_width=True, key="btn_pdf")

        price_meal = settings["price_high_protein"] if st.session_state.get("admin_plan","Nutri")=="High Protein" else settings["price_nutri"]
        lines_for_preview = []

        def add_line(desc, qty, rate):
            qty_i  = int(qty) if qty else 0
            rate_f = float(rate) if rate else 0.0
            price  = round(qty_i * rate_f)
            lines_for_preview.append({
                "desc": desc,
                "qty": qty_i if qty_i else "",
                "rate": f"{int(rate_f)}" if rate_f else "",
                "price": f"{price}" if price else ""
            })
            return price

        food_subtotal = 0
        food_subtotal += add_line("Meal Plan",      st.session_state.get("q_meals", 26), price_meal)
        food_subtotal += add_line("Seafood add-on", st.session_state.get("q_sea", 0),    settings["price_seafood_addon"])
        food_subtotal += add_line("Breakfast",      st.session_state.get("q_brk", 0),    settings["price_breakfast"])
        food_subtotal += add_line("Juice",          st.session_state.get("q_juice", 0),  settings["price_juice"])
        food_subtotal += add_line("Snack",          st.session_state.get("q_snack", 0),  settings["price_snack"])

        gst_amount = round(food_subtotal * (settings["gst_percent"]/100.0)) if settings["gst_percent"] else 0

        # DELIVERY — now always added whenever qty>0 OR rate>0
        delivery_days = int(st.session_state.get("q_delivdays", st.session_state.get("active_days", 0)) or 0)
        delivery_rate = float(st.session_state.get("rate_deliv", st.session_state.get("delivery_per_day", 0.0)) or 0.0)
        delivery_amount = round(delivery_days * delivery_rate)
        if delivery_days > 0 or delivery_rate > 0:
            add_line("Delivery", delivery_days, delivery_rate)

        grand_total = round(food_subtotal + gst_amount + delivery_amount)

        dur_start_text = (st.session_state.get("adm_start") or "").strip()
        dur_end_text   = (st.session_state.get("adm_end") or "").strip()
        client_label   = (st.session_state.get("adm_client_lbl") or st.session_state.get("client","")).strip()
        bill_date_text = st.session_state.get("adm_bill_date", date.today())
        if isinstance(bill_date_text, date):
            bill_date_text = bill_date_text.strftime("%d-%b-%Y")

        fields = {
            "dur_start":  dur_start_text,
            "dur_end":    dur_end_text,
            "invoice_no": st.session_state.get("admin_invoice_no","").strip(),
            "bill_date":  bill_date_text,
            "days":       "Days- 26",
            "client":     client_label,
            "gst_label":   "GST",
            "gst_value":   f"₹{gst_amount}",
            "total_label": "TOTAL",
            "total_value": f"₹{grand_total}",
        }

        template_img = try_load_template()
        if (do_preview or do_pdf):
            if not template_img:
                st.error("Template PNG not found. Place `invoice_template_a4.png` beside the app.")
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

# RIGHT — admin
with right:
    st.markdown("### 🛠️ Admin")
    st.text_input("Client label (print as)", value=(st.session_state.get("adm_client_lbl") or st.session_state.get("client","")), key="adm_client_lbl")
    st.date_input("Billing date", value=(st.session_state.get("adm_bill_date") or date.today()), key="adm_bill_date")
    st.selectbox("Plan", ["Nutri", "High Protein"], index=(0 if st.session_state.get("admin_plan","Nutri")=="Nutri" else 1), key="admin_plan")

    def _prefill(d: Optional[date]) -> str: return dtstr(d) if isinstance(d,date) else ""
    if st.session_state["fetched"]:
        if not st.session_state.get("adm_start"): st.session_state["adm_start"] = _prefill(st.session_state["next_start"])
        if not st.session_state.get("adm_end"):   st.session_state["adm_end"]   = _prefill(st.session_state["next_end"])

    st.text_input("Bill start (dd-MMM-YYYY)", value=st.session_state.get("adm_start",""), key="adm_start")
    st.text_input("Bill end (dd-MMM-YYYY)",   value=st.session_state.get("adm_end",""),   key="adm_end")
    st.text_input("Invoice No.", value=st.session_state.get("admin_invoice_no",""), key="admin_invoice_no")

    auto_dur = ""
    if st.session_state.get("adm_start") and st.session_state.get("adm_end"):
        auto_dur = f"from {st.session_state['adm_start']} to {st.session_state['adm_end']}"
        if not st.session_state.get("adm_dur"): st.session_state["adm_dur"] = auto_dur
    st.text_input("Bill duration text", value=st.session_state.get("adm_dur", auto_dur), key="adm_dur")

    st.markdown("**Quantities** *(rates & GST in left console)*")
    st.number_input("Meals qty", value=st.session_state.get("q_meals", 26), step=1, min_value=0, key="q_meals")
    st.number_input("Delivery days", value=st.session_state.get("q_delivdays", st.session_state.get("active_days",0)), step=1, min_value=0, key="q_delivdays")

    c3, c4, c5 = st.columns(3)
    c3.number_input("Seafood qty", value=st.session_state.get("q_sea", 0), step=1, min_value=0, key="q_sea")
    c4.number_input("Juice qty",   value=st.session_state.get("q_juice", 0), step=1, min_value=0, key="q_juice")
    c5.number_input("Snack qty",   value=st.session_state.get("q_snack", 0), step=1, min_value=0, key="q_snack")

    c6, c7 = st.columns(2)
    c6.number_input("Breakfast qty", value=st.session_state.get("q_brk", 0), step=1, min_value=0, key="q_brk")
    c7.number_input("Delivery per day (₹)", value=float(st.session_state.get("rate_deliv", st.session_state.get("delivery_per_day", 0.0))), step=5.0, key="rate_deliv")
