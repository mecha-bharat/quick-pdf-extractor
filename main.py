"""
PDF Extraction API for Legal Acts — Quick Clause Format

FastAPI backend that:
1. Accepts PDF uploads
2. Quick-inspects PDF page count (> 15 pages asks user for page range / section range)
3. Slices PDF if page range is selected for faster, reliable AI processing
4. Sends PDF to Gemini API for intelligent structural parsing
5. Returns structured JSON per Quick Clause schema with retry logic & fallback
"""

import asyncio
import io
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

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
    description="Upload a legal Act PDF and get structured JSON for Quick Clause",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directory to store generated JSON outputs
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Gemini client & retry config
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Verified active models with generous free-tier limits, prioritized by availability & stability
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


def get_gemini_client():
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY is not set. Please set it in your .env file.",
        )
    return genai.Client(api_key=GEMINI_API_KEY)


def call_gemini_with_retry(client, model: str, uploaded_file, prompt: str):
    """
    Call Gemini with retry + exponential backoff on 503/429 errors.
    Falls back quickly to alternate working models if the primary is experiencing high demand.
    """

    # Build ordered model list: requested model first, then fallbacks
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
# Post-Processing & Normalization (Bulletproof Clause Unbundling & Cleaning)
# ---------------------------------------------------------------------------
import re


def clean_identifier(text: str) -> str:
    """Clean stray footnote reference numbers and brackets from structural identifiers."""
    if not text:
        return ""
    s = str(text).strip()
    # Strip superscript footnote characters like ¹, ², ³, etc.
    s = re.sub(r"^[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", s)
    # Strip surrounding square brackets e.g. '[14A]' -> '14A' or '[3(2)]' -> '3(2)'
    s = re.sub(r"^\[\s*", "", s)
    s = re.sub(r"\s*\]$", "", s)
    s = re.sub(r"^Section\s+", "", s, flags=re.IGNORECASE)
    return s.strip()


def clean_description(desc: str) -> str:
    """Clean editorial footnote superscripts attached to amendment brackets in descriptions."""
    if not desc:
        return ""
    s = str(desc)
    s = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+\s*\[", "[", s)
    s = re.sub(r"\]\s*[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "]", s)
    return s.strip()


def normalize_provision_object(obj: dict) -> list:
    """
    Unbundle ordinary lettered clauses (a), (b), (c)... if the model accidentally
    merged them into a single parent description.
    LEAVES internal sub-clauses of Provisos and Explanations intact inside their description!
    """
    desc = obj.get("description", "")
    sub_no = clean_identifier(obj.get("sub_section_no", ""))
    sec_no = clean_identifier(obj.get("section_no", ""))

    obj["section_no"] = sec_no
    obj["sub_section_no"] = sub_no
    obj["description"] = clean_description(desc)

    # If the description starts with a proviso or explanation, DO NOT SPLIT internal clauses!
    if re.match(r"^\s*(?:\[\s*)?(?:Provided|Explanation)\b", obj["description"], re.IGNORECASE):
        return [obj]

    # If the description contains a quoted amendment block (e.g. namely:—\n"(a)..."), DO NOT SPLIT!
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

    # If no clauses or fewer than 2 clauses found, keep as-is (except split trailing proviso per sequence rule)
    if not has_clauses or len(clauses) < 2:
        if prov_part:
            obj1 = dict(obj)
            obj1["description"] = main_part
            obj2 = dict(obj)
            obj2["description"] = prov_part
            return [obj1, obj2]
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

    if prov_part:
        o_prov = dict(obj)
        o_prov["sub_section_no"] = sub_no
        o_prov["description"] = prov_part
        results.append(o_prov)

    return results


def post_process_extracted_data(data: list) -> list:
    """Apply safety normalization, clause unbundling, and identifier cleanup across all objects."""
    if not isinstance(data, list):
        return data

    processed = []
    for item in data:
        if isinstance(item, dict):
            unbundled = normalize_provision_object(item)
            processed.extend(unbundled)
        else:
            processed.append(item)

    cleaned_list = []
    required_keys = ["chapter_no", "chapter_name", "section_no", "section_name", "sub_section_no", "description"]
    for obj in processed:
        if isinstance(obj, dict):
            clean_obj = {}
            for k in required_keys:
                val = obj.get(k, "")
                clean_obj[k] = str(val) if val is not None else ""
            clean_obj["section_no"] = clean_identifier(clean_obj["section_no"])
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
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/inspect")
async def inspect_pdf(file: UploadFile = File(...)):
    """
    Instantly inspect PDF metadata (page count) upon selection.
    Threshold: 15 pages.
    """
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
    file: UploadFile = File(...),
    mode: str = Form(default="full"),  # 'full', 'page_range', or 'section_range'
    start_page: int = Form(default=1),
    end_page: int = Form(default=0),
    section_range: str = Form(default=""),
    model: str = Form(default="gemini-3.5-flash"),
):
    """
    Upload a legal Act PDF and extract structured JSON.

    - **file**: PDF file to process
    - **mode**: 'full' | 'page_range' | 'section_range'
    - **start_page**: Starting page number (1-indexed) if mode is page_range
    - **end_page**: Ending page number (inclusive) if mode is page_range
    - **section_range**: Optional section range (e.g., "1-30")
    - **model**: Gemini model to use
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Invalid file format. Only PDF files are allowed.",
        )

    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    tmp_path = None
    sliced_path = None

    try:
        # Read PDF to get actual page count
        reader = PdfReader(io.BytesIO(contents))
        total_pages = len(reader.pages)

        # Handle page range slicing if requested
        file_to_upload = None
        page_info = None

        if mode == "page_range" and start_page >= 1:
            actual_end = min(end_page if end_page > 0 else total_pages, total_pages)
            actual_start = max(1, min(start_page, actual_end))

            logger.info(
                f"Slicing PDF '{file.filename}' from page {actual_start} to {actual_end} (total: {total_pages})"
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
            # Full PDF
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".pdf", prefix="act_"
            ) as tmp:
                tmp.write(contents)
                tmp_path = tmp.name
            file_to_upload = tmp_path

        # Build extraction prompt
        range_text = section_range.strip() if section_range else None
        prompt = get_extraction_prompt(section_range=range_text, page_info=page_info)

        # Initialize Gemini client and upload file
        client = get_gemini_client()

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

            # Save output file with descriptive name
            safe_name = (
                Path(file.filename).stem.replace(" ", "_") if file.filename else "output"
            )
            output_id = str(uuid.uuid4())[:8]

            if mode == "page_range":
                output_filename = f"{safe_name}_pages_{start_page}-{end_page}_{output_id}.json"
            elif range_text:
                output_filename = f"{safe_name}_sec_{range_text}_{output_id}.json"
            else:
                output_filename = f"{safe_name}_{output_id}.json"

            output_path = OUTPUT_DIR / output_filename

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(extracted_data, f, ensure_ascii=False, indent=2)

            # Build section summary
            sections = sorted(
                set(obj.get("section_no", "") for obj in extracted_data if obj.get("section_no"))
            )

            return JSONResponse(
                content={
                    "success": True,
                    "filename": output_filename,
                    "model_used": used_model,
                    "total_pages": total_pages,
                    "pages_processed": page_info if page_info else f"1 to {total_pages}",
                    "total_objects": len(extracted_data),
                    "sections_found": sections,
                    "total_sections": len(sections),
                    "download_url": f"/api/download/{output_filename}",
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
        # Clean up temporary local files
        for p in [tmp_path, sliced_path]:
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except Exception:
                    pass


@app.get("/api/download/{filename}")
async def download_json(filename: str):
    """Download a generated JSON file."""
    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
