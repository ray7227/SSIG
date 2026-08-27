import re
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

try:
    from astral import LocationInfo
    from astral.sun import sun
    ASTRAL_OK = True
except ImportError:
    ASTRAL_OK = False

# =========================
# PAGE SETUP
# =========================
st.set_page_config(
    page_title="SSIG Assistant",
    page_icon="🦉",
    layout="wide",
)
st.title("SSIG Field Survey Assistant")

SSIG_PDF = "https://open.alberta.ca/dataset/93d8a251-4a9a-428f-ad99-7484c6ebabe0/resource/f4024e81-b835-4a50-8fb1-5b31d9726b84/download/2013-sensitivespeciesinventoryguidelines-apr18.pdf"

with st.sidebar:
    st.markdown("### Source")
    st.markdown(f"[Sensitive Species Inventory Guidelines (Apr 2013)]({SSIG_PDF})")

FILE = "SSIG_Breakdown.xlsx"
TZ = ZoneInfo("America/Edmonton")   # BC Peace users: MST year-round, adjust if needed

# Alberta location presets (lat, lon)
LOCATIONS = {
    "Calgary": (51.05, -114.07),
    "Edmonton": (53.55, -113.49),
    "Grande Prairie": (55.17, -118.80),
    "Medicine Hat": (50.04, -110.68),
    "Brooks": (50.58, -111.90),
    "Lethbridge": (49.69, -112.83),
    "Fort McMurray": (56.73, -111.38),
    "Fort St. John, BC": (56.25, -120.85),
    "Custom": None,
}

# =========================
# PHOTOS
# =========================
PHOTO_ROOT = "Images"

PHOTOS = {
    "Amphibians (Auditory Survey Guideline)": "Amph (Aud).png",
    "Amphibians (Non-Acoustic Survey Guideline)": "Amph (Non-ac).png",
    "Short-horned Lizard (ESHL)": "ESHL.png",
    "Snake Hibernacula Searches": "Snake.png",
    "Burrowing Owl (BUOW)": "BUOW.png",
    "Short-Eared Owl (SEOW) (BBS survey)": "SEOW.png",
    "Prairie Raptors (SR survey)": "SR.png",
    "Boreal & Foothills Raptors (BBS / SR survey)": "BBS & SR.png",
    "Grassland Birds (BBS survey)": "Grassland BBS.png",
    "Boreal & Foothills Breeding Songbirds & Woodpeckers (BBS survey)": "Boreal and foothills BBS.png",
    "Sharp-tailed Grouse (STGR)": "STGR.png",
    "Western Grebe (WEGR)": "WEGR.png",
    "Piping Plover (PIPL)": "PIPL.png",
    "Yellow Rail (BBS survey)": "Yellow Rail.png",
    "Common Nighthawk (CONI) (BBS survey)": "Nighthawk.png",
    "Bats": "Bats.png",
    "Swift Fox (SWFO)": "SWFO.png",
    "Ord's Kangaroo Rat (OKRA)": "OKRA.png",
    "Non-invasive Mammals Surveys-Winter Tracking & Searches for Mineral Licks": "Mineral licks.png",
    "Species at Risk Plant Surveys": "Plant.png",
}

def normalize(s):
    s = str(s).lower().strip()
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    for dash in ("\u2013", "\u2014", "\u2011"):
        s = s.replace(dash, "-")
    s = re.sub(r"\s+", " ", s)
    return s

PHOTOS_NORM = {normalize(k): v for k, v in PHOTOS.items()}

def get_photo(survey):
    name = PHOTOS.get(survey) or PHOTOS_NORM.get(normalize(survey))
    if not name:
        return None
    path = Path(PHOTO_ROOT) / name
    return path if path.is_file() else None

# =========================
# LOAD SPREADSHEET
# =========================
@st.cache_data
def load_data(path):
    df = pd.read_excel(path, header=0)
    df = df.rename(columns={df.columns[0]: "Field"})
    df["Field"] = df["Field"].astype(str).str.strip()
    df = df.set_index("Field")
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    return df

try:
    df = load_data(FILE)
except FileNotFoundError:
    st.error(f"Can't find {FILE}. Put it in the same folder as this app.")
    st.stop()

survey_types = list(df.columns)
fields = [f for f in df.index if f.lower() != "photo"]  # skip image row

