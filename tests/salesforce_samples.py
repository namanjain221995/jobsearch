"""Synthetic resume files for the Salesforce tests and the demo stack. Nobody here is a real person."""
import io

import docx
import pymupdf as fitz

RESUME_TEXT = ("{name}\nMachine Learning Engineer\nSummary: Python engineer with experience building and deploying models.\n"
               "Skills: Python, PyTorch, SQL, Docker, AWS, FastAPI\nExperience\nMachine Learning Engineer, Example Corp (2023-2026)\n"
               "Education\nB.S. Computer Science, Example University")


def make_pdf(name: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), RESUME_TEXT.format(name=name), fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def make_docx(name: str) -> bytes:
    document = docx.Document()
    for line in RESUME_TEXT.format(name=name).split("\n"):
        document.add_paragraph(line)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()
