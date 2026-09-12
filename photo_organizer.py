"""
Photo Organizer tab for the SSIG Field Survey Assistant.

What it does:
  1. You upload field photos (each has text written on a slate/whiteboard/paper in frame).
  2. Claude vision reads that written text.
  3. It proposes a filename: <content>_<date>.<ext>  (date pulled from the photo's EXIF).
  4. You eyeball/fix the content + date, then download a zip with files renamed and
     sorted into one folder per date.

Local use notes:
  - Needs your own key:  export ANTHROPIC_API_KEY=sk-ant-...   (or paste it in the sidebar).
  - Cost: default model is Haiku, a fraction of a cent per photo.
  - This is the ONLY part of the app that calls an API. The reference tabs stay fully local.

requirements.txt additions:
    anthropic
    pillow
    pillow-heif        # only needed if you shoot HEIC (iPhone default)

Drop-in as a tab in Main.py:
    from photo_organizer import render_photo_organizer
    ...
    with tab_photos:
        render_photo_organizer()
"""

import io
import re
import base64
import zipfile
from datetime import datetime, date

import streamlit as st
from PIL import Image
from PIL.ExifTags import TAGS

# HEIC support (iPhone). Silently skipped if not installed.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

try:
    import anthropic
    ANTHROPIC_OK = True
except Exception:
    ANTHROPIC_OK = False


DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # cheap + vision. Change if you prefer.

READ_PROMPT = (
    "This is a field wildlife photo. There is usually text written on a slate, whiteboard, "
    "paper, or held-up card somewhere in the frame (species, site name, date, etc.). "
    "Read that written text and return ONLY a short, plain summary suitable for a filename "
    "(e.g. 'great-horned-owl-site3'). No punctuation except hyphens. "
    "If you cannot find any written text, return exactly: NOTEXT"
)

MEDIA_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp",
    "gif": "image/gif", "heic": "image/jpeg", "heif": "image/jpeg",
}


# ---------- helpers ----------

def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")[:60] or "unlabeled"


def _photo_date(raw: bytes):
    """Return the date the photo was taken (EXIF DateTimeOriginal), or None."""
    try:
        im = Image.open(io.BytesIO(raw))
        exif = im._getexif() or {}
        for tag_id, val in exif.items():
            if TAGS.get(tag_id) in ("DateTimeOriginal", "DateTime"):
                return datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S").date()
    except Exception:
        pass
    return None


def _to_jpeg(raw: bytes) -> bytes:
    """Normalize anything (incl. HEIC) to JPEG bytes for the API + the zip."""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _read_slate(client, model: str, jpeg_bytes: bytes) -> str:
    b64 = base64.standard_b64encode(jpeg_bytes).decode()
    msg = client.messages.create(
        model=model,
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": READ_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return "" if text.upper() == "NOTEXT" else text


# ---------- main tab ----------

def render_photo_organizer():
    st.subheader("Photo Organizer")
    st.caption("Reads the slate text in each shot, renames it content_date, zips it by date.")

    if not ANTHROPIC_OK:
        st.error("`anthropic` isn't installed. Add it to requirements.txt and reinstall.")
        return

    import os
    with st.sidebar:
        st.markdown("**Photo tool**")
        key = st.text_input("Anthropic API key", type="password",
                            value=os.environ.get("ANTHROPIC_API_KEY", ""),
                            help="Or set ANTHROPIC_API_KEY in your environment.")
        model = st.text_input("Model", value=DEFAULT_MODEL)

    files = st.file_uploader(
        "Field photos",
        type=list(MEDIA_TYPES.keys()),
        accept_multiple_files=True,
    )
    if not files:
        st.info("Upload photos to start.")
        return

    if st.button("Read slate text", type="primary"):
        if not key:
            st.error("Add your API key in the sidebar first.")
            return
        client = anthropic.Anthropic(api_key=key)
        results = []
        prog = st.progress(0.0)
        for i, f in enumerate(files, 1):
            raw = f.read()
            try:
                jpeg = _to_jpeg(raw)
            except Exception as e:
                results.append({"orig": f.name, "content": "", "date": date.today(),
                                "jpeg": None, "err": f"unreadable ({e})"})
                prog.progress(i / len(files))
                continue
            try:
                content = _read_slate(client, model, jpeg)
                err = "" if content else "no text found"
            except Exception as e:
                content, err = "", f"read failed ({e})"
            results.append({
                "orig": f.name,
                "content": content,
                "date": _photo_date(raw) or date.today(),
                "jpeg": jpeg,
                "err": err,
            })
            prog.progress(i / len(files))
        st.session_state["photo_results"] = results

    results = st.session_state.get("photo_results")
    if not results:
        return

    st.markdown("**Review** — fix anything, then build the zip.")
    for idx, r in enumerate(results):
        c1, c2, c3 = st.columns([1, 2, 1])
        if r["jpeg"]:
            c1.image(r["jpeg"], use_container_width=True)
        else:
            c1.write("⚠️")
        r["content"] = c2.text_input(
            f"Content #{idx+1}", value=r["content"],
            key=f"content_{idx}",
            placeholder="what's written on the slate",
        )
        r["date"] = c3.date_input(f"Date #{idx+1}", value=r["date"], key=f"date_{idx}")
        if r["err"]:
            c2.caption(f"⚠️ {r['err']}  ·  original: {r['orig']}")
        else:
            c2.caption(f"original: {r['orig']}")

    if st.button("Build zip"):
        buf = io.BytesIO()
        used = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for r in results:
                if not r["jpeg"]:
                    continue
                d = r["date"]
                name = f"{_slug(r['content'])}_{d:%Y-%m-%d}.jpg"
                path = f"{d:%Y-%m-%d}/{name}"
                n = 2
                while path in used:
                    name = f"{_slug(r['content'])}_{d:%Y-%m-%d}-{n}.jpg"
                    path = f"{d:%Y-%m-%d}/{name}"
                    n += 1
                used.add(path)
                z.writestr(path, r["jpeg"])
        buf.seek(0)
        st.download_button(
            "Download organized_photos.zip",
            data=buf,
            file_name="organized_photos.zip",
            mime="application/zip",
        )


# Run standalone for testing:  streamlit run photo_organizer.py
if __name__ == "__main__":
    st.set_page_config(page_title="Photo Organizer", layout="wide")
    render_photo_organizer()
