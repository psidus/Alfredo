"""Tests for the dependency-free mobile client (no live server)."""

from __future__ import annotations

import hashlib
import json
import unittest

from mobile_client import (
    ControlAuthError,
    ControlClient,
    ControlConflictError,
    ControlConnectionError,
    ControlError,
    ControlNotFoundError,
    ControlServerError,
    ControlValidationError,
)
from mobile_client.models import Interrupt, RunDetail, WorkflowCard


class FakeTransport:
    """Records requests and replays queued ``(status, payload)`` answers."""

    def __init__(self, *responses, headers=None):
        self.responses = list(responses)
        self.calls = []
        self.headers = headers

    def __call__(self, method, url, headers, body, timeout):
        decoded = None
        if body:
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = body  # raw chunk (application/octet-stream)
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": decoded,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"Unexpected call: {method} {url}")
        status, payload = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, raw


def client_with(*responses, token="secret"):
    transport = FakeTransport(*responses)
    return ControlClient("http://alfredo.local:8000/", token=token, transport=transport), transport


class ChatTests(unittest.TestCase):
    def test_chat_posts_history_and_parses_suggestion(self):
        client, transport = client_with(
            (
                200,
                {
                    "reply": "Posso lanciare il report.",
                    "workflow": {
                        "workflow_id": 7,
                        "name": "Report termico",
                        "text": "Lancio.",
                        "inputs": {"documento": "dati.pdf"},
                        "missing_inputs": ["lingua"],
                    },
                },
            )
        )
        reply = client.chat(
            "Esegui il report", history=[{"role": "user", "content": "ciao"}]
        )

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "http://alfredo.local:8000/api/assistant/chat")
        self.assertEqual(call["headers"]["X-Control-Token"], "secret")
        self.assertEqual(call["body"]["message"], "Esegui il report")
        self.assertTrue(call["body"]["auto_route"])
        self.assertEqual(reply.reply, "Posso lanciare il report.")
        self.assertIsNotNone(reply.workflow)
        self.assertEqual(reply.workflow.workflow_id, 7)
        self.assertEqual(reply.workflow.missing_inputs, ["lingua"])
        self.assertFalse(reply.workflow.ready_to_run)

    def test_chat_without_suggestion(self):
        client, _ = client_with((200, {"reply": "Ciao!", "workflow": None}))
        reply = client.chat("ciao")
        self.assertEqual(reply.reply, "Ciao!")
        self.assertIsNone(reply.workflow)

    def test_decide_exposes_run_intent(self):
        client, transport = client_with(
            (
                200,
                {
                    "text": "Avvio.",
                    "action": "run_workflow",
                    "workflow_id": 3,
                    "inputs": {"topic": "x"},
                    "missing_inputs": [],
                },
            )
        )
        decision = client.decide("fai il workflow")
        self.assertTrue(decision.wants_run)
        self.assertEqual(decision.workflow_id, 3)
        self.assertEqual(decision.inputs, {"topic": "x"})
        self.assertIn("/api/assistant/decide", transport.calls[0]["url"])


class WorkflowTests(unittest.TestCase):
    def test_workflows_are_typed(self):
        client, _ = client_with(
            (
                200,
                [
                    {
                        "id": 1,
                        "name": "W",
                        "description": "d",
                        "inputs": [{"key": "a", "prompt": "A"}],
                        "expected_exports": ["xlsx"],
                    }
                ],
            )
        )
        cards = client.workflows()
        self.assertIsInstance(cards[0], WorkflowCard)
        self.assertEqual(cards[0].input_keys, ["a"])
        self.assertEqual(cards[0].missing_inputs({"a": 1}), [])
        self.assertEqual(cards[0].missing_inputs({"b": 1}), ["a"])


class RunTests(unittest.TestCase):
    def test_start_run_payload_and_parse(self):
        client, transport = client_with((200, {"run_id": 42, "status": "running"}))
        started = client.start_run(9, {"topic": "x"})
        self.assertEqual(started.run_id, 42)
        self.assertEqual(transport.calls[0]["body"]["workflow_id"], 9)
        self.assertEqual(transport.calls[0]["body"]["source"], "mobile")

    def test_run_detail_flags(self):
        waiting = RunDetail.from_dict(
            {"run_id": 1, "status": "waiting_human", "pending_interrupt_id": 5}
        )
        self.assertTrue(waiting.needs_approval)
        self.assertFalse(waiting.is_finished)

        done = RunDetail.from_dict(
            {"run_id": 1, "status": "completed", "result": "ok", "exports": ["a.pdf"]}
        )
        self.assertTrue(done.is_finished)
        self.assertEqual(done.result, "ok")
        self.assertEqual(done.exports, ["a.pdf"])

    def test_runs_and_cancel(self):
        client, transport = client_with(
            (200, [{"run_id": 2, "status": "completed", "preview": "x"}]),
            (200, {"run_id": 2, "status": "cancelled"}),
        )
        rows = client.runs(limit=5)
        self.assertTrue(rows[0].is_finished)
        self.assertIn("limit=5", transport.calls[0]["url"])
        self.assertEqual(client.cancel_run(2)["status"], "cancelled")


