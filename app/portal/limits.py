"""Checks made before FastAPI touches a request body.

FastAPI reads and parses the whole body before it resolves dependencies, so without this an anonymous caller
could stream any amount of data to an upload route (spooled to disk) or a JSON route (held in memory) and only
then be told 401. Here:
- a state-changing request without a session cookie is answered 401 unread (except the four public forms);
- a body larger than the route allows is refused, by its declared length or as soon as it grows past the limit;
- a NUL character in the query string is refused (PostgreSQL cannot store it; it would end as a server error)."""
import json

from fastapi import HTTPException

from ..config import Settings
from .deps import SESSION_COOKIE

PUBLIC_FORMS = {"/api/auth/login", "/api/auth/forgot-password", "/api/auth/reset-password", "/api/invitations/accept"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MULTIPART_OVERHEAD = 64 * 1024


class RequestGuardMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    def limit_for(self, path: str) -> int:
        if path == "/api/resumes/upload":
            return self.settings.max_upload_bytes + MULTIPART_OVERHEAD
        if path == "/api/jobs/import-csv":
            return self.settings.max_csv_bytes + MULTIPART_OVERHEAD
        return self.settings.max_json_body_bytes

    @staticmethod
    async def reply(send, status: int, error: str) -> None:
        body = json.dumps({"error": error, "detail": ""}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if b"%00" in scope.get("query_string", b""):
            return await self.reply(send, 422, "Text must not contain the NUL character")
        if scope["method"] not in UNSAFE_METHODS:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        path = scope["path"]
        if path not in PUBLIC_FORMS and f"{SESSION_COOKIE}=".encode() not in headers.get(b"cookie", b""):
            return await self.reply(send, 401, "Not authenticated")
        limit = self.limit_for(path)
        too_large = f"The request is too large (limit {limit // 1024} KB)."
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            return await self.reply(send, 413, too_large)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:                      # no (or a false) Content-Length: stop reading here
                    raise HTTPException(413, too_large)
            return message

        await self.app(scope, limited_receive, send)
