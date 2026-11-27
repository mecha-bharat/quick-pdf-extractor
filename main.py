"""
PDF Extraction API for Legal Acts — Quick Clause Format

Features:
1. Act Link (URL) + Act Name input with automatic Python downloader (no API required)
2. Storage in extractor_json/<Act_Name>/ with PDF, JSON, and metadata
3. PDF Preview & Bherifai (Split-screen verification of PDF vs JSON)
4. History management with search, selective delete, and clear-all
5. Mandatory BYOK (Bring Your Own Key) stored in client browser
6. Footnote reference number stripping & section heading cleaning
7. Quoted amendment block preservation ("...") & Proviso internal clause preservation
8. Ordinary clause unbundling (a, b, c) into individual JSON objects
"""

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pypdf import PdfReader, PdfWriter

from prompt import get_extraction_prompt

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Legal Act PDF Extractor",
    description="Upload or Link a legal Act PDF and get structured JSON for Quick Clause",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base storage directories
BASE_DIR = Path(__file__).parent
EXTRACTOR_DIR = BASE_DIR / "extractor_json"
EXTRACTOR_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Gemini client & retry config
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

FALLBACK_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
]

MAX_RETRIES_PER_MODEL = 2
INITIAL_BACKOFF_SECONDS = 1.5


def get_gemini_client(user_api_key: str = None):
    key = (user_api_key or "").strip() or GEMINI_API_KEY
    if not key:
        raise HTTPException(
            status_code=400,
            detail=(
                "Gemini API Key is required! Please enter your own API key in the web interface. "
                "You can get a free key in 30 seconds from https://aistudio.google.com/apikey"
            ),
        )
    return genai.Client(api_key=key)


def call_gemini_with_retry(client, model: str, uploaded_file, prompt: str):
    """
    Call Gemini with retry + exponential backoff on 503/429 errors.
    Falls back quickly to alternate working models if the primary is experiencing high demand.
    """
    models_to_try = [model] + [m for m in FALLBACK_MODELS if m != model]
    last_error = None

    for current_model in models_to_try:
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                logger.info(
                    f"Attempt {attempt}/{MAX_RETRIES_PER_MODEL} with model '{current_model}'"
                )

                response = client.models.generate_content(
                    model=current_model,
                    contents=[uploaded_file, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.0,
                        max_output_tokens=65536,
                    ),
                )

                raw_text = response.text
                if raw_text:
                    logger.info(
                        f"✅ Success with model '{current_model}' on attempt {attempt}"
                    )
                    return raw_text, current_model

                logger.warning(
                    f"Empty response from '{current_model}', attempt {attempt}"
                )

            except Exception as e:
                error_str = str(e)
                last_error = e
                is_retryable = any(
                    code in error_str
                    for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "high demand"]
                )

                if is_retryable and attempt < MAX_RETRIES_PER_MODEL:
                    wait_time = INITIAL_BACKOFF_SECONDS * attempt
                    logger.warning(
                        f"⏳ Model '{current_model}' busy (503/429). Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning(
                        f"⚠️ Switching from '{current_model}' to next fallback model due to: {error_str[:120]}"
                    )
                    break

    raise HTTPException(
        status_code=503,
        detail=(
            f"All Gemini models are currently unavailable after multiple retries. "
            f"Last error: {str(last_error)[:300]}. "
            f"Please wait a few minutes and try again."
        ),
    )


# ---------------------------------------------------------------------------
# String Sanitization & Footnote Cleaning Helpers
# ---------------------------------------------------------------------------


def sanitize_name(name: str) -> str:
    """Create a clean folder & file name from Act name."""
    s = re.sub(r"[^\w\s-]", "", name).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:80] or "Act_Document"


def roman_to_int(s: str) -> int:
    """Convert Roman numeral string to integer."""
    roman_map = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    s = s.upper().strip()
    total = 0
    prev = 0
    for char in reversed(s):
        val = roman_map.get(char, 0)
        if val == 0:
            return 0
        if val >= prev:
            total += val
            prev = val
        else:
            total -= val
    return total


