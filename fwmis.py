"""
FWMIS tab for the AiM Field Assistant.

This reuses the helpers and the embedded FWMIS template from your existing
fwmis_loadform_app.py (kept in the same folder, unchanged). We execute only
the top part of that file (everything before its UI) so none of its page
setup runs here, then expose render_fwmis() for the FWMIS tab.
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


def render_fwmis():
    st.subheader("FWMIS Loadform Builder")
    st.caption("Drop in the wildlife sweep tracker and download the FWMIS loadform.")

    lists = read_code_lists(TEMPLATE)  # noqa: F821
    SPECIES, FEATURES = lists["tax_TAXONOMIC"], lists["inv_SITFEATURE"]

    folder = Path(__file__).parent
    found = sorted(
        [p for p in list(folder.glob("*.xlsx")) + list(folder.glob("*.xlsm"))
         if "tracker" in p.name.lower()],
        key=lambda p: p.stat().st_mtime, reverse=True)

    tracker_bytes = None
    if found:
        names = [p.name for p in found]
        pick = st.selectbox("Tracker (from the app folder)", names, key="fwmis_pick") if len(found) > 1 else names[0]
        if len(found) == 1:
            st.caption(f"Using {pick} from the app folder.")
        tracker_bytes = next(p for p in found if p.name == pick).read_bytes()
        with st.expander("Upload a different tracker instead"):
            up = st.file_uploader("Wildlife Sweep & Feature Tracker (.xlsx)", type=["xlsx", "xlsm"], key="fwmis_up")
            if up:
                tracker_bytes = up.getvalue()
    else:
        up = st.file_uploader("Wildlife Sweep & Feature Tracker (.xlsx)", type=["xlsx", "xlsm"], key="fwmis_up")
        if not up:
            st.info("Add a tracker .xlsx (filename containing \"tracker\") to the app folder, or upload one here. "
                    "Each feature uses its Lat, Long; if blank it uses the row's ATS Start and End.")
            return
        tracker_bytes = up.getvalue()

    try:
        df, info, meta = read_tracker(tracker_bytes)  # noqa: F821
    except Exception as e:
        st.error(f"Could not read the tracker: {e}")
        return

    if df.empty:
        st.error("No features found in the tracker.")
        return

    # --- Project details (from FWMIS Info sheet, editable) ------------------
    sweep_dates = [d for d in df["sweep_date"].tolist() + df["date"].tolist() if d]
    start_default = min(sweep_dates) if sweep_dates else dt.date.today()  # noqa: F821
    end_default = max(sweep_dates) if sweep_dates else dt.date.today()  # noqa: F821
    title_guess = re.sub(r"\s*Master Wildlife Sweep.*$", "", str(meta["title"] or "")).strip()  # noqa: F821
    auto_start, auto_end = project_extent(df)  # noqa: F821
    desc_override = str(info_get(info, "Description Override", default="")).strip()  # noqa: F821
    if desc_override:
        desc_default = desc_override
    elif info_get(info, "Client Name", "Project Scope"):  # noqa: F821
        desc_default = write_description(  # noqa: F821
            info_get(info, "Client Name", default=""), info_get(info, "Client Short Name", default=""),  # noqa: F821
            info_get(info, "Work Completed", default="wildlife sweep"), info_get(info, "Project Scope", default=""),  # noqa: F821
            info_get(info, "Start Location", default=auto_start), info_get(info, "End Location", default=auto_end))  # noqa: F821
    else:
        desc_default = str(info_get(info, "Project Description", default=""))  # noqa: F821

    with st.expander("Project details (filled from the tracker's FWMIS Info sheet)", expanded=not info):
        if not info:
            st.warning("This tracker has no FWMIS Info sheet, so fill these in here.")
        c1, c2 = st.columns([2, 1])
        proj_name = c1.text_input("Project name", value=str(info_get(info, "Project Name", default=title_guess)))  # noqa: F821
        licence = c2.text_input("Research licence number", value=str(info_get(info, "Research Licence Number", default="NA")))  # noqa: F821
        proj_desc = st.text_area("Project description (auto-written from FWMIS Info, edit here if needed)",
                                 value=desc_default, height=100, key="fwmis_desc_" + str(hash(desc_default)))
        c1, c2, c3, c4 = st.columns(4)
        survey_start = c1.date_input("Survey start", value=start_default, format="YYYY-MM-DD", key="fwmis_start")
        survey_end = c2.date_input("Survey end", value=end_default, format="YYYY-MM-DD", key="fwmis_end")

        def sel(col, label, options, value, fallback):
            choice = pick_option(options, value) or pick_option(options, fallback) or options[0]  # noqa: F821
            return col.selectbox(label, options, index=options.index(choice), key="fwmis_" + label)

        survey_type = sel(c3, "Survey type", lists["inv_SURVEYTYPE"], info_get(info, "Survey Type"), "AREASEARCH")  # noqa: F821
        sensitive = c4.selectbox("Sensitive data", ["N", "Y"], key="fwmis_sens",
                                 index=1 if str(info_get(info, "Sensitive Data (Y/N)", default="N")).upper().startswith("Y") else 0)  # noqa: F821
        c1, c2, c3, c4 = st.columns(4)
        gis_source = sel(c1, "Measurement source", lists["inv_GISSOURCE"], info_get(info, "Measurement Source"), "DERIVED")  # noqa: F821
        datum = sel(c2, "Datum", lists["inv_GISDATUM"], info_get(info, "Datum"), "NAD 83")  # noqa: F821
        confidence = sel(c3, "Confidence", lists["inv_CONFDNTLVL"], info_get(info, "Default Confidence"), "CONFIRMED")  # noqa: F821
        include_obs = c4.selectbox(
            "Include species observations", ["Y", "N"], key="fwmis_obs",
            index=1 if str(info_get(info, "Include Species Observations (Y/N)", default="Y")).upper().startswith("N") else 0)  # noqa: F821

    SITE = pick_option(lists["inv_LOCTYPE"], "SITE")  # noqa: F821
    TRANSECT = pick_option(lists["inv_LOCTYPE"], "TRANSECT")  # noqa: F821

    # --- Rows to submit: lat/long first, ATS start/end as fallback ----------
    work = df.copy()
    if include_obs == "N":
        work = work[~work["observation"].str.lower().str.contains("species observation")]
    work["has_xy"] = work["lat"].notna() & work["lon"].notna()
    work["ats_s"] = work["ats_start_text"].map(parse_ats)  # noqa: F821
    work["ats_e"] = work["ats_end_text"].map(parse_ats)  # noqa: F821
    work["use"] = "skip"
    work.loc[work["has_xy"], "use"] = "latlon"
    work.loc[~work["has_xy"] & (work["ats_s"].notna() | work["ats_e"].notna()), "use"] = "ats"
    bad_ll = work[(work["latlon_text"] != "") & ~work["has_xy"]]
    if len(bad_ll):
        st.warning("Could not read Lat, Long on tracker rows " + ", ".join(str(r) for r in bad_ll["row"].tolist()[:40])
                   + ". Use the format 55.123456, -119.123456. Those rows use ATS for now.")
    skipped = work[work["use"] == "skip"]
    work = work[work["use"] != "skip"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Features in tracker", len(df))
    c2.metric("Using lat/long", int((work["use"] == "latlon").sum()))
    c3.metric("Using ATS", int((work["use"] == "ats").sum()))
    c4.metric("Skipped", len(skipped))
    if len(skipped):
        rows_txt = ", ".join(str(r) for r in skipped["row"].tolist()[:40]) + (" ..." if len(skipped) > 40 else "")
        st.warning(f"{len(skipped)} features skipped: no lat/long and no readable ATS. Tracker rows: {rows_txt}")

    # --- Species and feature matching --------------------------------------
    saved_sp, saved_ft = load_saved_maps()  # noqa: F821

    def match_table(values, saved, auto_fn, options):
        rows = []
        for v, n in values.items():
            auto = saved.get(v) if saved.get(v) in options else auto_fn(v)
            rows.append({"From tracker": v, "FWMIS": auto, "Rows": n, "_auto": auto})
        return pd.DataFrame(rows, columns=["From tracker", "FWMIS", "Rows", "_auto"])

    sp_counts = work["species_name"].value_counts().sort_index()
    ft_counts = work["observation"].value_counts().sort_index()
    sp_tbl = match_table(sp_counts, saved_sp, lambda v: auto_species(v, SPECIES), SPECIES)  # noqa: F821
    ft_tbl = match_table(ft_counts, saved_ft, lambda v: auto_feature(v, FEATURES), FEATURES)  # noqa: F821
    unmatched = int(sp_tbl["FWMIS"].isna().sum()) if len(sp_tbl) else 0

    with st.expander(f"Species and feature matches ({unmatched} need picking)" if unmatched
                     else "Species and feature matches (all matched)", expanded=bool(unmatched)):
        st.caption("Change any match here. Changes are remembered for next time.")
        c1, c2 = st.columns(2)
        sp_edit = c1.data_editor(
            sp_tbl.drop(columns="_auto"), key="fwmis_sp_" + str(hash(tuple(sp_tbl["From tracker"]))),
            hide_index=True, disabled=["From tracker", "Rows"],
            column_config={"FWMIS": st.column_config.SelectboxColumn("FWMIS species", options=SPECIES, width="medium")})  # noqa: F821
        ft_edit = c2.data_editor(
            ft_tbl.drop(columns="_auto"), key="fwmis_ft_" + str(hash(tuple(ft_tbl["From tracker"]))),
            hide_index=True, disabled=["From tracker", "Rows"],
            column_config={"FWMIS": st.column_config.SelectboxColumn("FWMIS feature", options=FEATURES, width="medium")})  # noqa: F821

    changed = False
    for edit, tbl, store in ((sp_edit, sp_tbl, saved_sp), (ft_edit, ft_tbl, saved_ft)):
        for (_, e), (_, t) in zip(edit.iterrows(), tbl.iterrows()):
            if e["FWMIS"] and e["FWMIS"] != t["_auto"] and store.get(e["From tracker"]) != e["FWMIS"]:
                store[e["From tracker"]] = e["FWMIS"]
                changed = True
    if changed:
        save_maps(saved_sp, saved_ft)  # noqa: F821

    sp_map = dict(zip(sp_edit["From tracker"], sp_edit["FWMIS"]))
    ft_map = dict(zip(ft_edit["From tracker"], ft_edit["FWMIS"]))

    # --- Build rows ---------------------------------------------------------
    rows, errors = [], []
    for _, r in work.iterrows():
        comment = r["observation"]
        if r["status"] and r["status"].upper() not in ("N/A", "NA"):
            comment = f"{comment} ({r['status']})"
        row = {"row": r["row"], "date": r["date"] or survey_start, "species": sp_map.get(r["species_name"]),
               "feature": ft_map.get(r["observation"]), "comment": comment, "lat": None, "lon": None}
        if r["use"] == "latlon":
            row.update(lat=r["lat"], lon=r["lon"], loc_type=SITE, where=f"{r['lat']:.6f}, {r['lon']:.6f}")
        else:
            s_ats, e_ats = r["ats_s"], r["ats_e"]
            if s_ats is None:
                s_ats, e_ats = e_ats, None
            if e_ats == s_ats:
                e_ats = None
            kps = " to ".join(k for k in (r["kp_start"], r["kp_end"]) if k)
            row.update(ats_start=ats_to_fwmis(s_ats, lists), ats_end=ats_to_fwmis(e_ats, lists),  # noqa: F821
                       loc_type=TRANSECT if e_ats else SITE, loc_comments=kps or None,
                       where=" to ".join(t for t, a in ((r["ats_start_text"], r["ats_s"]), (r["ats_end_text"], r["ats_e"])) if a))
        rows.append(row)

    if not proj_name.strip():
        errors.append("Add a project name (FWMIS Info sheet, or Project details above).")
    if not proj_desc.strip():
        errors.append("Add a project description (FWMIS Info sheet, or Project details above).")
    if survey_end < survey_start:
        errors.append("Survey end is before survey start.")
    if not rows:
        errors.append("No features have a location. Fill in Lat, Long or ATS Start/End in the tracker.")
    for name in [k for k, v in sp_map.items() if not v]:
        errors.append(f"Pick an FWMIS species for '{name}' in Species and feature matches.")
    for r in rows:
        if r["lat"] is None:
            continue
        if not (ALBERTA_LAT[0] <= r["lat"] <= ALBERTA_LAT[1]) or not (ALBERTA_LON[0] <= r["lon"] <= ALBERTA_LON[1]):  # noqa: F821
            errors.append(f"Tracker row {r['row']}: {r['lat']}, {r['lon']} is outside Alberta. Check the coordinates "
                          "(decimal degrees, longitude negative).")

    if rows:
        with st.expander(f"Preview ({len(rows)} rows)"):
            st.dataframe(pd.DataFrame([{
                "Tracker row": r["row"], "Date": r["date"], "Species": r["species"], "Feature": r["feature"],
                "Location": r["where"], "Comments": r["comment"]} for r in rows]), hide_index=True)

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
