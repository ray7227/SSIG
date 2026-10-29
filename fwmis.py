"""
FWMIS tab for the AiM Field Assistant.

This reuses the helpers and the embedded FWMIS template from your existing
fwmis_loadform_app.py (kept in the same folder, unchanged). We execute only
the top part of that file (everything before its UI) so none of its page
setup runs here, then expose render_fwmis() for the FWMIS tab.

You type the project info and feature rows in the app, and it fills the
loadform from that. No tracker upload.
"""

import base64
from pathlib import Path

import pandas as pd
import streamlit as st

# ---- Load helpers + embedded template from the standalone app --------------
_PATH = Path(__file__).with_name("fwmis_loadform_app.py")
_SRC = _PATH.read_text(encoding="utf-8")
_HELPERS = _SRC.split("st.set_page_config", 1)[0]   # cut off the standalone UI
_NS = {"__file__": str(_PATH), "__name__": "fwmis_loadform_app"}
exec(compile(_HELPERS, str(_PATH), "exec"), _NS)
for _k, _v in _NS.items():                          # pull in helpers + constants only
    if not _k.startswith("__"):
        globals()[_k] = _v

TEMPLATE = base64.b64decode(TEMPLATE_B64)  # noqa: F821  (from _NS)

FEATURE_COLS = ["Species", "Feature", "Date", "Lat, Long", "ATS Start", "ATS End", "KP", "Comments"]


