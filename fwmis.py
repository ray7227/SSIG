"""
FWMIS tab for the AiM Field Assistant.

Reuses the helpers and the embedded FWMIS template from your existing
fwmis_loadform_app.py (kept in the same folder, unchanged). We execute only
the top part of that file (everything before its UI) so none of its page
setup runs here, then expose render_fwmis() for the FWMIS tab.

You type the project name and the observations in the app; everything else
(description, datum, confidence, measurement source, sensitive flag) fills
itself. No tracker, no upload.
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

OBS_COLS = ["Species", "Observation", "Lat, Long", "ATS Start", "ATS End", "Comments"]

NAME_HELP = ("Format: client, project, then \"Wildlife Sweep\". "
             "Example: Keyera GWD 31640 and 31670 Wildlife Sweep. "
             "The description auto-fills from this and the sweep date.")
LICENCE_HELP = ("The research licence number is for species wildlife surveys, typically where "
                "playback surveys or capture occurs. Leave as NA for a standard sweep.")


def _build_loadform(project, survey, rows):
    """Like the standalone build, but measurement source is per row."""
    zin = zipfile.ZipFile(io.BytesIO(TEMPLATE))  # noqa: F821
    paths = sheet_paths(zin)  # noqa: F821
    sst = SharedStrings(zin.read("xl/sharedStrings.xml"))  # noqa: F821

    proj = SheetXml(zin.read(paths[PROJECT_SHEET]))  # noqa: F821
    proj.clear_values_from(FIRST_DATA_ROW)  # noqa: F821
    for key, (col, style) in PROJECT_CELLS.items():  # noqa: F821
        proj.set(f"{col}{FIRST_DATA_ROW}", project.get(key), sst, style)  # noqa: F821

    surv = SheetXml(zin.read(paths[SURVEY_SHEET]))  # noqa: F821
    surv.clear_values_from(FIRST_DATA_ROW)  # noqa: F821
    for i, r in enumerate(rows):
        values = {
            "survey_type": survey["survey_type"],
            "start": excel_serial(survey["start"]),  # noqa: F821
            "end": excel_serial(survey["end"]),  # noqa: F821
            "sensitive": survey["sensitive"],
            "loc_type": r["loc_type"],
            "gis_source": r.get("gis_source") or survey["gis_source"],
            "datum": survey["datum"],
            "in_alberta": "Y",
            "lat": round(float(r["lat"]), 6) if r.get("lat") is not None else None,
            "lon": round(float(r["lon"]), 6) if r.get("lon") is not None else None,
            "loc_comments": r.get("loc_comments"),
            "feature": r["feature"],
            "obs_date": excel_serial(r["date"]),  # noqa: F821
            "confidence": survey["confidence"],
            "species": r["species"],
            "comments": r["comment"],
        }
        for prefix, ats in (("s_", r.get("ats_start")), ("e_", r.get("ats_end"))):
            ats = ats or {}
            values.update({prefix + "lsd": ats.get("lsd"), prefix + "quarter": ats.get("quarter"),
                           prefix + "section": ats.get("section"), prefix + "township": ats.get("township"),
                           prefix + "range": ats.get("range"), prefix + "meridian": ats.get("meridian")})
        for key, (col, style) in SURVEY_COLS.items():  # noqa: F821
            surv.set(f"{col}{FIRST_DATA_ROW + i}", values[key], sst, style)  # noqa: F821

    changed = {
        paths[PROJECT_SHEET]: proj.to_bytes(),  # noqa: F821
        paths[SURVEY_SHEET]: surv.to_bytes(),  # noqa: F821
        "xl/sharedStrings.xml": sst.to_bytes(),
    }
    out = io.BytesIO()  # noqa: F821
    with zipfile.ZipFile(out, "w") as zout:  # noqa: F821
        for info in zin.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)  # noqa: F821
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zout.writestr(zi, changed.get(info.filename, zin.read(info.filename)))
    return out.getvalue()


def render_fwmis():
    st.subheader("FWMIS Loadform Builder")
    st.caption("Type the project name and the observations, then download the FWMIS loadform.")

    lists = read_code_lists(TEMPLATE)  # noqa: F821
    SPECIES, FEATURES = lists["tax_TAXONOMIC"], lists["inv_SITFEATURE"]
    SITE = pick_option(lists["inv_LOCTYPE"], "SITE")  # noqa: F821
    TRANSECT = pick_option(lists["inv_LOCTYPE"], "TRANSECT")  # noqa: F821
    OTHER = pick_option(FEATURES, "OTHR")  # noqa: F821  used when Observation is blank (visual)
    GPS = pick_option(lists["inv_GISSOURCE"], "GPS")  # noqa: F821
    DERIVED = pick_option(lists["inv_GISSOURCE"], "DERIVED")  # noqa: F821
    DATUM = pick_option(lists["inv_GISDATUM"], "NAD 83")  # noqa: F821
    CONFIDENCE = pick_option(lists["inv_CONFDNTLVL"], "CONFIRMED")  # noqa: F821

    def default(options, *wanted):
        for w in wanted:
            o = pick_option(options, w)  # noqa: F821
            if o:
                return o
        return options[0]

    # --- Project details ---------------------------------------------------
    st.markdown("**Project**")
    c1, c2 = st.columns([2, 1])
    proj_name = c1.text_input("Project name", key="fwmis_name", help=NAME_HELP,
                              placeholder="Keyera GWD 31640 and 31670 Wildlife Sweep")
    licence = c2.text_input("Research licence number", value="NA", key="fwmis_lic", help=LICENCE_HELP)

    c1, c2 = st.columns(2)
    sweep_date = c1.date_input("Sweep date", format="YYYY-MM-DD", key="fwmis_date")
    survey_type = c2.selectbox("Survey type", lists["inv_SURVEYTYPE"],
                               index=lists["inv_SURVEYTYPE"].index(default(lists["inv_SURVEYTYPE"], "AREASEARCH")),
                               key="fwmis_stype")

    date_str = f"{sweep_date:%B} {sweep_date.day}, {sweep_date.year}"
    proj_desc = (f"A preconstruction wildlife sweep was completed on {date_str}, for {proj_name.strip()}."
                 if proj_name.strip() else "")

    # --- Observations ------------------------------------------------------
    st.markdown("**Observations** (one row each)")
    st.caption("Leave Observation blank for a plain visual sighting. Give each row a Lat, Long "
               "(e.g. 55.1234, -119.1234) or an ATS Start (e.g. NE-16-75-10-W6M). "
               "ATS End only if it crosses into a second quarter.")

    c1, c2 = st.columns(2)
    common_s = c1.text_input("ATS Start for all rows (optional)", key="fwmis_ats_s",
                             help="Used for any row that has no Lat, Long and no ATS of its own.")
    common_e = c2.text_input("ATS End for all rows (optional)", key="fwmis_ats_e")

    if "fwmis_rows" not in st.session_state:
        st.session_state["fwmis_rows"] = pd.DataFrame([{c: "" for c in OBS_COLS} for _ in range(8)])

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
            row.update(lat=lat, lon=lon, loc_type=SITE, gis_source=GPS)
            if not (ALBERTA_LAT[0] <= lat <= ALBERTA_LAT[1]) or not (ALBERTA_LON[0] <= lon <= ALBERTA_LON[1]):  # noqa: F821
                errors.append(f"Row {n}: {lat}, {lon} is outside Alberta (decimal degrees, longitude negative).")
        else:
            if not ats_s_text and not ats_e_text:   # fall back to the common ATS
                ats_s_text, ats_e_text = common_s.strip(), common_e.strip()
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
                       loc_type=TRANSECT if e_ats else SITE, gis_source=DERIVED)
        rows.append(row)

    if not proj_name.strip():
        errors.append("Add a project name.")
    if not rows:
        errors.append("Add at least one observation.")

    for e in errors:
        st.error(e)

    file_bytes = None
    if not errors:
        file_bytes = _build_loadform(
            project={"name": proj_name.strip(), "description": proj_desc, "licence": licence.strip()},
            survey={"survey_type": survey_type, "start": sweep_date, "end": sweep_date, "sensitive": "N",
                    "gis_source": DERIVED, "datum": DATUM, "confidence": CONFIDENCE},
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
