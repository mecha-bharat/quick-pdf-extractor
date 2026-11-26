# Quick Clause — Legal Act PDF Extractor

Upload a legal Act PDF and get structured JSON output ready for Quick Clause import.

## Features

- 📄 **PDF Upload** — Drag & drop or click to upload Act PDFs
- 🤖 **Gemini AI Extraction** — Uses Google Gemini to intelligently parse legal structure
- 📋 **Quick Clause Schema** — Output follows the exact 6-key JSON format
- 📥 **Download / Copy** — Get your JSON as a downloadable file or copy to clipboard
- 🎯 **Section Range** — Extract specific sections or the full Act

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set your Gemini API key

Create a `.env` file:

```
GEMINI_API_KEY=your-actual-gemini-api-key
```

Get a free API key from: https://aistudio.google.com/apikey

### 3. Run the server

```bash
python main.py
```

Or:

```bash
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### 4. Open in browser

Go to: http://127.0.0.1:8000

## JSON Output Format

Each provision is extracted as:

```json
{
  "chapter_no": "Chapter-I",
  "chapter_name": "Preliminary",
  "section_no": "1",
  "section_name": "Short title and commencement",
  "sub_section_no": "1(1)",
  "description": "This Act may be called the Example Act, 2025."
}
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Web UI |
| POST | `/api/extract` | Upload PDF and extract JSON |
| GET | `/api/download/{filename}` | Download generated JSON file |
| GET | `/docs` | Swagger API documentation |

## Tech Stack

- **FastAPI** — Backend API
- **Google Gemini API** — AI-powered PDF analysis and structured extraction
- **pdfplumber** — Fallback text extraction
- **Vanilla HTML/JS** — Frontend UI (no build step)
