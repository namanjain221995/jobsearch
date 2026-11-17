"""Checks made before FastAPI touches a request body.

FastAPI reads and parses the whole body before it resolves dependencies, so without this an anonymous caller
could stream any amount of data to an upload route (spooled to disk) or a JSON route (held in memory) and only
then be told 401. Here:
- a state-changing request without a session cookie is answered 401 and never parsed (except the four public
  forms); at most 1 MB of it is read, and dropped;
- a body larger than the route allows is refused, by its declared length or as soon as it grows past the limit;
- a NUL character in the query string is refused (PostgreSQL cannot store it; it would end as a server error)."""
import json

from fastapi import HTTPException

from ..config import Settings
from .deps import SESSION_COOKIE

PUBLIC_FORMS = {"/api/auth/login", "/api/auth/forgot-password", "/api/auth/reset-password", "/api/invitations/accept"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MULTIPART_OVERHEAD = 64 * 1024
UNAUTHENTICATED_DRAIN = 1024 * 1024          # bytes read and dropped from a request without a session
OVERSIZE_DRAIN = 32 * 1024 * 1024            # the same for a signed-in request whose declared size is over the limit


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
    async def discard(receive, budget: int) -> None:
        """Read and drop up to `budget` bytes of the body before answering. The web app's proxy is still sending
        when the answer arrives; if the connection were closed under it, the person would see its generic 500
        instead of this answer. Nothing is kept, and a body beyond the budget is cut off after all."""
        while budget > 0:
            message = await receive()
            if message["type"] != "http.request":
                return
            budget -= len(message.get("body") or b"")
            if not message.get("more_body"):
                return

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
            await self.discard(receive, UNAUTHENTICATED_DRAIN)
            return await self.reply(send, 401, "Not authenticated")
        limit = self.limit_for(path)
        too_large = f"The request is too large (limit {limit // 1024} KB)."
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self.discard(receive, OVERSIZE_DRAIN)
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