class EventTests(unittest.TestCase):
    def test_events_request_uses_cursor(self):
        client, transport = client_with(
            (
                200,
                {
                    "run_id": 5,
                    "latest_seq": 7,
                    "events": [
                        {"run_id": 5, "seq": 6, "type": "run.node.completed", "payload": {"node_id": "n1"}},
                        {"run_id": 5, "seq": 7, "type": "run.finished", "payload": {}},
                    ],
                },
            )
        )
        page = client.events(5, after_seq=5, limit=50)
        call = transport.calls[0]
        self.assertIn("/api/runs/5/events", call["url"])
        self.assertIn("after_seq=5", call["url"])
        self.assertIn("limit=50", call["url"])
        self.assertEqual(page.latest_seq, 7)
        self.assertEqual([e.seq for e in page.events], [6, 7])
        self.assertEqual(page.events[0].type, "run.node.completed")
        self.assertEqual(page.events[0].payload["node_id"], "n1")

    def test_page_without_latest_seq_uses_max(self):
        page = client_with(
            (200, {"run_id": 1, "events": [{"run_id": 1, "seq": 3, "type": "run.started"}]})
        )[0].events(1)
        self.assertEqual(page.latest_seq, 3)


class InterruptTests(unittest.TestCase):
    def test_list_and_answer(self):
        client, transport = client_with(
            (
                200,
                [
                    {
                        "id": 11,
                        "run_id": 4,
                        "node_id": "hitl1",
                        "question": "Procedo?",
                        "options": ["Yes", "No"],
                    }
                ],
            ),
            (200, {"status": "resumed", "run_id": 4, "execution": "started"}),
        )
        pending = client.interrupts(run_id=4)
        self.assertIsInstance(pending[0], Interrupt)
        self.assertTrue(pending[0].is_yes_no)

        outcome = client.answer_interrupt(11, "Yes")
        self.assertTrue(outcome.resumed)
        self.assertEqual(transport.calls[1]["body"], {"answer": "Yes", "execute": True})
        self.assertIn("/api/interrupts/11/answer", transport.calls[1]["url"])


class FileTests(unittest.TestCase):
    def test_run_files_are_typed(self):
        client, transport = client_with(
            (
                200,
                {
                    "run_id": 5,
                    "files": [
                        {
                            "name": "out.xlsx",
                            "path": "runs/5/exports/out.xlsx",
                            "size": 123,
                            "kind": "table",
                            "mime": "application/vnd.ms-excel",
                            "exists": True,
                            "accessible": True,
                        },
                        {
                            "name": "outside.pdf",
                            "path": "",
                            "kind": "pdf",
                            "accessible": False,
                            "exists": False,
                        },
                    ],
                },
            )
        )
        rows = client.run_files(5)
        self.assertEqual(rows[0].name, "out.xlsx")
        self.assertTrue(rows[0].downloadable)
        self.assertFalse(rows[1].downloadable)
        self.assertIn("/api/runs/5/files", transport.calls[0]["url"])

    def test_preview_table(self):
        client, transport = client_with(
            (
                200,
                {
                    "name": "data.csv",
                    "path": "data.csv",
                    "kind": "table",
                    "rows": [["a", "b"], ["1", "2"]],
                    "truncated": True,
                },
            )
        )
        preview = client.file_preview("data.csv")
        self.assertEqual(preview.rows[0], ["a", "b"])
        self.assertTrue(preview.truncated)
        self.assertIn("/api/files/preview", transport.calls[0]["url"])

    def test_download_returns_raw_bytes(self):
        client, transport = client_with((200, b"\x00\x01binary"))
        payload = client.download_file("runs/5/exports/a b.txt")
        self.assertEqual(payload, b"\x00\x01binary")
        call = transport.calls[0]
        self.assertEqual(call["headers"]["X-Control-Token"], "secret")
        self.assertIn("/api/files/download", call["url"])
        self.assertIn("path=", call["url"])

    def test_download_errors_are_mapped(self):
        client, _ = client_with((404, {"detail": "File not found"}))
        with self.assertRaises(ControlNotFoundError):
            client.download_file("nope.txt")

    def test_share_creates_link(self):
        client, transport = client_with(
            (
                200,
                {
                    "token": "tok",
                    "url": "http://alfredo/api/files/shared/tok",
                    "path": "a.txt",
                    "expires_at": "2026-01-01 10:00:00",
                    "ttl_seconds": 900,
                },
            )
        )
        link = client.share_file("a.txt", ttl_seconds=900)
        self.assertEqual(link.url, "http://alfredo/api/files/shared/tok")
        self.assertEqual(link.ttl_seconds, 900)
        self.assertEqual(transport.calls[0]["body"], {"path": "a.txt", "ttl_seconds": 900})


