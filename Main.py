import pandas as pd
import streamlit as st

# =========================
# PAGE SETUP
# =========================
st.set_page_config(
    page_title="SSIG Assistant",
    page_icon="🦉",
    layout="wide",
)
st.title("SSIG Field Survey Assistant")

FILE = "SSIG_Breakdown.xlsx"   # keep the no-images copy here

# =========================
# LOAD SPREADSHEET
# =========================
@st.cache_data
def load_data(path):
    # Row 0 = header (Category | survey type | survey type | ...)
    # Column 0 = field names (Species, Status, Method, Survey Window, ...)
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

# =========================
# UI
# =========================
tab_browse, tab_compare, tab_search = st.tabs(["Browse", "Compare", "Search"])

# ---- BROWSE ----
with tab_browse:
    survey = st.selectbox("Survey type", survey_types, key="browse_survey")
    st.header(survey)
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