def get_value(field, survey):
    val = df.loc[field, survey]
    if pd.isna(val):
        return ""
    return str(val).strip()

def field_value(field_query, survey):
    # match an index label by normalized name, exact first then contains
    for idx in df.index:
        if normalize(idx) == normalize(field_query):
            return get_value(idx, survey)
    for idx in df.index:
        if normalize(field_query) in normalize(idx):
            return get_value(idx, survey)
    return ""

# =========================
# TIMING ENGINE
# =========================
def sun_times(lat, lon, d):
    loc = LocationInfo(latitude=lat, longitude=lon)
    s = sun(loc.observer, date=d, tzinfo=TZ)
    return s["sunrise"], s["sunset"]

def _clock(text):
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text)
    if not m:
        return None
    h = int(m.group(1)) % 12
    if m.group(3) == "pm":
        h += 12
    return dtime(h, int(m.group(2) or 0))

def _offset(text, anchor):
    # returns a timedelta if <anchor> is present, else None. Zero if no number given.
    if anchor not in text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\s+" + anchor, text)
    if not m:
        return timedelta(0)
    n = float(m.group(1))
    unit = m.group(2)
    return timedelta(hours=n) if unit.startswith(("hour", "hr")) else timedelta(minutes=n)

def compute_window(rule, sunrise, sunset, d):
    """Best-effort start/end from a Time-of-Day rule. Returns (start, end)."""
    t = normalize(rule)
    start = end = None

    off = _offset(t, "before sunrise")
    if off is not None:
        start = sunrise - off
    elif (off := _offset(t, "after sunrise")) is not None:
        start = sunrise + off
    elif (off := _offset(t, "after sunset")) is not None:
        start = sunset + off

    off = _offset(t, "before sunset")
    if off is not None:
        end = sunset - off

    if ("no later than" in t) or ("until" in t):
        tail = t.split("no later than")[-1] if "no later than" in t else t.split("until")[-1]
        c = _clock(tail)
        if c:
            end = datetime.combine(d, c).replace(tzinfo=TZ)
            if start and end <= start:
                end += timedelta(days=1)

    if start is None and end is None and "daylight" in t:
        start, end = sunrise, sunset

    return start, end

def fmt(dt):
    return dt.strftime("%H:%M") if dt else "-"

# =========================
# SAFETY FORMS
# =========================
def safety_forms(vehicle, first_day, water_ice, prime, drive_hr):
    flha_when = "Daily (Prime Contractor)" if prime else "Daily"
    submit = [("FLHA / Tailgate", flha_when, "SafetyAdmin")]
    if first_day:
        submit.append(("Kickoff + ERP / First Aid", "First day", "SafetyAdmin"))
    if vehicle == "AiM-owned":
        submit.append(("Vehicle inspection", "Daily", "Fleetio"))
    elif vehicle == "Personal":
        submit.append(("Vehicle inspection", "Monthly", "SafetyAdmin"))
    elif vehicle == "Rental":
        submit.append(("Vehicle inspection", "Daily", "Fleetio"))
    if water_ice == "Water":
        submit.append(("Working on Water", "Before water work", "SafetyAdmin"))
    elif water_ice == "Ice":
        submit.append(("Working on Ice + Ice Rod", "Before ice work", "SafetyAdmin"))
    if drive_hr > 3:
        submit.append(("Journey Mgmt Plan", "Drive over 3 hr", "SafetyAdmin"))
    as_needed = "Hazard ID, Near Miss, Incident"
    return submit, as_needed

# =========================
# UI
# =========================
tab_browse, tab_compare, tab_search, tab_plan = st.tabs(
    ["Browse", "Compare", "Search", "Plan Day"]
)

# ---- BROWSE ----
with tab_browse:
    survey = st.selectbox("Survey type", survey_types, key="browse_survey")
    st.header(survey)

    photo = get_photo(survey)
    if photo:
        left, right = st.columns([2, 3])
        with left:
            st.image(str(photo), use_container_width=True)
        st.divider()

    for field in fields:
        value = get_value(field, survey)
        if not value:
            continue
        st.markdown(f"**{field}**")
        st.write(value)
        st.divider()