class UploadTests(unittest.TestCase):
    UPLOAD_ID = "a" * 32

    def session_payload(self, *, size=2500, chunk_size=1024, received=()):
        return {
            "upload_id": self.UPLOAD_ID,
            "path": "inbox/a.bin",
            "size": size,
            "chunk_size": chunk_size,
            "chunks": (size + chunk_size - 1) // chunk_size,
            "received": list(received),
            "expires_at": "2026-02-01 10:00:00",
        }

    def chunk_payload(self, index, chunks, received):
        return {
            "upload_id": self.UPLOAD_ID,
            "index": index,
            "received": received,
            "chunks": chunks,
            "complete": len(received) >= chunks,
        }

    def test_upload_file_sends_every_chunk_and_checksum(self):
        data = b"a" * 2500
        client, transport = client_with(
            (200, self.session_payload()),
            (200, self.chunk_payload(0, 3, [0])),
            (200, self.chunk_payload(1, 3, [0, 1])),
            (200, self.chunk_payload(2, 3, [0, 1, 2])),
            (200, {"name": "a.bin", "path": "inbox/a.bin", "size": 2500, "kind": "binary"}),
        )
        seen = []
        result = client.upload_file(
            "inbox/a.bin",
            data,
            chunk_size=1024,
            progress=lambda sent, total: seen.append((sent, total)),
        )
        self.assertEqual(result.path, "inbox/a.bin")
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "PUT", "PUT", "PUT", "POST"])
        self.assertEqual(transport.calls[0]["body"]["size"], 2500)
        lengths = [len(call["body"]) for call in transport.calls[1:4]]
        self.assertEqual(lengths, [1024, 1024, 452])
        self.assertEqual(
            transport.calls[-1]["body"]["sha256"],
            hashlib.sha256(data).hexdigest(),
        )
        self.assertEqual(seen[-1], (2500, 2500))
        self.assertEqual(
            transport.calls[1]["headers"]["X-Control-Token"], "secret"
        )
        self.assertEqual(
            transport.calls[1]["headers"]["Content-Type"], "application/octet-stream"
        )
        self.assertIn(f"/api/files/upload/{self.UPLOAD_ID}/chunks/0", transport.calls[1]["url"])

    def test_upload_file_resumes_only_missing_chunks(self):
        from mobile_client.models import UploadSession

        data = b"b" * 2500
        session = UploadSession.from_dict(self.session_payload(received=[0, 2]))
        client, transport = client_with(
            (200, self.chunk_payload(1, 3, [0, 1, 2])),
            (200, {"name": "a.bin", "path": "inbox/a.bin", "size": 2500}),
        )
        client.upload_file("inbox/a.bin", data, session=session)
        methods = [call["method"] for call in transport.calls]
        self.assertEqual(methods, ["PUT", "POST"])
        self.assertIn("/chunks/1", transport.calls[0]["url"])

    def test_failed_upload_is_aborted(self):
        data = b"c" * 100
        client, transport = client_with(
            (200, self.session_payload(size=100, chunk_size=1024)),
            (500, {"detail": "disk full"}),
            (200, {"upload_id": self.UPLOAD_ID, "aborted": True}),
        )
        with self.assertRaises(ControlServerError):
            client.upload_file("inbox/a.bin", data, chunk_size=1024)
        self.assertEqual(transport.calls[-1]["method"], "DELETE")

    def test_status_and_abort(self):
        client, transport = client_with(
            (200, self.session_payload(received=[0])),
            (200, {"upload_id": self.UPLOAD_ID, "aborted": True}),
        )
        status = client.upload_status(self.UPLOAD_ID)
        self.assertEqual(status.missing, [1, 2])
        self.assertFalse(status.is_complete)
        self.assertTrue(client.abort_upload(self.UPLOAD_ID)["aborted"])
        self.assertIn(f"/api/files/upload/{self.UPLOAD_ID}", transport.calls[0]["url"])

    def test_chunk_response_is_exposed(self):
        client, transport = client_with((200, self.chunk_payload(1, 3, [0, 1])))
        payload = client.upload_chunk(self.UPLOAD_ID, 1, b"x" * 1024)
        self.assertEqual(payload["received"], [0, 1])
        self.assertIn("/chunks/1", transport.calls[0]["url"])


