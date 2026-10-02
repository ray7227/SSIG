import io
import json
import math
import os
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, time as dtime
from difflib import get_close_matches
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

# Free local OCR for the Photos tab (needs Tesseract installed on the machine).
try:
    import pytesseract
    from PIL import Image, ExifTags, ImageDraw, ImageFont
    from dateutil import parser as dateparser
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    OCR_LIBS = True
except Exception:
    OCR_LIBS = False

# Free offline dictionary for merging split words (optional).
try:
    from spellchecker import SpellChecker
    _SPELL = SpellChecker()
except Exception:
    _SPELL = None

# Optional GIS libs for the custom location picker.
try:
    import geopandas as gpd
    GEO_OK = True
except Exception:
    GEO_OK = False

try:
    import gpxpy
    GPX_OK = True
except Exception:
    GPX_OK = False

# =========================
# PAGE SETUP
# =========================
st.set_page_config(
    page_title="AiM Field Assistant",
    page_icon="🧭",
    layout="wide",
)
st.title("AiM Field Assistant")

SSIG_PDF = "https://open.alberta.ca/dataset/93d8a251-4a9a-428f-ad99-7484c6ebabe0/resource/f4024e81-b835-4a50-8fb1-5b31d9726b84/download/2013-sensitivespeciesinventoryguidelines-apr18.pdf"
SWEEP_PDF = "https://open.alberta.ca/dataset/d15221f2-f6d8-4671-8b49-d8fff6eab2b6/resource/6968392a-9e05-4bd8-bd76-ea107ba86c1c/download/aep-wildlife-sweep-protocols-sensitive-species-inventory-guidelines-2020.pdf"
FWMIS_CHECK = "https://www.alberta.ca/fwmis-loadform-check-tool"
FWMIS_GUIDE = "https://www.alberta.ca/system/files/custom_downloaded_images/ep-fwmis-data-submission-guide.pdf"

with st.sidebar:
    st.markdown("### Sources")
    st.markdown(f"[Sensitive Species Inventory Guidelines (2013)]({SSIG_PDF})")
    st.markdown(f"[Wildlife Sweep Protocols (2020)]({SWEEP_PDF})")

FILE = "SSIG_Breakdown.xlsx"
SWEEP_FILE = "Sweep_Breakdown.xlsx"
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
    "Custom / map": None,
}
