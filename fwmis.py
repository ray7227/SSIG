"""
FWMIS tab for the AiM Field Assistant.

Reuses the helpers and the embedded FWMIS template from your existing
fwmis_loadform_app.py (kept in the same folder, unchanged). We execute only
the top part of that file (everything before its UI) so none of its page
setup runs here, then expose render_fwmis() for the FWMIS tab.

You type the project info and the observations in the app; it fills the
loadform from that. No tracker, no upload.
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

FEATURE_COLS = ["Species", "Observation", "Lat, Long", "ATS Start", "ATS End", "Comments"]

SOURCE_HELP = ("How the coordinates were obtained. "
               "Derived = digitized from mapping or imagery. "
               "GPS Specified = recorded with a GPS unit. "
               "Unknown = not recorded.")
DATUM_HELP = ("Coordinate datum. NAD 83 = current standard, use this unless told otherwise. "
              "NAD 27 = older datum. Unknown = not recorded.")
LICENCE_HELP = ("The research licence number is for species wildlife surveys, typically where "
                "playback surveys or capture occurs. Leave as NA for a standard sweep.")


def render_fwmis():
    st.subheader("FWMIS Loadform Builder")
    st.caption("Fill in the project info and the observations, then download the FWMIS loadform.")

    lists = read_code_lists(TEMPLATE)  # noqa: F821
    SPECIES, FEATURES = lists["tax_TAXONOMIC"], lists["inv_SITFEATURE"]
    SITE = pick_option(lists["inv_LOCTYPE"], "SITE")  # noqa: F821
    TRANSECT = pick_option(lists["inv_LOCTYPE"], "TRANSECT")  # noqa: F821
    OTHER = pick_option(FEATURES, "OTHR")  # noqa: F821  used when Observation is blank (visual)

    def default(options, *wanted):
        for w in wanted:
            o = pick_option(options, w)  # noqa: F821
            if o:
                return o
        return options[0]

    # --- Project details ---------------------------------------------------
    st.markdown("**Project**")
    c1, c2 = st.columns([2, 1])
    proj_name = c1.text_input("Project name", key="fwmis_name",
                              placeholder="Client name Project name Wildlife Sweep")
    licence = c2.text_input("Research licence number", value="NA", key="fwmis_lic", help=LICENCE_HELP)

    c1, c2, c3, c4 = st.columns(4)
    sweep_date = c1.date_input("Sweep date", format="YYYY-MM-DD", key="fwmis_date")
    survey_type = c2.selectbox("Survey type", lists["inv_SURVEYTYPE"],
                               index=lists["inv_SURVEYTYPE"].index(default(lists["inv_SURVEYTYPE"], "AREASEARCH")),
                               key="fwmis_stype")
    gis_source = c3.selectbox("Measurement source", lists["inv_GISSOURCE"],
                              index=lists["inv_GISSOURCE"].index(default(lists["inv_GISSOURCE"], "DERIVED")),
                              key="fwmis_src", help=SOURCE_HELP)
    datum = c4.selectbox("Datum", lists["inv_GISDATUM"],
                         index=lists["inv_GISDATUM"].index(default(lists["inv_GISDATUM"], "NAD 83", "NAD83")),
                         key="fwmis_datum", help=DATUM_HELP)

    confidence = st.selectbox("Confidence", lists["inv_CONFDNTLVL"],
                              index=lists["inv_CONFDNTLVL"].index(default(lists["inv_CONFDNTLVL"], "CONFIRMED")),
                              key="fwmis_conf")

    date_str = f"{sweep_date:%B} {sweep_date.day}, {sweep_date.year}"
    auto_desc = (f"A preconstruction wildlife sweep was completed on {date_str}, for {proj_name.strip()}."
                 if proj_name.strip() else "")
    proj_desc = st.text_area(
        "Project description (leave blank to auto-fill from the project name and sweep date)",
        height=80, key="fwmis_desc", placeholder=auto_desc)
    proj_desc = proj_desc.strip() or auto_desc
    sensitive = "N"  # sweeps are never sensitive

    # --- Observations ------------------------------------------------------
    st.markdown("**Observations** (one row each)")
    st.caption("Leave Observation blank for a plain visual sighting. "
               "Give either a Lat, Long (e.g. 55.1234, -119.1234) or an ATS Start "
               "(e.g. NE-16-75-10-W6M); ATS End only if it spans into a second quarter.")
    if "fwmis_rows" not in st.session_state:
        st.session_state["fwmis_rows"] = pd.DataFrame([{c: "" for c in FEATURE_COLS} for _ in range(8)])

    edited = st.data_editor(
        st.session_state["fwmis_rows"], num_rows="dynamic", hide_index=True, key="fwmis_editor",
        column_config={
            "Species": st.column_config.SelectboxColumn("Species", options=SPECIES, width="large"),
            "Observation": st.column_config.SelectboxColumn("Observation", options=FEATURES, width="medium"),
            "Lat, Long": st.column_config.TextColumn("Lat, Long"),
            "ATS Start": st.column_config.TextColumn("ATS Start"),
            "ATS End": st.column_config.TextColumn("ATS End"),
            "Comments": st.column_config.TextColumn("Comments", width="large"),
        })

    # --- Build rows --------------------------------------------------------
    rows, errors = [], []
    for i, r in edited.reset_index(drop=True).iterrows():
        sp, ft = r.get("Species"), r.get("Observation")
        ll_text = str(r.get("Lat, Long") or "").strip()
        ats_s_text = str(r.get("ATS Start") or "").strip()
        ats_e_text = str(r.get("ATS End") or "").strip()
        if not sp and not ft and not ll_text and not ats_s_text and not ats_e_text:
            continue  # blank row
        n = i + 1
        if not sp:
            errors.append(f"Row {n}: pick a species.")
            continue
        comment = str(r.get("Comments") or "").strip()
        if not ft:                       # plain visual sighting
            ft = OTHER
            comment = comment or "Visual observation"
        row = {"row": n, "date": sweep_date, "species": sp, "feature": ft,
               "comment": comment, "lat": None, "lon": None}

        ll = parse_latlon(ll_text) if ll_text else None  # noqa: F821
        if ll:
            lat, lon = ll
            row.update(lat=lat, lon=lon, loc_type=SITE)
            if not (ALBERTA_LAT[0] <= lat <= ALBERTA_LAT[1]) or not (ALBERTA_LON[0] <= lon <= ALBERTA_LON[1]):  # noqa: F821
                errors.append(f"Row {n}: {lat}, {lon} is outside Alberta (decimal degrees, longitude negative).")
        else:
            s_ats = parse_ats(ats_s_text)  # noqa: F821
            e_ats = parse_ats(ats_e_text)  # noqa: F821
            if s_ats is None and e_ats is None:
                if ats_s_text or ats_e_text:
                    errors.append(f"Row {n}: ATS doesn't look right. Use e.g. NE-16-75-10-W6M.")
                else:
                    errors.append(f"Row {n}: add a Lat, Long or an ATS Start.")
                continue
            if s_ats is None:            # only an end given: use it as the site
                s_ats, e_ats = e_ats, None
            if e_ats == s_ats:
                e_ats = None
            row.update(ats_start=ats_to_fwmis(s_ats, lists), ats_end=ats_to_fwmis(e_ats, lists),  # noqa: F821
                       loc_type=TRANSECT if e_ats else SITE)
        rows.append(row)

    if not proj_name.strip():
        errors.append("Add a project name.")
    if not proj_desc.strip():
        errors.append("Add a project description (or a project name so it can auto-fill).")
    if not rows:
        errors.append("Add at least one observation.")

    for e in errors:
        st.error(e)

    file_bytes = None
    if not errors:
        file_bytes = build_loadform(  # noqa: F821
            TEMPLATE,
            project={"name": proj_name.strip(), "description": proj_desc.strip(), "licence": licence.strip()},
            survey={"survey_type": survey_type, "start": sweep_date, "end": sweep_date, "sensitive": sensitive,
                    "gis_source": gis_source, "datum": datum, "confidence": confidence},
            rows=rows,
        )

    safe = re.sub(r"[^A-Za-z0-9]+", "_", proj_name).strip("_") or "Project"  # noqa: F821
    st.download_button(
        f"Download loadform ({len(rows)} rows)" if file_bytes else "Download loadform",
        data=file_bytes or b"",
        file_name=f"FWMIS_LOADFORM_{safe}_{sweep_date:%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=file_bytes is None,
        type="primary",
        key="fwmis_dl",
    )
