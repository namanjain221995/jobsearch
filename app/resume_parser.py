"""Resume upload validation and text extraction (PDF / DOCX / TXT), plus a deterministic
skills profile used when no AI provider is available. Files are processed in memory only."""
import io
import re
import zipfile

from .schemas import Profile

MAX_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 60
MAX_DOCX_UNCOMPRESSED = 60 * 1024 * 1024
ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}


class ResumeError(ValueError):
    """User-facing validation error."""


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def extract_text(filename: str, data: bytes, max_bytes: int) -> tuple[str, bool]:
    """Return (text, truncated). Raises ResumeError with a friendly message."""
    ext = _extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise ResumeError("Unsupported file type. Upload a PDF, DOCX or TXT resume.")
    if not data:
        raise ResumeError("The uploaded file is empty.")
    if len(data) > max_bytes:
        raise ResumeError(f"File is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.")
    try:
        if ext == "pdf":
            text = _pdf(data)
        elif ext == "docx":
            text = _docx(data)
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


def _pdf(data: bytes) -> str:
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise ResumeError("This file is not a valid PDF.")
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ResumeError("Password-protected PDFs are not supported.")
        if doc.page_count > MAX_PDF_PAGES:
            raise ResumeError(f"PDF has too many pages (limit {MAX_PDF_PAGES}).")
        return "\n".join(page.get_text("text") for page in doc)


def _docx(data: bytes) -> str:
    if not data.startswith(b"PK"):
        raise ResumeError("This file is not a valid DOCX document.")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        if sum(i.file_size for i in zf.infolist()) > MAX_DOCX_UNCOMPRESSED:
            raise ResumeError("DOCX file expands to an unreasonable size.")
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


# ---------------------------------------------------------------- skills vocabulary
# canonical name -> regex (case-insensitive unless flagged). Shared by resume and job-description matching.
_V = {
    "Python": r"python", "Java": r"java(?!script)", "JavaScript": r"javascript|\bjs\b", "TypeScript": r"typescript",
    "Go": r"golang|\bgo\b(?=\s*(?:,|/|\)|lang|and\b|or\b))", "Rust": r"rust", "C++": r"c\+\+", "C#": r"c#|\.net\b", "C": r"\bc\b(?=\s*(?:,|/)\s*c\+\+)",
    "Scala": r"scala", "Kotlin": r"kotlin", "Swift": r"swift", "Ruby": r"ruby", "PHP": r"php", "R": r"\bR\b(?=\s*(?:,|/|and|or|programming|language|\)))",
    "SQL": r"sql", "Bash": r"bash|shell scripting", "MATLAB": r"matlab",
    "Machine Learning": r"machine learning|\bml\b", "Deep Learning": r"deep learning|neural networks?",
    "NLP": r"\bnlp\b|natural language processing", "Computer Vision": r"computer vision|\bcv\b(?= models?)",
    "LLM": r"\bllms?\b|large language models?", "Generative AI": r"generative ai|gen ?ai",
    "RAG": r"\brag\b|retrieval[- ]augmented", "Prompt Engineering": r"prompt engineering",
    "Fine-tuning": r"fine[- ]?tun(?:e|ing)", "Reinforcement Learning": r"reinforcement learning",
    "Recommendation Systems": r"recommend(?:er|ation) systems?", "MLOps": r"mlops|ml ops",
    "AI Agents": r"\bagents?\b(?= (?:framework|system|workflow))|agentic|multi-agent",
    "PyTorch": r"pytorch", "TensorFlow": r"tensorflow", "Keras": r"keras", "JAX": r"\bjax\b",
    "scikit-learn": r"scikit[- ]?learn|sklearn", "XGBoost": r"xgboost|lightgbm", "Hugging Face": r"hugging ?face|transformers library",
    "Transformers": r"transformers?\b(?! library)", "LangChain": r"langchain|langgraph", "LlamaIndex": r"llamaindex|llama index",
    "OpenAI API": r"openai", "Anthropic API": r"anthropic|claude", "Vector Databases": r"vector (?:db|database|store)s?|pinecone|weaviate|faiss|milvus|qdrant|chroma",
    "Pandas": r"pandas", "NumPy": r"numpy", "Spark": r"spark|pyspark", "Hadoop": r"hadoop", "Airflow": r"airflow",
    "dbt": r"\bdbt\b", "Kafka": r"kafka", "Flink": r"flink", "Snowflake": r"snowflake", "Databricks": r"databricks",
    "BigQuery": r"bigquery", "Redshift": r"redshift", "ETL": r"\betl\b|\belt\b|data pipelines?",
    "Statistics": r"statistics|statistical", "A/B Testing": r"a/b test(?:ing|s)?|experimentation",
    "Data Visualization": r"data visuali[sz]ation|tableau|power ?bi|looker",
    "AWS": r"\baws\b|amazon web services", "GCP": r"\bgcp\b|google cloud", "Azure": r"azure",
    "Docker": r"docker", "Kubernetes": r"kubernetes|\bk8s\b", "Terraform": r"terraform", "Ansible": r"ansible",
    "CI/CD": r"ci/cd|continuous integration|github actions|jenkins|gitlab ci", "Linux": r"linux|unix",
    "Git": r"\bgit\b|github|gitlab", "Serverless": r"serverless|aws lambda", "SageMaker": r"sagemaker", "Vertex AI": r"vertex ai",
    "Bedrock": r"bedrock", "Ray": r"\bray\b(?= |,|\))", "MLflow": r"mlflow", "Kubeflow": r"kubeflow",
    "Triton": r"triton", "ONNX": r"onnx", "CUDA": r"cuda", "GPU": r"\bgpus?\b", "Distributed Systems": r"distributed systems?",
    "Microservices": r"microservices?", "REST APIs": r"rest(?:ful)? apis?|\brest\b", "GraphQL": r"graphql", "gRPC": r"grpc",
    "FastAPI": r"fastapi", "Django": r"django", "Flask": r"flask", "Node.js": r"node\.?js", "React": r"react(?:\.?js)?",
    "Next.js": r"next\.?js", "Vue": r"vue(?:\.?js)?", "Angular": r"angular", "Spring": r"spring ?boot|\bspring\b",
    "PostgreSQL": r"postgres(?:ql)?", "MySQL": r"mysql", "MongoDB": r"mongodb", "Redis": r"redis",
    "Elasticsearch": r"elasticsearch|opensearch", "DynamoDB": r"dynamodb", "Cassandra": r"cassandra",
    "NoSQL": r"nosql", "Data Modeling": r"data model(?:ing|ling)", "System Design": r"system design",
    "Observability": r"observability|prometheus|grafana|datadog", "Security": r"cybersecurity|application security|infosec",
    "Networking": r"\btcp/ip\b|networking", "Agile": r"agile|scrum", "Testing": r"unit test(?:ing|s)?|pytest|test automation|\btdd\b",
    "Model Deployment": r"model (?:deployment|serving)|inference (?:serving|optimization|pipelines?)",
    "Model Evaluation": r"model evaluation|\bevals?\b", "Feature Engineering": r"feature engineering|feature stores?",
    "Time Series": r"time[- ]series", "Optimization": r"\boptimization\b", "Robotics": r"robotics|\bros\b",
    "Embedded": r"embedded systems?|firmware", "iOS": r"\bios\b", "Android": r"android",
    "HTML/CSS": r"\bhtml5?\b|\bcss3?\b", "Excel": r"\bexcel\b",
}
_CASE_SENSITIVE = {"R", "Go"}
SKILL_PATTERNS: dict[str, re.Pattern] = {}
def _probes(pattern: str) -> tuple[str, ...] | None:
    """Literal text that must be present (lower-cased) for each top-level alternative of a skill pattern.
    Lets find_skills skip the regex for skills that cannot occur. None = no reliable literal, always run."""
    alternatives, depth, current = [], 0, ""
    for i, ch in enumerate(pattern):
        escaped = i > 0 and pattern[i - 1] == "\\"
        if ch in "([" and not escaped:
            depth += 1
        elif ch in ")]" and not escaped:
            depth -= 1
        if ch == "|" and depth == 0 and not escaped:
            alternatives.append(current)
            current = ""
        else:
            current += ch
    alternatives.append(current)
    probes = []
    for alt in alternatives:
        if alt.startswith("\\b"):
            alt = alt[2:]
        literal, i = "", 0
        while i < len(alt):
            ch = alt[i]
            if ch == "\\" and i + 1 < len(alt) and alt[i + 1] in "+.#-/":
                literal += alt[i + 1]
                i += 2
            elif ch.isalnum() or ch in " /_-":
                literal += ch
                i += 1
            else:
                if ch in "?*{":                    # the previous character is optional
                    literal = literal[:-1]
                break
        if len(literal.strip()) < 2:
            return None
        probes.append(literal.lower())
    return tuple(probes)