def render_fwmis():
    st.subheader("FWMIS Loadform Builder")
    st.caption("Fill in the project info and the features, then download the FWMIS loadform.")

    lists = read_code_lists(TEMPLATE)  # noqa: F821
    SPECIES, FEATURES = lists["tax_TAXONOMIC"], lists["inv_SITFEATURE"]

    def default(options, *wanted):
        for w in wanted:
            o = pick_option(options, w)  # noqa: F821
            if o:
                return o
        return options[0]

    # --- Project details ---------------------------------------------------
    st.markdown("**Project**")
    c1, c2 = st.columns([2, 1])
    proj_name = c1.text_input("Project name", key="fwmis_name")
    licence = c2.text_input("Research licence number", value="NA", key="fwmis_lic")
    proj_desc = st.text_area("Project description", height=90, key="fwmis_desc")

    c1, c2, c3, c4 = st.columns(4)
    survey_start = c1.date_input("Survey start", format="YYYY-MM-DD", key="fwmis_start")
    survey_end = c2.date_input("Survey end", format="YYYY-MM-DD", key="fwmis_end")
    survey_type = c3.selectbox("Survey type", lists["inv_SURVEYTYPE"],
                               index=lists["inv_SURVEYTYPE"].index(default(lists["inv_SURVEYTYPE"], "AREASEARCH")),
                               key="fwmis_stype")
    sensitive = c4.selectbox("Sensitive data", ["N", "Y"], key="fwmis_sens")

    c1, c2, c3 = st.columns(3)
    gis_source = c1.selectbox("Measurement source", lists["inv_GISSOURCE"],
                              index=lists["inv_GISSOURCE"].index(default(lists["inv_GISSOURCE"], "DERIVED")),
                              key="fwmis_src")
    datum = c2.selectbox("Datum", lists["inv_GISDATUM"],
                         index=lists["inv_GISDATUM"].index(default(lists["inv_GISDATUM"], "NAD 83", "NAD83")),
                         key="fwmis_datum")
    confidence = c3.selectbox("Confidence", lists["inv_CONFDNTLVL"],
                              index=lists["inv_CONFDNTLVL"].index(default(lists["inv_CONFDNTLVL"], "CONFIRMED")),
                              key="fwmis_conf")

    SITE = pick_option(lists["inv_LOCTYPE"], "SITE")  # noqa: F821
    TRANSECT = pick_option(lists["inv_LOCTYPE"], "TRANSECT")  # noqa: F821

    # --- Features ----------------------------------------------------------
    st.markdown("**Features** (add a row per observation)")
    st.caption("Fill Lat, Long (e.g. 55.1234, -119.1234). If blank, ATS Start/End is used. "
               "KP goes into location comments.")
    if "fwmis_rows" not in st.session_state:
        st.session_state["fwmis_rows"] = pd.DataFrame(
            [{c: (survey_start if c == "Date" else "") for c in FEATURE_COLS} for _ in range(5)])

    edited = st.data_editor(
        st.session_state["fwmis_rows"], num_rows="dynamic", hide_index=True, key="fwmis_editor",
        column_config={
            "Species": st.column_config.SelectboxColumn("Species", options=SPECIES, width="large"),
            "Feature": st.column_config.SelectboxColumn("Feature", options=FEATURES, width="medium"),
            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
            "Lat, Long": st.column_config.TextColumn("Lat, Long"),
            "ATS Start": st.column_config.TextColumn("ATS Start"),
            "ATS End": st.column_config.TextColumn("ATS End"),
            "KP": st.column_config.TextColumn("KP"),
            "Comments": st.column_config.TextColumn("Comments", width="large"),
        })

    # --- Build rows --------------------------------------------------------
    rows, errors = [], []
    for i, r in edited.reset_index(drop=True).iterrows():
        sp, ft = r.get("Species"), r.get("Feature")
        ll_text = str(r.get("Lat, Long") or "").strip()
        ats_s_text = str(r.get("ATS Start") or "").strip()
        if not sp and not ft and not ll_text and not ats_s_text:
            continue  # blank row
        n = i + 1
        if not sp:
            errors.append(f"Row {n}: pick a species.")
            continue
        if not ft:
            errors.append(f"Row {n}: pick a feature.")
            continue
        date = to_date(r.get("Date")) or survey_start  # noqa: F821
        comment = str(r.get("Comments") or "").strip()
        row = {"row": n, "date": date, "species": sp, "feature": ft,
               "comment": comment, "lat": None, "lon": None}

        ll = parse_latlon(ll_text) if ll_text else None  # noqa: F821
        if ll:
            lat, lon = ll
            row.update(lat=lat, lon=lon, loc_type=SITE)
            if not (ALBERTA_LAT[0] <= lat <= ALBERTA_LAT[1]) or not (ALBERTA_LON[0] <= lon <= ALBERTA_LON[1]):  # noqa: F821
                errors.append(f"Row {n}: {lat}, {lon} is outside Alberta (decimal degrees, longitude negative).")
        else:
            s_ats = parse_ats(ats_s_text)  # noqa: F821
            e_ats = parse_ats(r.get("ATS End"))  # noqa: F821
            if s_ats is None and e_ats is None:
                errors.append(f"Row {n}: add a Lat, Long or an ATS Start (e.g. NE-16-75-10-W6M).")
                continue
            if s_ats is None:
                s_ats, e_ats = e_ats, None
            if e_ats == s_ats:
                e_ats = None
            kp = str(r.get("KP") or "").strip()
            row.update(ats_start=ats_to_fwmis(s_ats, lists), ats_end=ats_to_fwmis(e_ats, lists),  # noqa: F821
                       loc_type=TRANSECT if e_ats else SITE, loc_comments=kp or None)
        rows.append(row)

    if not proj_name.strip():
        errors.append("Add a project name.")
    if not proj_desc.strip():
        errors.append("Add a project description.")
    if survey_end < survey_start:
        errors.append("Survey end is before survey start.")
    if not rows:
        errors.append("Add at least one feature row.")

    for e in errors:
        st.error(e)

    file_bytes = None
    if not errors:
        file_bytes = build_loadform(  # noqa: F821
            TEMPLATE,
            project={"name": proj_name.strip(), "description": proj_desc.strip(), "licence": licence.strip()},
            survey={"survey_type": survey_type, "start": survey_start, "end": survey_end, "sensitive": sensitive,
                    "gis_source": gis_source, "datum": datum, "confidence": confidence},
            rows=rows,
        )

    safe = re.sub(r"[^A-Za-z0-9]+", "_", proj_name).strip("_") or "Project"  # noqa: F821
    st.download_button(
        f"Download loadform ({len(rows)} rows)" if file_bytes else "Download loadform",
        data=file_bytes or b"",
        file_name=f"FWMIS_LOADFORM_{safe}_{survey_start:%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=file_bytes is None,
        type="primary",
        key="fwmis_dl",
    )
