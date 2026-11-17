"""Resume text extraction (PDF / DOCX / TXT). Files are processed in memory only.

PDF and DOCX are compressed containers: a file of a few kilobytes can expand to gigabytes while it is parsed
(a decompression bomb). Two layers keep that harmless:

1. Size checks before the expensive work: a document whose page descriptions or archive members inflate
   beyond the upload limit is refused.
2. The parsers run in a child process with hard limits on memory, CPU time and run time
   (`extract_text_safely`). Whatever a file manages to do there ends with that process, and the upload is
   answered "could not be read". The child gets no application secrets in its environment.

Nothing heavy is imported at module level, so the child starts quickly."""
import asyncio
import io
import json
import os
import re
import sys
import zipfile

MAX_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 60
ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}
CHILD_MEMORY_BYTES = 1024 * 1024 * 1024         # address space of the parsing process
CHILD_CPU_SECONDS = 10
CHILD_WALL_SECONDS = 20
MAX_PARALLEL_CHILDREN = 4
TOO_DENSE = "This file expands to far more content than a resume holds. Upload a normal PDF, DOCX or TXT resume."
UNREADABLE = "This file could not be read safely. Save it again as a normal PDF, DOCX or TXT file and retry."
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_running = 0


class ResumeError(ValueError):
    """User-facing validation error."""


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def _precheck(filename: str, data: bytes, max_bytes: int) -> str:
    ext = _extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise ResumeError("Unsupported file type. Upload a PDF, DOCX or TXT resume.")
    if not data:
        raise ResumeError("The uploaded file is empty.")
    if len(data) > max_bytes:
        raise ResumeError(f"File is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")
    return ext


def extract_text(filename: str, data: bytes, max_bytes: int) -> tuple[str, bool]:
    """Return (text, truncated). Raises ResumeError with a friendly message. Runs the parser in this process:
    call `extract_text_safely` for files that come from a user."""
    ext = _precheck(filename, data, max_bytes)
    try:
        if ext == "pdf":
            text = _pdf(data, max_bytes)
        elif ext == "docx":
            text = _docx(data, max_bytes)
        else:
            text = _txt(data)
    except ResumeError:
        raise
    except Exception:
        raise ResumeError(f"Could not read this {ext.upper()} file. It may be corrupted or password protected.") from None
    text = re.sub(r"[ \t\r\f\v]+", " ", text.replace("\x00", " "))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) < 40:
        raise ResumeError("No readable text found in the resume. Scanned image PDFs are not supported.")
    truncated = len(text) > MAX_TEXT_CHARS
    return text[:MAX_TEXT_CHARS], truncated


def _pdf(data: bytes, max_bytes: int) -> str:
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise ResumeError("This file is not a valid PDF.")
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ResumeError("Password-protected PDFs are not supported.")
        if doc.page_count > MAX_PDF_PAGES:
            raise ResumeError(f"PDF has too many pages (limit {MAX_PDF_PAGES}).")
        budget, parts, total = max_bytes, [], 0
        for page in doc:
            for xref in page.get_contents():               # the page description, inflated
                budget -= len(doc.xref_stream(xref) or b"")
                if budget < 0:
                    raise ResumeError(TOO_DENSE)
            text = page.get_text("text")
            parts.append(text)
            total += len(text)
            if total > MAX_TEXT_CHARS * 4:                  # the rest would be cut off anyway
                break
        return "\n".join(parts)


def _docx(data: bytes, max_bytes: int) -> str:
    if not data.startswith(b"PK"):
        raise ResumeError("This file is not a valid DOCX document.")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if sum(i.file_size for i in zf.infolist()) > max_bytes:      # zipfile never reads more than these sizes
            raise ResumeError(TOO_DENSE)
        if "word/document.xml" not in zf.namelist():
            raise ResumeError("This file is not a valid DOCX document.")
    import docx
    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" ".join(c.text for c in row.cells))
    return "\n".join(parts)


def _txt(data: bytes) -> str:
    if b"\x00" in data[:4096]:
        raise ResumeError("This file does not look like plain text.")
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ResumeError("Could not decode the text file.")


# ---------------------------------------------------------------- limited child process
def _limit_child() -> None:                                  # runs in the child, before the interpreter starts
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.nice(5)


async def extract_text_safely(filename: str, data: bytes, max_bytes: int) -> tuple[str, bool]:
    """`extract_text` for files that come from a user: PDF and DOCX are parsed in a limited child process."""
    global _running
    ext = _precheck(filename, data, max_bytes)
    if ext == "txt":                                         # no parser, nothing to contain
        return extract_text(filename, data, max_bytes)
    if _running >= MAX_PARALLEL_CHILDREN:
        raise ResumeError("The server is reading other resumes right now. Try again in a moment.")
    _running += 1
    try:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.resume_extract", ext, str(max_bytes), cwd=_ROOT, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=_limit_child if os.name == "posix" else None)
        try:
            output, _ = await asyncio.wait_for(process.communicate(data), CHILD_WALL_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise ResumeError(UNREADABLE) from None
    finally:
        _running -= 1
    if process.returncode != 0 or not output:                # killed by a limit, or the parser crashed
        raise ResumeError(UNREADABLE)
    try:
        result = json.loads(output)
    except ValueError:
        raise ResumeError(UNREADABLE) from None
    if result.get("error"):
        raise ResumeError(str(result["error"])[:300])
    return str(result["text"])[:MAX_TEXT_CHARS], bool(result["truncated"])


def _child_main() -> int:
    ext, max_bytes = sys.argv[1], int(sys.argv[2])
    data = sys.stdin.buffer.read(max_bytes + 1)
    try:
        text, truncated = extract_text(f"resume.{ext}", data, max_bytes)
        result = {"text": text, "truncated": truncated}
    except ResumeError as exc:
        result = {"error": str(exc)}
    except MemoryError:
        result = {"error": TOO_DENSE}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_child_main())
