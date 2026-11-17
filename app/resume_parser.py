"""Resume profile extraction: a deterministic skills vocabulary and profile used when no AI provider is
available. Reading the uploaded file itself (PDF / DOCX / TXT) lives in resume_extract.py, which runs the
parsers in a separate, resource-limited process."""
import re

from .resume_extract import (ALLOWED_EXTENSIONS, MAX_PDF_PAGES, MAX_TEXT_CHARS, ResumeError, extract_text,  # noqa: F401
                             extract_text_safely)
from .schemas import Profile


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
