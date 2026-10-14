"""Turn uploaded files into text. Supports .txt/.md, .pdf (text layer) and .docx."""

from __future__ import annotations

import io
from pathlib import Path


class UnsupportedFile(Exception):
    pass


SUPPORTED = {".txt", ".md", ".pdf", ".docx"}


def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md", ""):
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise UnsupportedFile("PDF has no text layer (scanned?). OCR it first.")
        return text
    if ext == ".docx":
        import docx

        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    raise UnsupportedFile(f"unsupported file type '{ext}' (supported: {', '.join(sorted(SUPPORTED))})")


def title_from(text: str, filename: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip().strip("*_").strip()  # tolerate markdown headings / bold
        if 3 < len(line) < 120:
            if line.isupper():
                small = {"of", "and", "the", "for", "a", "an", "to", "in", "on"}
                words = line.lower().split()
                return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words))
            return line
    return Path(filename).stem or "Untitled agreement"
