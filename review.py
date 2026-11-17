"""
Review tab for the AiM Field Assistant.

Final review checklist for reports, plus the APA 7 reference formatting guide.
Tick items as you go; progress shows at the top.
"""

import pandas as pd
import streamlit as st

CHECKLIST = {
    "Project details": [
        ("\"Project\" defined",
         "Define the Project once at first mention, e.g. \"...GWD 31640 pipeline dig (the Project)\". "
         "Use \"the Project\" after that."),
        ("P in \"Project\" capitalized",
         "Every reference to this Project uses a capital P: \"the Project\", \"Project area\", "
         "\"Project footprint\"."),
        ("AiM defined",
         "First mention: \"AiM Land Services Ltd. (AiM)\". Use AiM after that."),
        ("Other compressed Project details defined",
         "Client short names, facility names and other shortened Project details are defined once "
         "at first mention, e.g. \"Keyera Energy Ltd. (Keyera)\"."),
    ],
    "Consistency": [
        ("Consistent mentions of details",
         "Names of things stay the same throughout, e.g. a facility, site, pipeline or location is "
         "written the same way every time."),
        ("References consistent",
         "Every in-text citation is in the reference list, and every reference is cited in the text. "
         "Names and years match."),
    ],
    "Front matter": [
        ("Table of contents updated", "Headings and page numbers match the final report."),
        ("List of figures updated", "Figure numbers, titles and page numbers match."),
        ("List of appendices updated", "Appendix letters and titles match."),
        ("Acronyms list updated", "Every acronym used in the report is in the list, and nothing unused is left in it."),
    ],
    "Acronyms": [
        ("All acronyms defined", "No acronym appears without being written out at some point."),
        ("Acronyms defined once only, at first mention",
         "Write it out with the acronym in brackets the first time, e.g. \"Government of Alberta (GoA)\". "
         "Use only the acronym after that. Don't define it again later."),
        ("Acronyms defined separately in the references",
         "Treat the reference list on its own: define each acronym once at its first mention there too, "
         "e.g. \"Government of Alberta (GoA)\" first, then \"GoA\"."),
    ],
    "Style": [
        ("Proper names capitalized (except plant species)",
         "Common practice in scientific literature. Wildlife common names capitalized, e.g. Black Bear, "
         "Red Squirrel. Plant common names are not, e.g. trembling aspen."),
        ("Species names follow binomial nomenclature",
         "Genus capitalized, species lowercase, both italicized, e.g. *Ursus americanus*."),
        ("Acts italicized",
         "The name of the Act only, e.g. *Water Act*, *Wildlife Act*, *Species at Risk Act*."),
        ("Text justified", "Body text uses justified alignment."),
    ],
}

APA = [
    ("In-text citation: Government / Organization", "(Organization Name, Year)",
     "(COSEWIC, 2023)", "Comma required between name and year."),
    ("In-text citation: Two authors", "(Author Last Name & Author Last Name, Year)",
     "(Smith & Jones, 2022)", "Use ampersand in parentheses."),
    ("In-text citation: Three or more authors", "(Author Last Name et al., Year)",
     "(Smith et al., 2023)", "Use et al. for 3+ authors."),
    ("Report", "Organization Name. (Year). *Title of report* (Report No. if applicable). Publisher. URL",
     "Alberta Energy Regulator. (2022). *Directive 056: Energy development applications and schedules*. "
     "https://www.aer.ca/regulating-development/rules-and-directives/directives/directive-056",
     "Italicize report title. Omit publisher if same as author."),
    ("Website (content that may change)",
     "Organization Name. (Year, Month Day). *Title of webpage*. Website Name. Retrieved Month Day, Year, from URL",
     "Government of Canada. (2024, March 12). *Species at Risk Public Registry*. Retrieved February 19, 2026, "
     "from https://www.canada.ca/en/environment-climate-change/services/species-risk-public-registry.html",
     "Italicize title of webpage. Include retrieval date only if content is designed to change. "
     "Omit website name if same as author."),
    ("Legislation", "*Name of Act*, Jurisdiction citation (Year).",
     "*Wildlife Act*, Revised Statutes of Alberta 2000, c W-10.", "Italicize the name of the Act only."),
    ("Scientific literature (Journal Article)",
     "Author, A. A., & Author, B. B. (Year). Title of article. *Journal Name, Volume*(Issue), pages. "
     "https://doi.org/xxxxx",
     "Smith, J. A., & Brown, L. M. (2021). Habitat selection of grassland birds in Alberta. "
     "*Canadian Field-Naturalist, 135*(2), 145–156. https://doi.org/10.1234/cfn.2021.5678",
     "Italicize journal name and volume number. Use the DOI (not the URL), as URLs may change over time "
     "whereas DOIs remain permanent identifiers."),
]


def _keys():
    return [f"rev_{s}_{i}" for s, items in CHECKLIST.items() for i in range(len(items))]


def render_review():
    st.subheader("Final Review Checklist")
    st.caption("Run through this before a report goes out. Tick each item as you check it.")

    keys = _keys()
    done = sum(bool(st.session_state.get(k)) for k in keys)
    c1, c2 = st.columns([4, 1])
    c1.progress(done / len(keys), text=f"{done} of {len(keys)} checked")
    if c2.button("Reset", key="rev_reset", use_container_width=True):
        for k in keys:
            st.session_state[k] = False
        st.rerun()
    if done == len(keys):
        st.success("All checks done.")

    for section, items in CHECKLIST.items():
        st.markdown(f"**{section}**")
        for i, (item, how) in enumerate(items):
            st.checkbox(item, key=f"rev_{section}_{i}", help=None)
            st.caption(how)

    lines = []
    for section, items in CHECKLIST.items():
        lines.append(section.upper())
        for i, (item, how) in enumerate(items):
            mark = "[x]" if st.session_state.get(f"rev_{section}_{i}") else "[ ]"
            lines.append(f"{mark} {item}: {how.replace('*', '')}")
        lines.append("")
    st.download_button("Download checklist (.txt)", "\n".join(lines), "final_review_checklist.txt",
                       "text/plain", key="rev_dl")

    st.markdown("---")
    st.subheader("APA 7 Reference Formatting Guide")
    for cat, fmt, ex, note in APA:
        with st.expander(cat):
            st.markdown(f"**Format:** {fmt}")
            st.markdown(f"**Example:** {ex}")
            st.caption(note)
