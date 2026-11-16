import asyncio
import json
import unittest

from request_limits import GIB, MIB, RequestBodyLimitMiddleware, _limit_for


class RequestBodyLimitTests(unittest.TestCase):
    def run_asgi(self, *, path="/api/example", headers=(), messages=(), default_limit=4):
        received = []
        sent = []
        app_called = False
        queue = list(messages)

        async def receive():
            if queue:
                message = queue.pop(0)
            else:
                message = {"type": "http.request", "body": b"", "more_body": False}
            received.append(message)
            return message

        async def send(message):
            sent.append(message)

        async def app(_scope, inner_receive, inner_send):
            nonlocal app_called
            app_called = True
            while True:
                message = await inner_receive()
                if message.get("type") != "http.request" or not message.get("more_body"):
                    break
            await inner_send({"type": "http.response.start", "status": 200, "headers": []})
            await inner_send({"type": "http.response.body", "body": b"ok", "more_body": False})

        scope = {
            "type": "http", "method": "POST", "path": path,
            "headers": list(headers),
        }
        asyncio.run(RequestBodyLimitMiddleware(app, default_limit)(scope, receive, send))
        status = next(item["status"] for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return status, body, received, app_called

    def test_rejects_declared_oversize_before_calling_application(self):
        status, body, received, app_called = self.run_asgi(
            headers=[(b"content-length", b"5")],
            messages=[{"type": "http.request", "body": b"12345", "more_body": False}],
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["max_bytes"], 4)
        self.assertEqual(received, [])
        self.assertFalse(app_called)

    def test_rejects_chunked_oversize_without_consuming_the_rest(self):
        messages = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": True},
            {"type": "http.request", "body": b"789", "more_body": False},
        ]
        status, body, received, app_called = self.run_asgi(messages=messages)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["max_bytes"], 4)
        self.assertEqual(len(received), 2)
        self.assertTrue(app_called)

    def test_accepts_body_within_limit(self):
        status, body, _received, app_called = self.run_asgi(
            messages=[{"type": "http.request", "body": b"1234", "more_body": False}],
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertTrue(app_called)

    def test_preserves_large_model_upload_cap_and_normalizes_versioned_routes(self):
        model_limit = 2 * GIB + MIB
        self.assertEqual(_limit_for("POST", "/api/admin/ml/model-sources", 16 * MIB), model_limit)
        self.assertEqual(_limit_for("POST", "/api/v1/admin/ml/model-sources", 16 * MIB), model_limit)
        self.assertEqual(_limit_for("POST", "/api/admin/ml/model-sources-extra", 16 * MIB), 16 * MIB)
        self.assertEqual(_limit_for("POST", "/api/enroll/invitation-token", 16 * MIB), 33 * MIB)


if __name__ == "__main__":
    unittest.main()