def convert_roman_identifier(ident: str) -> str:
    """Convert Roman section numerals (I, II, IV, IV-A, XXX) to Arabic numbers (1, 2, 4, 4A, 30)."""
    if not ident:
        return ""
    s = str(ident).strip()
    s = re.sub(r"\.+$", "", s).strip()

    # Check if section number with optional letter suffix: e.g. IV, IV-A, IVA, XXX
    m = re.match(r"^([IVXLCDMivxlcdm]+)(?:[\s\-_]*([A-Za-z]))?$", s)
    if m:
        r_part = m.group(1)
        suffix = m.group(2) or ""
        val = roman_to_int(r_part)
        if val > 0:
            return f"{val}{suffix.upper()}"

    # Check if subsection identifier starts with Roman numeral: e.g. IV(1), IV(2)(a)
    m2 = re.match(r"^([IVXLCDMivxlcdm]+)(\(.*\))$", s)
    if m2:
        val = roman_to_int(m2.group(1))
        if val > 0:
            return f"{val}{m2.group(2)}"

    return s


def clean_identifier(text: str) -> str:
    """Clean stray footnote reference numbers, brackets, and convert Roman numerals to Arabic numbers."""
    if not text:
        return ""
    s = str(text).strip()
    s = re.sub(r"^[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", s)
    s = re.sub(r"^\[\s*", "", s)
    s = re.sub(r"\s*\]$", "", s)
    s = re.sub(r"^Section\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.+$", "", s).strip()
    # Normalize 14-A -> 14A
    s = re.sub(r"^(\d+)-([A-Za-z])$", r"\1\2", s)
    return convert_roman_identifier(s)


def clean_section_heading(heading: str) -> str:
    """Clean footnote reference numbers and trailing full stops from section headings."""
    if not heading:
        return ""
    s = str(heading).strip()
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", s)
    s = re.sub(r"[\.\s]+$", "", s)
    s = re.sub(r"([a-zA-Z]+)\d+$", r"\1", s)
    s = re.sub(r"^\[\s*", "", s)
    s = re.sub(r"\s*\]$", "", s)
    return s.strip()


def clean_editorial_references(desc: str) -> str:
    """
    Remove small editorial footnote reference numbers and superscript digits
    without corrupting genuine statutory numbers (dates, amounts, section references).
    """
    if not desc:
        return ""
    s = str(desc)
    # Strip superscript digits: ¹, ², ³, etc.
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", s)
    # Strip footnote reference digits immediately preceding or following brackets: e.g. 1[ -> [, ]2 -> ]
    s = re.sub(r"(?<=\s)\d+\[", "[", s)
    s = re.sub(r"^\d+\[", "[", s)
    s = re.sub(r"\]\d+(?=\s|[,\.;:\)])", "]", s)
    s = re.sub(r"\]\d+$", "]", s)
    # Strip footnote digits directly attached to word letters before punctuation:
    # e.g. 'settlement,3' -> 'settlement,', 'Government4,' -> 'Government,'
    s = re.sub(r"([a-zA-Z]+)\d+([,\.;:—\)])", r"\1\2", s)
    s = re.sub(r"([,\.;:—])\d+\b", r"\1", s)
    # Strip single footnote digit attached directly to an English word before space (e.g. 'Government1 was' -> 'Government was')
    s = re.sub(r"([a-zA-Z]{3,})\d(\s+)", r"\1\2", s)
    # Strip stray bottom-of-page footnote lines if any leaked
    lines = [
        line
        for line in s.split("\n")
        if not re.match(
            r"^\s*\d+\s*[\.\)]\s*(?:Subs|Added|Omitted|Repealed|Ins|See|Words)\b",
            line,
            re.IGNORECASE,
        )
    ]
    return "\n".join(lines).strip()


