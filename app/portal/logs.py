"""Structured logs: one JSON object per line (LOG_FORMAT=json, the default) or plain text (LOG_FORMAT=text).

Request lines hold the route template, never the raw path or query string, because invitation and password
reset links carry their token there. Bodies, cookies, tokens and resume text are never logged."""
import json
import logging
import sys
from datetime import datetime, timezone

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                 "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD and not k.startswith("_")})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure(log_format: str = "json", level: str = "INFO") -> None:
    """Attach one handler to the application's loggers ("jobfinder.*"). Safe to call more than once."""
    root = logging.getLogger("jobfinder")
    for handler in [h for h in root.handlers if getattr(h, "_jobfinder", False)]:
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._jobfinder = True
    handler.setFormatter(JsonFormatter() if log_format.lower() == "json"
                         else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
