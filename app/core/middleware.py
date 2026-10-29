import re


class UploadRequestTooLarge(Exception):
    pass


class MRIUploadSizeLimitMiddleware:
    """Reject oversized MRI request bodies before Starlette parses multipart data."""

    upload_path = re.compile(r"^/api/v1/visits/\d+/uploads$")

    def __init__(self, app, max_file_bytes: int, overhead_bytes: int = 256 * 1024):
        self.app = app
        self.max_request_bytes = max_file_bytes + overhead_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not self.upload_path.fullmatch(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        try:
            if content_length is not None and int(content_length) > self.max_request_bytes:
                await self._too_large(send)
                return
        except ValueError:
            await self._too_large(send)
            return

        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_request_bytes:
                    raise UploadRequestTooLarge
            return message

        response_started = False

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except UploadRequestTooLarge:
            if response_started:
                raise
            await self._too_large(send)

    async def _too_large(self, send):
        body = b'{"detail":"MRI request exceeds configured size limit"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