def clean_leading_identifiers(desc: str, section_no: str = "", sub_section_no: str = "", section_name: str = ""):
    """
    Remove redundant leading section numbers, marginal headings, subsection numbers,
    or clause letters from description while preserving brackets and statutory text.
    """
    if not desc:
        return "", sub_section_no

    s = str(desc).strip()

    # Never touch STATE AMENDMENTS or Quoted amendment blocks starting with quotes
    if (
        "state amendment" in section_name.lower()
        or "state amendment" in s[:200].lower()
        or s.startswith('"')
        or s.startswith('“')
    ):
        return s, sub_section_no

    sec_no = section_no.strip()
    sec_name = section_name.strip()
    sub_no = sub_section_no.strip()

    # 1. Strip leading Section Number + Section Name (e.g. "30. Procedure applicable...—" or "30. Procedure applicable...")
    if sec_no and sec_name:
        esc_sec = re.escape(sec_no)
        esc_name = re.escape(sec_name)
        pattern = rf"^(\[\s*)?(?:Section\s+)?{esc_sec}\s*[\.\—\-\:\)]*\s*{esc_name}\s*[\.\—\-\:\)]*\s*"
        m = re.match(pattern, s, re.IGNORECASE)
        if m:
            prefix = "[" if m.group(1) else ""
            s = prefix + s[m.end():].strip()

    # 2. Strip leading Section Name alone if repeated at start (e.g. "Procedure applicable...—The procedure...")
    if sec_name and len(sec_name) > 3:
        esc_name = re.escape(sec_name)
        pattern = rf"^(\[\s*)?{esc_name}\s*[\.\—\-\:\)]+\s*"
        m = re.match(pattern, s, re.IGNORECASE)
        if m:
            prefix = "[" if m.group(1) else ""
            s = prefix + s[m.end():].strip()

    # 3. Strip leading Section Number alone (e.g. "30. The procedure...", "30.—The procedure...", "[30. The procedure...")
    if sec_no:
        esc_sec = re.escape(sec_no)
        pattern = rf"^(\[\s*)?(?:Section\s+)?{esc_sec}\s*[\.\—\-\:\)]+\s*"
        m = re.match(pattern, s, re.IGNORECASE)
        if m:
            prefix = "[" if m.group(1) else ""
            s = prefix + s[m.end():].strip()

    # 4. Strip leading Subsection / Clause identifier (e.g. "(1) ", "[(1) ", "(2) ", "(a) ", "(i) ")
    if sub_no:
        m_id = re.search(r"\(([a-zA-Z0-9]+)\)$", sub_no)
        if m_id:
            last_id = m_id.group(1)
            esc_id = re.escape(last_id)
            pat_sub = rf"^(\[\s*)?\({esc_id}\)\s*[\.\—\-\:\)]*\s*"
            m_sub = re.match(pat_sub, s, re.IGNORECASE)
            if m_sub:
                prefix = "[" if m_sub.group(1) else ""
                s = prefix + s[m_sub.end():].strip()
            else:
                pat_sub_num = rf"^(\[\s*)?{esc_id}\s*[\.\—\-\:]+\s*"
                m_num = re.match(pat_sub_num, s)
                if m_num:
                    prefix = "[" if m_num.group(1) else ""
                    s = prefix + s[m_num.end():].strip()
        elif re.match(r"^[0-9a-zA-Z]+$", sub_no):
            esc_id = re.escape(sub_no)
            pat_sub = rf"^(\[\s*)?\({esc_id}\)\s*[\.\—\-\:\)]*\s*"
            m_sub = re.match(pat_sub, s, re.IGNORECASE)
            if m_sub:
                prefix = "[" if m_sub.group(1) else ""
                s = prefix + s[m_sub.end():].strip()
    else:
        # If sub_section_no was empty, but description starts with "(1) " or "[(1) "
        m_first = re.match(r"^(\[\s*)?\(([0-9a-zA-Z]+)\)\s*[\.\—\-\:\)]*\s*(.*)", s, re.DOTALL)
        if m_first:
            cid = m_first.group(2)
            prefix = "[" if m_first.group(1) else ""
            s = prefix + m_first.group(3).strip()
            if sec_no:
                sub_no = f"{sec_no}({cid})"
            else:
                sub_no = f"({cid})"

    return s.strip(), sub_no


