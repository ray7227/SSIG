import pandas as pd
import streamlit as st
from openai import OpenAI

# =========================
# PAGE SETUP
# =========================

st.set_page_config(
    page_title="SSIG Assistant",
    page_icon="🦉"
)

st.title("SSIG Field Survey Assistant")
# =========================
# OPENAI CLIENT
# =========================

client = OpenAI(
    api_key=st.secrets["OPENAI_API_KEY"]
)
# =========================
# LOAD SPREADSHEET
# =========================

df = pd.read_excel("SSIG_Breakdown.xlsx", header=None)

sheet_text = df.to_string(index=False, header=False)

# =========================
# AI FUNCTION
# =========================

def ask_ssig(question):

    prompt = f"""
You are a professional environmental field survey assistant.

Use ONLY the spreadsheet information below.
Never invent information.
Never guess.

If information is missing, say:
- Not specified in spreadsheet

Return answers in this exact format:

SURVEY:
[Survey/species name]

WHEN TO SURVEY:
- item

WHAT YOU NEED:
- item

FIELD METHOD:
- item

NOTES / LIMITATIONS:
- item

Rules:
- Keep answers short
- Keep answers field practical
- No markdown tables
- No long paragraphs
- No extra commentary

Spreadsheet:
{sheet_text}

Question:
{question}
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an environmental field survey assistant. "
                    "Answer ONLY using spreadsheet information."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return response.choices[0].message.content

# =========================
# WEB APP UI
# =========================

question = st.text_input(
    "Ask a question",
    placeholder="What do I need for Burrowing Owl surveys?"
)

if st.button("Ask"):

    if question.strip():

        with st.spinner("Checking spreadsheet..."):
            answer = ask_ssig(question)

        st.subheader("Answer")
        st.write(answer)

    else:
        st.warning("Enter a question first.")