# ---- COMPARE ----
with tab_compare:
    picks = st.multiselect(
        "Survey types to compare",
        survey_types,
        default=survey_types[:2],
        key="compare_picks",
    )
    if picks:
        for field in fields:
            values = [get_value(field, s) for s in picks]
            if not any(values):
                continue
            st.markdown(f"### {field}")
            cols = st.columns(len(picks))
            for col, survey, value in zip(cols, picks, values):
                with col:
                    st.caption(survey)
                    st.write(value if value else "Not specified")
            st.divider()
    else:
        st.info("Pick at least one survey type.")

# ---- SEARCH ----
with tab_search:
    query = st.text_input(
        "Search all guidelines",
        placeholder="wind, sunset, egg searches, temperature, transect...",
        key="search_query",
    )
    if query.strip():
        q = query.strip().lower()
        hits = []
        for field in fields:
            for survey in survey_types:
                value = get_value(field, survey)
                if value and q in value.lower():
                    hits.append((survey, field, value))
        if hits:
            st.caption(f"{len(hits)} match(es)")
            for survey, field, value in hits:
                st.markdown(f"**{field}  ·  {survey}**")
                st.write(value)
                st.divider()
        else:
            st.info("No matches.")

# ---- PLAN DAY ----
with tab_plan:
    c1, c2, c3 = st.columns(3)
    with c1:
        plan_date = st.date_input("Date", key="plan_date")
        loc_name = st.selectbox("Location", list(LOCATIONS.keys()), key="plan_loc")
    with c2:
        if LOCATIONS[loc_name] is None:
            lat = st.number_input("Latitude", value=51.05, format="%.4f")
            lon = st.number_input("Longitude", value=-114.07, format="%.4f")
        else:
            lat, lon = LOCATIONS[loc_name]
        site_type = st.radio("Site type", ["Non-linear", "Linear"], horizontal=True)
    with c3:
        vehicle = st.selectbox("Vehicle", ["AiM-owned", "Personal", "Rental", "None"])
        first_day = st.checkbox("First day of project")
        water_ice = st.selectbox("Water / ice work", ["None", "Water", "Ice"])
        drive_hr = st.number_input("Drive one-way (hr)", min_value=0.0, value=0.0, step=0.5)
        prime = st.checkbox(
            "AiM is Prime Contractor",
            help=(
                "Only when AiM is Prime Contractor on a site with multiple trades "
                "or subcontractors working under us (e.g. large construction). "
                "Rare for standalone wildlife surveys. Submitted as the FLHA Tailgate."
            ),
        )

    plan_surveys = st.multiselect("Surveys this day", survey_types, key="plan_surveys")

    if not ASTRAL_OK:
        st.warning("Add 'astral' to requirements.txt to compute sunrise/sunset.")

    if plan_surveys:
        sunrise = sunset = None
        if ASTRAL_OK:
            sunrise, sunset = sun_times(lat, lon, plan_date)

        # Header line
        st.markdown(f"### {plan_date:%b %d, %Y}  ·  {loc_name}")
        bits = [f"{site_type} site"]
        if ASTRAL_OK:
            bits = [f"Sunrise {fmt(sunrise)}", f"Sunset {fmt(sunset)}"] + bits
        st.caption("  ·  ".join(bits))

        # Timeline: one row per survey, sorted by start time
        rows = []
        for s in plan_surveys:
            rule = field_value("Time of Day", s)
            start = end = None
            if ASTRAL_OK and rule:
                start, end = compute_window(rule, sunrise, sunset, plan_date)
            when = f"{fmt(start)}-{fmt(end)}" if (start and end) else (rule or "-")
            rows.append({
                "Survey": s,
                "What": field_value("Method", s) or "-",
                "When": when,
                "Crew": field_value("Required Survey Personnel", s) or "-",
                "_sort": start.timestamp() if start else float("inf"),
            })
        rows.sort(key=lambda r: r["_sort"])
        st.table(pd.DataFrame([{k: v for k, v in r.items() if k != "_sort"} for r in rows]))

        # Forms
        submit, as_needed = safety_forms(vehicle, first_day, water_ice, prime, drive_hr)
        st.markdown("**Forms to submit**")
        st.table(pd.DataFrame(submit, columns=["Form", "When", "Where"]))
        st.caption(f"As needed: {as_needed}")
    else:
        st.info("Pick at least one survey to build the day plan.")