def normalize_provision_object(obj: dict) -> list:
    """
    1. Converts Roman section numerals to Arabic integers (IV -> 4, XXX -> 30).
    2. Strips editorial footnote reference numbers and cleans section names/identifiers.
    3. Strips redundant leading section or subsection numbers from description.
    4. Mandatory State Amendment preservation: keeps entire State Amendment block merged in one object.
    5. Preserves quoted amendment blocks in double quotes (namely:—\n"(a)...").
    6. Preserves internal clauses of Provisos and Explanations ((a),(b) or (1),(2) or (i),(ii)).
    7. Keeps Provisos merged with their respective section / subsection description.
    8. Unbundles ordinary child clauses (a), (b), (c)... into separate JSON objects.
    """
    desc = obj.get("description", "")
    sub_no = clean_identifier(obj.get("sub_section_no", ""))
    sec_no = clean_identifier(obj.get("section_no", ""))
    sec_name = clean_section_heading(obj.get("section_name", ""))

    cleaned_desc = clean_editorial_references(desc)
    cleaned_desc, sub_no = clean_leading_identifiers(cleaned_desc, sec_no, sub_no, sec_name)

    obj["section_no"] = sec_no
    obj["section_name"] = sec_name
    obj["sub_section_no"] = sub_no
    obj["description"] = cleaned_desc

    # CASE S: If the object is a STATE AMENDMENT, DO NOT SPLIT! Keep entirely merged!
    if (
        "state amendment" in obj.get("section_name", "").lower()
        or "state amendment" in obj.get("description", "")[:250].lower()
        or re.search(r"STATE\s+AMENDMENT", obj.get("description", "")[:250], re.IGNORECASE)
    ):
        return [obj]

    # CASE A: If the description starts with a proviso or explanation, DO NOT SPLIT internal clauses!
    # (Whether internal clauses are (a), (b), (c) or (1), (2), (3) or (i), (ii), (iii)!)
    if re.match(r"^\s*(?:\[\s*)?(?:Provided|Explanation)\b", obj["description"], re.IGNORECASE):
        return [obj]

    # CASE B: If the description contains a quoted amendment block (e.g. namely:—\n"(a)..."), DO NOT SPLIT!
    # Quoted clauses in double quotes belong to the amended Act and must stay merged in one description.
    if (
        re.search(r"namely\s*[:—\-]*\s*[\"“\']", obj["description"], re.IGNORECASE)
        or re.search(r"[\"“\']\s*\([a-zA-Z0-9]+\)", obj["description"])
        or (('"' in obj["description"] or '“' in obj["description"]) and re.search(r'[\"“][^\"”]*\([a-zA-Z0-9]+\)', obj["description"], re.DOTALL))
    ):
        return [obj]

    # Check for trailing Proviso or Explanation
    prov_match = re.search(r"\n(?=(?:Provided|Explanation)\b)", obj["description"], re.IGNORECASE)
    if prov_match:
        main_part = obj["description"][:prov_match.start()].strip()
        prov_part = obj["description"][prov_match.start()+1:].strip()
    else:
        main_part = obj["description"].strip()
        prov_part = None

    lines = main_part.split("\n")
    clause_regex = re.compile(r"^\(([a-zA-Z0-9]+)\)\s+(.*)", re.DOTALL)

    has_clauses = False
    intro_lines = []
    clauses = []

    current_clause_id = None
    current_clause_text = []

    for line in lines:
        stripped = line.strip()
        m = clause_regex.match(stripped)
        if m:
            has_clauses = True
            if current_clause_id is not None:
                clauses.append((current_clause_id, "\n".join(current_clause_text)))
            current_clause_id = m.group(1)
            current_clause_text = [m.group(2)]
        else:
            if current_clause_id is not None:
                current_clause_text.append(line)
            else:
                intro_lines.append(line)

    if current_clause_id is not None:
        clauses.append((current_clause_id, "\n".join(current_clause_text)))

    # If the provision has no ordinary child clauses (a), (b), (c)...,
    # KEEP the Proviso merged with its respective section / sub-section description!
    if not has_clauses or len(clauses) < 2:
        return [obj]

    # Unbundle ordinary clauses into separate JSON objects!
    results = []
    intro_str = "\n".join(intro_lines).strip()
    if intro_str:
        o_intro = dict(obj)
        o_intro["sub_section_no"] = sub_no
        o_intro["description"] = intro_str
        results.append(o_intro)

    for cid, ctext in clauses:
        o_clause = dict(obj)
        if sub_no:
            o_clause["sub_section_no"] = f"{sub_no}({cid})"
        else:
            o_clause["sub_section_no"] = f"{sec_no}({cid})"
        o_clause["description"] = ctext.strip()
        results.append(o_clause)

    # Any trailing Proviso belongs to the parent subsection/section in statutory hierarchy
    if prov_part:
        o_prov = dict(obj)
        o_prov["sub_section_no"] = sub_no
        o_prov["description"] = prov_part
        results.append(o_prov)

    return results


