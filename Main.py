    st.subheader("Photos")

    if OCR_LIBS:
        with st.expander("Caption a photo (type text onto the bottom-left)"):
            cf = st.file_uploader("Photo", type=["jpg", "jpeg", "png", "heic", "heif"], key="cap_up")
            cap = st.text_input("Bottom-left caption", key="cap_text")
            cco = st.text_input("Bottom-right line 1 (e.g. 50.934N 113.963W)", key="cap_coord")
            cwh = st.text_input("Bottom-right line 2 (e.g. date)", key="cap_when")
            if cf is not None and st.button("Add caption", key="cap_btn"):
                out = caption_photo(cf.getvalue(), cap.strip(), cco.strip(), cwh.strip())
                st.image(out)
                st.download_button(
                    "Download captioned photo", out,
                    file_name="captioned.jpg", mime="image/jpeg", key="cap_dl",
                )
    st.divider()

    st.caption("Reads the label in each photo, pre-fills a name and date, you fix any misses, then download a zip sorted by date. Free, runs on your machine.")

    if not OCR_LIBS:
        st.info("Add pillow, pillow-heif, and pytesseract to requirements.txt to enable this.")
    elif not _tesseract_ok():
        st.warning(
            "Tesseract isn't installed. On Streamlit Cloud add a packages.txt containing 'tesseract-ocr'. "
            "On Windows install the UB Mannheim build, then restart the app."
        )
    else:
        mode_label = st.radio(
            "Text location",
            ["Bottom banner (Context Camera)", "Whole photo"],
            horizontal=True,
        )
        mode = "banner" if mode_label.startswith("Bottom") else "whole"
        band = 0.08

        up = st.file_uploader(
            "Upload photos or zip files",
            type=["jpg", "jpeg", "png", "heic", "heif", "zip"],
            accept_multiple_files=True,
            key="photo_up",
        )
        if up:
            IMG_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif")
            items = []
            for f in up:
                if f.name.lower().endswith(".zip"):
                    src = f.name[:-4]
                    try:
                        with zipfile.ZipFile(io.BytesIO(f.getvalue())) as z:
                            for info in z.infolist():
                                nm = info.filename
                                base = nm.split("/")[-1]
                                if info.is_dir() or "__MACOSX" in nm or base.startswith("."):
                                    continue
                                if nm.lower().endswith(IMG_EXT):
                                    items.append((base, z.read(info), src))
                    except Exception as e:
                        st.error(f"{f.name}: {e}")
                else:
                    items.append((f.name, f.getvalue(), None))

            if not items:
                st.info("No images found in the upload.")
            else:
                with st.spinner(f"Reading {len(items)} photos..."):
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        results = list(ex.map(lambda it: ocr_photo(it[1], mode, band), items))
                rows = [
                    {"File": items[i][0], "Label": results[i][0], "Date": results[i][1]}
                    for i in range(len(items))
                ]

                if st.checkbox("Auto-fix odd labels using the rest of the batch", value=True) and len(rows) > 1:
                    fixed = clean_labels([r["Label"] for r in rows])
                    for r, fl in zip(rows, fixed):
                        r["Label"] = fl

                wps = st.session_state.get("gpx_waypoints", [])
                if wps and st.checkbox("Name photos from nearest GPX waypoint (uses photo GPS)", value=False):
                    for pos, (nm, data, _s) in enumerate(items):
                        gps = _exif_gps(data)
                        if not gps:
                            continue
                        best, bestd = None, 1e12
                        for wlat, wlon, wname in wps:
                            dd = _dist_m(gps, (wlat, wlon))
                            if dd < bestd:
                                bestd, best = dd, wname
                        if best and bestd <= 100:
                            rows[pos]["Label"] = f"{_safe(best)}_{rows[pos]['Label']}"

                st.caption("Check the label and date, edit anything the OCR got wrong.")
                edited = st.data_editor(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    num_rows="fixed",
                    key="photo_editor",
                    column_config={
                        "File": st.column_config.TextColumn(disabled=True),
                        "Label": st.column_config.TextColumn(),
                        "Date": st.column_config.TextColumn(help="YYYY-MM-DD. Blank goes to a 'nodate' folder."),
                    },
                )

                zip_sources = {s for _, _, s in items if s}
                multi = len(zip_sources) >= 2
                used = set()
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for pos, (_, r) in enumerate(edited.iterrows()):
                        src = items[pos][2]
                        date = str(r["Date"]).strip() or "nodate"
                        label = _safe(r["Label"])
                        if multi and src:
                            srcf = re.sub(r'[\\/:*?"<>|]+', "_", src).strip() or "zip"
                            folder = f"{srcf}/{date}"
                        else:
                            folder = date
                        base = f"{folder}/{label}"
                        name = base + ".jpg"
                        k = 2
                        while name in used:
                            name = f"{base}_{k}.jpg"
                            k += 1
                        used.add(name)
                        z.writestr(name, _jpeg_cached(items[pos][1]))
                st.download_button(
                    "Download zip", buf.getvalue(),
