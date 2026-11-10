"""Contract tests for the Control API models (used by the mobile app)."""

from __future__ import annotations

import unittest

from core.api_models import (
    AnswerInterruptRequest,
    ChatRequest,
    ChatResponse,
    DecideResponse,
    PushKeys,
    PushSubscribeRequest,
    StartRunRequest,
    WorkflowSuggestion,
)


class ChatModelsTests(unittest.TestCase):
    def test_defaults(self):
        req = ChatRequest(message="ciao")
        self.assertEqual(req.history, [])
        self.assertTrue(req.auto_route)

    def test_chat_response_with_suggestion(self):
        resp = ChatResponse(
            reply="Ok",
            workflow=WorkflowSuggestion(
                workflow_id=3, name="Report", inputs={"topic": "x"}, missing_inputs=["date"]
            ),
        )
        dumped = resp.model_dump()
        self.assertEqual(dumped["workflow"]["workflow_id"], 3)
        self.assertEqual(dumped["workflow"]["missing_inputs"], ["date"])

    def test_chat_response_without_suggestion(self):
        self.assertIsNone(ChatResponse(reply="solo testo").model_dump()["workflow"])

    def test_decide_response_optional_workflow(self):
        data = DecideResponse(text="a", action="clarify").model_dump()
        self.assertIsNone(data["workflow_id"])
        self.assertEqual(data["inputs"], {})


class RunModelsTests(unittest.TestCase):
    def test_start_run_defaults(self):
        req = StartRunRequest(workflow_id=1)
        self.assertEqual(req.source, "mobile")
        self.assertEqual(req.inputs, {})

    def test_answer_interrupt_defaults(self):
        req = AnswerInterruptRequest(answer="ok")
        self.assertTrue(req.execute)


class PushModelsTests(unittest.TestCase):
    def test_subscribe_requires_keys(self):
        req = PushSubscribeRequest(
            endpoint="https://push/1",
            keys=PushKeys(p256dh="pk", auth="auth"),
        )
        data = req.model_dump()
        self.assertEqual(data["endpoint"], "https://push/1")
        self.assertEqual(data["keys"]["p256dh"], "pk")
        self.assertEqual(data["user_agent"], "")

    def test_subscribe_rejects_missing_keys(self):
        with self.assertRaises(Exception):
            PushSubscribeRequest(endpoint="https://push/1")

    def test_models_exposed_in_schema(self):
        from core import api_models

        for name in ("PushKeys", "PushSubscribeRequest", "PushUnsubscribeRequest", "PushKeyResponse"):
            self.assertTrue(hasattr(api_models, name), name)


class FileModelTests(unittest.TestCase):
    def test_preview_model(self):
        from core.api_models import FilePreview

        preview = FilePreview(
            name="data.csv",
            path="data.csv",
            kind="table",
            rows=[["a", "b"]],
            truncated=False,
        )
        data = preview.model_dump()
        self.assertEqual(data["rows"], [["a", "b"]])
        self.assertIsNone(data["data_uri"])

    def test_share_ttl_is_bounded(self):
        from core.api_models import ShareFileRequest

        request = ShareFileRequest(path="a.txt", ttl_seconds=900)
        self.assertEqual(request.ttl_seconds, 900)
        with self.assertRaises(Exception):
            ShareFileRequest(path="a.txt", ttl_seconds=10)
        with self.assertRaises(Exception):
            ShareFileRequest(path="a.txt", ttl_seconds=10**9)


class UploadModelTests(unittest.TestCase):
    def test_session_request_bounds(self):
        from core.api_models import UploadSessionRequest

        request = UploadSessionRequest(path="inbox/a.bin", size=2048, chunk_size=65536)
        self.assertEqual(request.size, 2048)
        with self.assertRaises(Exception):
            UploadSessionRequest(path="inbox/a.bin", size=0)
        with self.assertRaises(Exception):
            UploadSessionRequest(path="inbox/a.bin", size=2048, chunk_size=1024)
        with self.assertRaises(Exception):
            UploadSessionRequest(path="inbox/a.bin", size=2048, sha256="not-a-hash")

    def test_session_serialization(self):
        from core.api_models import UploadSession, UploadChunkStatus

        session = UploadSession(
            upload_id="a" * 32,
            path="inbox/a.bin",
            size=2048,
            chunk_size=1024,
            chunks=2,
            received=[0],
            expires_at="2026-02-01 10:00:00",
        )
        data = session.model_dump()
        self.assertEqual(data["received"], [0])
        status = UploadChunkStatus(
            upload_id="a" * 32, index=0, received=[0], chunks=2, complete=False
        )
        self.assertFalse(status.model_dump()["complete"])

    def test_complete_request_checksum_pattern(self):
        from core.api_models import CompleteUploadRequest

        self.assertIsNone(CompleteUploadRequest().sha256)
        self.assertEqual(CompleteUploadRequest(sha256="A" * 64).sha256, "A" * 64)
        with self.assertRaises(Exception):
            CompleteUploadRequest(sha256="broken")


class SystemStatusModelTests(unittest.TestCase):
    def test_parses_gate_snapshot(self):
        from core.api_models import SystemStatus

        status = SystemStatus(
            gpu={"available": True, "name": "RTX", "free_mb": 11288, "reserve_mb": 500},
            profiles={
                "fast": {
                    "name": "fast",
                    "model": "m",
                    "fits": True,
                    "headroom_mb": 4788,
                    "description": "extra key is fine",
                }
            },
            queue={
                "current": {"profile": "fast", "run_id": 7},
                "waiting": [{"position": 1, "profile": "deep", "run_id": 8}],
                "length": 1,
            },
            metrics={"served": 2, "first_token": {"fast": {"count": 1, "avg_s": 0.3}}},
        )
        data = status.model_dump()
        self.assertEqual(data["queue"]["length"], 1)
        self.assertEqual(data["queue"]["waiting"][0]["run_id"], 8)
        self.assertTrue(data["profiles"]["fast"]["fits"])
        self.assertEqual(data["metrics"]["served"], 2)

    def test_defaults_are_safe(self):
        from core.api_models import SystemStatus

        status = SystemStatus()
        self.assertFalse(status.gpu.available)
        self.assertIsNone(status.queue.current)
        self.assertEqual(status.queue.waiting, [])


class RunEventModelTests(unittest.TestCase):
    def test_page_serialization(self):
        from core.api_models import RunEvent, RunEventPage

        page = RunEventPage(
            run_id=3,
            latest_seq=2,
            events=[
                RunEvent(run_id=3, seq=1, type="run.started", payload={"source": "mobile"}),
                RunEvent(run_id=3, seq=2, type="run.finished"),
            ],
        )
        data = page.model_dump()
        self.assertEqual(data["run_id"], 3)
        self.assertEqual([e["seq"] for e in data["events"]], [1, 2])
        self.assertEqual(data["events"][0]["payload"], {"source": "mobile"})
        self.assertEqual(data["latest_seq"], 2)


if __name__ == "__main__":
    unittest.main()