def is_preamble_or_recital(obj: dict) -> bool:
    """
    Return True if the object represents a Preamble, Long Title, Enacting Formula,
    or unnumbered introductory recital that should be excluded from statutory sections.
    """
    if not isinstance(obj, dict):
        return False

    sec_name = str(obj.get("section_name", "")).strip()
    sec_name_lower = sec_name.lower()
    sec_no = str(obj.get("section_no", "")).strip().lower()
    sub_no = str(obj.get("sub_section_no", "")).strip()
    desc = str(obj.get("description", "")).strip()
    desc_lower = desc.lower()

    # 1. Direct label check in section_name or section_no
    if sec_name_lower in ["preamble", "preamble.", "preamble :", "preamble:", "long title", "enacting formula", "enacting formula:"]:
        return True
    if re.search(r"^\s*preamble\b", sec_name_lower):
        return True
    if sec_no in ["preamble", "long title"]:
        return True

    # 2. Check if description is an unnumbered preamble recital or enacting formula
    is_recital_start = bool(
        re.match(
            r"^\s*(?:WHEREAS\b|Now,\s*THEREFORE\b|An Act to\b|BE it enacted by\b)",
            desc,
            re.IGNORECASE,
        )
    )

    if is_recital_start:
        # If section name is preamble-like, empty, or generic
        if sec_name_lower in ["", "preamble", "long title", "enacting formula", "ordinance"] or "preamble" in sec_name_lower:
            return True
        # If section_no is "1" or "" and description has recital formula without statutory subsections
        if sec_no in ["", "1"] and not sub_no:
            # Check if this is preamble text rather than Section 1 statutory text
            if (
                "whereas parliament" in desc_lower
                or "whereas it is expedient" in desc_lower
                or "president is pleased to promulgate" in desc_lower
                or "governor is pleased to promulgate" in desc_lower
                or "be it enacted by parliament" in desc_lower
                or "powers conferred by clause (1) of article 123" in desc_lower
            ):
                return True

    # 3. Check for historical regulation preambles (e.g. Bengal Patni Taluks 1819)
    if "preamble" in sec_name_lower:
        return True
    if sec_no in ["", "1"] and not sub_no:
        if (
            "the following rules have accordingly been enacted" in desc_lower
            or "the following regulation has been enacted" in desc_lower
            or "the following provisions have accordingly been enacted" in desc_lower
        ) and "short title" not in sec_name_lower and "definition" not in sec_name_lower:
            return True

    return False


def post_process_extracted_data(data: list) -> list:
    """Apply safety normalization, clause unbundling, identifier cleanup, and preamble exclusion across all objects."""
    if not isinstance(data, list):
        return data

    processed = []
    for item in data:
        if isinstance(item, dict):
            # Filter out preambles and introductory recitals
            if is_preamble_or_recital(item):
                continue
            unbundled = normalize_provision_object(item)
            for u in unbundled:
                if not is_preamble_or_recital(u):
                    processed.append(u)
        else:
            processed.append(item)

    cleaned_list = []
    required_keys = ["chapter_no", "chapter_name", "section_no", "section_name", "sub_section_no", "description"]
    for obj in processed:
        if isinstance(obj, dict):
            if is_preamble_or_recital(obj):
                continue
            clean_obj = {}
            for k in required_keys:
                val = obj.get(k, "")
                clean_obj[k] = str(val) if val is not None else ""
            clean_obj["section_no"] = clean_identifier(clean_obj["section_no"])
            clean_obj["section_name"] = clean_section_heading(clean_obj["section_name"])
            clean_obj["sub_section_no"] = clean_identifier(clean_obj["sub_section_no"])
            clean_obj["description"] = clean_editorial_references(clean_obj["description"])
            clean_desc, clean_sub = clean_leading_identifiers(
                clean_obj["description"],
                clean_obj["section_no"],
                clean_obj["sub_section_no"],
                clean_obj["section_name"],
            )
            clean_obj["description"] = clean_desc
            clean_obj["sub_section_no"] = clean_sub
            if not is_preamble_or_recital(clean_obj):
                cleaned_list.append(clean_obj)
        else:
            cleaned_list.append(obj)

    return cleaned_list


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main UI page."""
    html_path = BASE_DIR / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/download-act")
async def download_act(
    act_url: str = Form(""),
    act_name: str = Form(""),
):
    """
    Download an Act PDF from URL directly via Python (no API needed).
    Stores it in extractor_json/<Act_Name>/<Act_Name>.pdf
    """
    url = act_url.strip()
    if not url:
        raise HTTPException(
            status_code=400,
            detail="abbe pdf link de na , gandmasti mat kar",
        )

    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=400,
            detail="Valid http:// ya https:// wala PDF link chahiye!",
        )

    # Determine Act name
    raw_name = act_name.strip()
    if not raw_name:
        parsed = urllib.parse.urlparse(url)
        base = os.path.basename(parsed.path)
        raw_name = os.path.splitext(base)[0] or "Act_Document"

    safe_name = sanitize_name(raw_name)
    act_folder = EXTRACTOR_DIR / safe_name
    act_folder.mkdir(parents=True, exist_ok=True)
    pdf_path = act_folder / f"{safe_name}.pdf"

    # Download PDF via httpx directly
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=90.0, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"PDF download fail ho gaya! Server status code: {resp.status_code}.",
                )
            content = resp.content
            if len(content) < 100:
                raise HTTPException(
                    status_code=400,
                    detail="Downloaded file bohot chhoti ya corrupt hai.",
                )

            with open(pdf_path, "wb") as f:
                f.write(content)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"PDF download error: {str(e)}",
        )

    # Inspect page count
    try:
        reader = PdfReader(pdf_path)
        total_pages = len(reader.pages)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Downloaded file valid PDF nahi hai: {str(e)}",
        )

    # Create/update meta.json
    meta = {
        "act_name": raw_name,
        "folder_name": safe_name,
        "source_url": url,
        "total_pages": total_pages,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "downloaded",
        "has_json": False,
    }
    with open(act_folder / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return JSONResponse(
        content={
            "success": True,
            "act_name": raw_name,
            "folder_name": safe_name,
            "total_pages": total_pages,
            "pdf_url": f"/api/pdf/{safe_name}",
            "exceeds_threshold": total_pages > 15,
        }
    )


@app.post("/api/inspect")
async def inspect_uploaded_pdf(file: UploadFile = File(...)):
    """Inspect manually uploaded PDF metadata."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Invalid file format. Only PDF files are allowed.",
        )

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        reader = PdfReader(io.BytesIO(contents))
        total_pages = len(reader.pages)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read PDF structure: {str(e)}",
        )

    PAGE_THRESHOLD = 15
    return JSONResponse(
        content={
            "filename": file.filename,
            "total_pages": total_pages,
            "threshold": PAGE_THRESHOLD,
            "exceeds_threshold": total_pages > PAGE_THRESHOLD,
        }
    )