SKILL_PROBES: dict[str, tuple[str, ...] | None] = {}
for _name, _rx in _V.items():
    SKILL_PATTERNS[_name] = re.compile(rf"(?<![A-Za-z0-9+#])(?:{_rx})(?![A-Za-z0-9+#])", 0 if _name in _CASE_SENSITIVE else re.I)
    SKILL_PROBES[_name] = _probes(_rx)


def find_skills(text: str) -> list[str]:
    text = text or ""
    lowered = text.lower()
    found = []
    for name, rx in SKILL_PATTERNS.items():
        probes = SKILL_PROBES[name]
        if probes is not None and not any(probe in lowered for probe in probes):
            continue
        if rx.search(text):
            found.append(name)
    return found


_TITLE_LINE = re.compile(
    r"\b((?:senior|staff|principal|lead|junior|associate|sr\.?|jr\.?)\s+)?"
    r"((?:[A-Z][A-Za-z/&+.-]*\s+){0,3}(?:Engineer|Developer|Scientist|Analyst|Architect|Researcher|Manager|Consultant|Administrator|Designer|Intern))\b")


def heuristic_profile(text: str) -> Profile:
    """Deterministic resume profile: vocabulary skills and title-looking lines. No inference of personal attributes."""
    skills = find_skills(text)
    titles, seen = [], set()
    for m in _TITLE_LINE.finditer(text):
        t = re.sub(r"\s+", " ", m.group(0)).strip()
        if 5 < len(t) < 60 and t.lower() not in seen:
            seen.add(t.lower())
            titles.append(t)
        if len(titles) >= 8:
            break
    education = []
    for m in re.finditer(r"(?im)^.*\b(b\.?s\.?c?|m\.?s\.?c?|bachelor|master|ph\.?d|mba|b\.?tech|m\.?tech|b\.?e\.?)\b.*$", text):
        line = re.sub(r"\s+", " ", m.group(0)).strip()
        if 6 < len(line) < 160:
            education.append(line)
        if len(education) >= 4:
            break
    return Profile(skills=skills, job_titles=titles, education=education, source="heuristic")