class SystemStatusTests(unittest.TestCase):
    def test_parses_gpu_queue_and_metrics(self):
        client, transport = client_with(
            (
                200,
                {
                    "gpu": {
                        "available": True,
                        "name": "RTX 3060",
                        "total_mb": 12288,
                        "used_mb": 1000,
                        "free_mb": 11288,
                        "reserve_mb": 500,
                    },
                    "profiles": {
                        "fast": {"name": "fast", "model": "m", "fits": True, "headroom_mb": 4788}
                    },
                    "queue": {
                        "current": {"profile": "fast", "run_id": 7},
                        "waiting": [{"position": 1, "profile": "deep", "run_id": 8}],
                        "length": 1,
                    },
                    "metrics": {
                        "served": 3,
                        "evictions": 1,
                        "first_token": {"fast": {"count": 2, "avg_s": 0.4}},
                    },
                },
            )
        )
        status = client.system_status()
        self.assertTrue(status.gpu_available)
        self.assertEqual(status.gpu_free_mb, 11288)
        self.assertEqual(status.waiting_count, 1)
        self.assertEqual(status.current_run_id, 7)
        self.assertEqual(status.raw["metrics"]["served"], 3)
        self.assertIn("/api/system/status", transport.calls[0]["url"])
        self.assertEqual(transport.calls[0]["headers"]["X-Control-Token"], "secret")


class WaitTests(unittest.TestCase):
    def _clocked_client(self, *responses):
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        transport = FakeTransport(*responses)
        client = ControlClient(
            "http://x", token="t", transport=transport, sleep=sleep, clock=clock
        )
        return client, transport, now

    def test_waits_until_completed(self):
        client, transport, _ = self._clocked_client(
            (200, {"run_id": 1, "status": "running"}),
            (200, {"run_id": 1, "status": "running"}),
            (200, {"run_id": 1, "status": "completed", "result": "fatto"}),
        )
        seen = []
        final = client.wait_for_run(1, interval=2, max_interval=3, on_update=seen.append)
        self.assertEqual(final.status, "completed")
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(transport.calls), 3)

    def test_stops_on_interrupt(self):
        client, _, _ = self._clocked_client(
            (200, {"run_id": 1, "status": "waiting_human", "pending_interrupt_id": 2})
        )
        detail = client.wait_for_run(1)
        self.assertTrue(detail.needs_approval)

    def test_timeout_raises(self):
        client, _, _ = self._clocked_client(
            (200, {"run_id": 1, "status": "running"}),
            (200, {"run_id": 1, "status": "running"}),
            (200, {"run_id": 1, "status": "running"}),
            (200, {"run_id": 1, "status": "running"}),
        )
        with self.assertRaises(ControlError):
            client.wait_for_run(1, timeout=1.0, interval=1.0, max_interval=1.0)


class ErrorMappingTests(unittest.TestCase):
    def test_status_codes_map_to_exceptions(self):
        cases = [
            (401, ControlAuthError),
            (404, ControlNotFoundError),
            (409, ControlConflictError),
            (422, ControlValidationError),
            (500, ControlServerError),
        ]
        for status, exc in cases:
            client, _ = client_with((status, {"detail": "boom"}))
            with self.subTest(status=status):
                with self.assertRaises(exc):
                    client.workflows()

    def test_network_failure_is_wrapped(self):
        def broken(method, url, headers, body, timeout):
            raise ControlConnectionError("down")

        client = ControlClient("http://x", token="t", transport=broken)
        with self.assertRaises(ControlConnectionError):
            client.workflows()


class ConfigTests(unittest.TestCase):
    def test_env_fallbacks(self):
        transport = FakeTransport((200, {"status": "ok"}))
        client = ControlClient(
            transport=transport,
            env={"ALFREDO_CONTROL_URL": "http://10.0.0.2:9000/", "ALFREDO_CONTROL_TOKEN": "envtok"},
        )
        client.health()
        call = transport.calls[0]
        self.assertEqual(call["url"], "http://10.0.0.2:9000/api/health")
        self.assertEqual(call["headers"]["X-Control-Token"], "envtok")

    def test_no_token_means_no_header(self):
        transport = FakeTransport((200, {}))
        client = ControlClient("http://x", token=None, transport=transport, env={})
        client.health()
        self.assertNotIn("X-Control-Token", transport.calls[0]["headers"])


if __name__ == "__main__":
    unittest.main()