@app.post("/api/extract")
async def extract_pdf(
    file: UploadFile = File(None),
    folder_name: str = Form(default=""),
    act_name: str = Form(default=""),
    api_key: str = Form(default=""),
    mode: str = Form(default="full"),  # 'full', 'page_range', or 'section_range'
    start_page: int = Form(default=1),
    end_page: int = Form(default=0),
    section_range: str = Form(default=""),
    model: str = Form(default="gemini-3.5-flash"),
):
    """
    Extract statutory content from either:
    1. A previously downloaded Act in extractor_json/<folder_name>/
    2. A newly uploaded file
    """
    tmp_path = None
    sliced_path = None

    try:
        # Determine PDF source
        pdf_source_path = None
        current_act_name = act_name.strip()
        safe_folder = folder_name.strip()

        if safe_folder:
            act_folder = EXTRACTOR_DIR / safe_folder
            pdf_candidate = act_folder / f"{safe_folder}.pdf"
            if pdf_candidate.exists():
                pdf_source_path = pdf_candidate
                if not current_act_name:
                    meta_path = act_folder / "meta.json"
                    if meta_path.exists():
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                current_act_name = json.load(f).get("act_name", safe_folder)
                        except Exception:
                            pass
            else:
                # Find any pdf in that folder
                pdf_list = list(act_folder.glob("*.pdf"))
                if pdf_list:
                    pdf_source_path = pdf_list[0]

        if not pdf_source_path and file:
            # Handle uploaded file
            contents = await file.read()
            if len(contents) == 0:
                raise HTTPException(status_code=400, detail="Uploaded file is empty.")

            raw_name = current_act_name or Path(file.filename).stem
            safe_folder = sanitize_name(raw_name)
            act_folder = EXTRACTOR_DIR / safe_folder
            act_folder.mkdir(parents=True, exist_ok=True)
            pdf_source_path = act_folder / f"{safe_folder}.pdf"
            with open(pdf_source_path, "wb") as f:
                f.write(contents)
            current_act_name = raw_name

        if not pdf_source_path or not os.path.exists(pdf_source_path):
            raise HTTPException(
                status_code=400,
                detail="PDF source nahi mila. Pehle PDF link do ya file upload karo!",
            )

        # Read PDF to get actual page count
        reader = PdfReader(pdf_source_path)
        total_pages = len(reader.pages)

        # Handle page range slicing if requested
        file_to_upload = None
        page_info = None

        if mode == "page_range" and start_page >= 1:
            actual_end = min(end_page if end_page > 0 else total_pages, total_pages)
            actual_start = max(1, min(start_page, actual_end))

            logger.info(
                f"Slicing PDF '{safe_folder}' from page {actual_start} to {actual_end} (total: {total_pages})"
            )

            writer = PdfWriter()
            for p_num in range(actual_start - 1, actual_end):
                writer.add_page(reader.pages[p_num])

            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".pdf", prefix=f"act_p{actual_start}_p{actual_end}_"
            ) as tmp_slice:
                writer.write(tmp_slice)
                sliced_path = tmp_slice.name

            file_to_upload = sliced_path
            page_info = f"{actual_start} to {actual_end} (out of {total_pages} total pages)"
        else:
            file_to_upload = str(pdf_source_path)

        # Build extraction prompt
        range_text = section_range.strip() if section_range else None
        prompt = get_extraction_prompt(section_range=range_text, page_info=page_info)

        # Initialize Gemini client with user's key (BYOK)
        client = get_gemini_client(user_api_key=api_key)

        upload_size = os.path.getsize(file_to_upload)
        logger.info(f"Uploading PDF ({upload_size} bytes) to Gemini Files API...")
        uploaded_file = client.files.upload(file=file_to_upload)
        logger.info(f"PDF uploaded as: {uploaded_file.name}")

        try:
            # Call Gemini with retry + fallback
            raw_json_text, used_model = call_gemini_with_retry(
                client, model, uploaded_file, prompt
            )

            # Clean up potential markdown fences
            cleaned = raw_json_text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines)

            # Validate JSON
            try:
                extracted_data = json.loads(cleaned)
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Gemini returned invalid JSON: {str(e)}. Raw response length: {len(raw_json_text)} chars.",
                )

            # Apply bulletproof clause unbundling, identifier cleaning & normalization
            extracted_data = post_process_extracted_data(extracted_data)

            # Validate structure — must be a list
            if not isinstance(extracted_data, list):
                raise HTTPException(
                    status_code=500,
                    detail="Gemini returned JSON but it is not an array. Expected a JSON array of objects.",
                )

            # Validate each object has the required 6 keys
            required_keys = {
                "chapter_no",
                "chapter_name",
                "section_no",
                "section_name",
                "sub_section_no",
                "description",
            }
            for i, obj in enumerate(extracted_data):
                if not isinstance(obj, dict):
                    raise HTTPException(
                        status_code=500,
                        detail=f"Item {i} in the JSON array is not an object.",
                    )
                missing = required_keys - set(obj.keys())
                if missing:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Item {i} is missing required keys: {missing}",
                    )

            # Save in extractor_json/<folder_name>/<folder_name>.json
            target_folder = EXTRACTOR_DIR / safe_folder
            target_folder.mkdir(parents=True, exist_ok=True)
            output_json_path = target_folder / f"{safe_folder}.json"

            with open(output_json_path, "w", encoding="utf-8") as f:
                json.dump(extracted_data, f, ensure_ascii=False, indent=2)

            # Build section summary
            sections = sorted(
                set(obj.get("section_no", "") for obj in extracted_data if obj.get("section_no"))
            )

            # Update meta.json
            meta_path = target_folder / "meta.json"
            meta = {}
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass

            meta.update({
                "act_name": current_act_name or safe_folder,
                "folder_name": safe_folder,
                "total_pages": total_pages,
                "total_sections": len(sections),
                "total_provisions": len(extracted_data),
                "sections_found": sections,
                "model_used": used_model,
                "pages_processed": page_info if page_info else f"1 to {total_pages}",
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "has_json": True,
            })
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            return JSONResponse(
                content={
                    "success": True,
                    "act_name": current_act_name or safe_folder,
                    "folder_name": safe_folder,
                    "filename": f"{safe_folder}.json",
                    "model_used": used_model,
                    "total_pages": total_pages,
                    "pages_processed": page_info if page_info else f"1 to {total_pages}",
                    "total_objects": len(extracted_data),
                    "sections_found": sections,
                    "total_sections": len(sections),
                    "download_url": f"/api/download/{safe_folder}",
                    "pdf_url": f"/api/pdf/{safe_folder}",
                    "data": extracted_data,
                }
            )

        finally:
            # Clean up remote file from Gemini servers
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing PDF: {str(e)}",
        )
    finally:
        # Clean up temporary sliced files
        for p in [tmp_path, sliced_path]:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass


@app.get("/api/pdf/{folder_name}")
async def serve_pdf(folder_name: str):
    """Serve PDF file inline for preview and Bherifai split-screen."""
    target_folder = EXTRACTOR_DIR / folder_name
    pdf_path = target_folder / f"{folder_name}.pdf"
    if not pdf_path.exists():
        # Fallback to any pdf in that folder
        candidates = list(target_folder.glob("*.pdf"))
        if candidates:
            pdf_path = candidates[0]
        else:
            raise HTTPException(status_code=404, detail="PDF file not found.")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@app.get("/api/download/{folder_name}")
async def download_json(folder_name: str):
    """Download generated JSON file."""
    target_folder = EXTRACTOR_DIR / folder_name
    json_path = target_folder / f"{folder_name}.json"
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="JSON file not found.")

    return FileResponse(
        path=str(json_path),
        filename=f"{folder_name}.json",
        media_type="application/json",
    )


@app.get("/api/history")
async def get_history():
    """List all extracted and downloaded Acts in extractor_json/."""
    items = []
    if not EXTRACTOR_DIR.exists():
        return JSONResponse(content={"items": []})

    for folder in sorted(EXTRACTOR_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not folder.is_dir():
            continue

        meta_file = folder / "meta.json"
        meta = {}
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                pass

        json_file = folder / f"{folder.name}.json"
        pdf_file = folder / f"{folder.name}.pdf"
        has_json = json_file.exists() or any(folder.glob("*.json"))
        has_pdf = pdf_file.exists() or any(folder.glob("*.pdf"))

        items.append({
            "folder_name": folder.name,
            "act_name": meta.get("act_name", folder.name.replace("_", " ")),
            "created_at": meta.get("completed_at") or meta.get("created_at") or time.strftime("%Y-%m-%d %H:%M"),
            "total_pages": meta.get("total_pages", 0),
            "total_sections": meta.get("total_sections", 0),
            "total_provisions": meta.get("total_provisions", 0),
            "has_json": has_json,
            "has_pdf": has_pdf,
            "download_url": f"/api/download/{folder.name}" if has_json else None,
            "pdf_url": f"/api/pdf/{folder.name}" if has_pdf else None,
        })

    return JSONResponse(content={"items": items})


@app.get("/api/history/{folder_name}")
async def get_single_history(folder_name: str):
    """Get full data of a single Act from history."""
    folder = EXTRACTOR_DIR / folder_name
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Act folder not found.")

    meta_file = folder / "meta.json"
    meta = {}
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass

    json_path = folder / f"{folder_name}.json"
    data = []
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    return JSONResponse(
        content={
            "meta": meta,
            "folder_name": folder_name,
            "act_name": meta.get("act_name", folder_name),
            "pdf_url": f"/api/pdf/{folder_name}",
            "download_url": f"/api/download/{folder_name}",
            "data": data,
        }
    )


@app.delete("/api/history/{folder_name}")
async def delete_history_item(folder_name: str):
    """Delete a single Act folder from extractor_json/."""
    folder = EXTRACTOR_DIR / folder_name
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Folder not found.")

    try:
        shutil.rmtree(folder)
        return JSONResponse(content={"success": True, "deleted": folder_name})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete error: {str(e)}")


@app.post("/api/history/delete-selected")
async def delete_selected_history(payload: dict):
    """Delete multiple selected folders from extractor_json/."""
    folders = payload.get("folder_names", [])
    deleted = []
    for f_name in folders:
        folder = EXTRACTOR_DIR / f_name
        if folder.exists() and folder.is_dir():
            try:
                shutil.rmtree(folder)
                deleted.append(f_name)
            except Exception:
                pass
    return JSONResponse(content={"success": True, "deleted": deleted})


@app.post("/api/history/clear-all")
async def clear_all_history():
    """Clear all saved Acts in extractor_json/."""
    try:
        count = 0
        for item in EXTRACTOR_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                count += 1
            else:
                item.unlink()
        return JSONResponse(content={"success": True, "cleared_count": count})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clear error: {str(e)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
