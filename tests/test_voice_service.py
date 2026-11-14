"""Unit and API contract tests for Alfredo's Voice (STT & TTS) integration."""

from __future__ import annotations

import io
import math
import os
import struct
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from core.api_server import app
from core.voice_service import (
    _decode_audio_to_float32,
    clean_text_for_tts,
    get_voice_status,
    synthesize_speech,
    transcribe_audio_bytes,
)

ROOT = Path(__file__).resolve().parents[1]


class CleanTextForTtsTests(unittest.TestCase):
    def test_strips_tool_calls_code_and_markdown(self):
        raw = (
            "## Risultato\n\n"
            "Ho **completato** l'analisi! Vedi [report](https://example.com/r) "
            "o https://example.com/raw per dettagli.\n"
            "```tool_call\n{\"tool\": \"list_files\", \"args\": {}}\n```\n"
            "```python\nprint('hello')\n```\n"
            "Controlla il file `report.md`."
        )
        cleaned = clean_text_for_tts(raw)
        self.assertNotIn("tool_call", cleaned)
        self.assertNotIn("print(", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("##", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertIn("Risultato", cleaned)
        self.assertIn("Ho completato l'analisi! Vedi report", cleaned)
        self.assertIn("Controlla il file report.md.", cleaned)

    def test_truncates_long_text_cleanly(self):
        long_text = ("Questa è una frase di test abbastanza lunga. " * 200).strip()
        cleaned = clean_text_for_tts(long_text)
        self.assertLessEqual(len(cleaned), 4000)
        self.assertTrue(cleaned.endswith(".") or cleaned.endswith("..."))


class VoiceServiceLogicTests(unittest.TestCase):
    def test_transcribe_rejects_empty_audio(self):
        with self.assertRaises(ValueError):
            transcribe_audio_bytes(b"")

    def test_status_reports_installed_engines(self):
        status = get_voice_status()
        self.assertTrue(status["stt_available"])
        self.assertTrue(status["tts_available"])
        self.assertIn("small", status["available_stt_models"])
        self.assertIn("it-IT-DiegoNeural", status["available_tts_voices_it"])

    def test_synthesize_falls_back_to_offline_when_edge_fails(self):
        with (
            patch.dict(os.environ, {"ALFREDO_TTS_PROVIDER": "edge_tts"}, clear=False),
            patch("core.voice_service._synthesize_edge_tts", side_effect=RuntimeError("offline")),
            patch("core.voice_service._synthesize_offline", return_value=(b"RIFFdemo", "audio/wav")) as mock_off,
        ):
            data, mime = synthesize_speech("Ciao Alfredo", lang="it", speed=1.25)
        self.assertEqual(data, b"RIFFdemo")
        self.assertEqual(mime, "audio/wav")
        mock_off.assert_called_once()

    @staticmethod
    def _make_stereo_wav_bytes(duration_s: float = 0.25, sample_rate: int = 44100) -> bytes:
        buf = io.BytesIO()
        num_frames = int(duration_s * sample_rate)
        pcm = bytearray()
        for i in range(num_frames):
            sample = int(12000 * math.sin(2.0 * math.pi * 440.0 * i / sample_rate))
            pcm.extend(struct.pack("<hh", sample, sample))
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(pcm))
        return buf.getvalue()

    def test_decode_audio_to_float32_resamples_stereo_wav(self):
        wav_bytes = self._make_stereo_wav_bytes(duration_s=0.25, sample_rate=44100)
        audio = _decode_audio_to_float32(wav_bytes, filename="sample.wav", mime_type="audio/wav")
        self.assertIsInstance(audio, np.ndarray)
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.ndim, 1)
        self.assertGreaterEqual(audio.size, 3500)
        self.assertLessEqual(audio.size, 4500)
        self.assertGreater(float(np.max(np.abs(audio))), 0.05)

    def test_transcribe_faster_whisper_passes_float32_ndarray(self):
        wav_bytes = self._make_stereo_wav_bytes(duration_s=0.25, sample_rate=44100)
        captured: dict[str, object] = {}

        class _FakeWhisper:
            def transcribe(self, audio, **kwargs):
                captured["audio"] = audio
                captured["kwargs"] = kwargs
                return [SimpleNamespace(text=" ciao alfredo ")], SimpleNamespace(language="it", duration=0.25)

        with (
            patch.dict(os.environ, {"ALFREDO_STT_PROVIDER": "faster_whisper"}, clear=False),
            patch("core.voice_service._get_whisper_model", return_value=_FakeWhisper()),
        ):
            res = transcribe_audio_bytes(wav_bytes, filename="note.wav", mime_type="audio/wav", language="it")

        self.assertEqual(res["text"], "ciao alfredo")
        self.assertEqual(res["language"], "it")
        self.assertEqual(res["provider"], "faster_whisper")
        self.assertIsInstance(captured["audio"], np.ndarray)
        self.assertEqual(captured["audio"].dtype, np.float32)


class VoiceApiEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_voice_status_endpoint(self):
        with patch.dict(os.environ, {"ALFREDO_CONTROL_TOKEN": ""}, clear=False):
            resp = self.client.get("/api/voice/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("stt_provider", body)
        self.assertIn("tts_provider", body)
        self.assertIn("available_stt_models", body)

    def test_voice_transcribe_rejects_empty_body(self):
        with patch.dict(os.environ, {"ALFREDO_CONTROL_TOKEN": ""}, clear=False):
            resp = self.client.post("/api/voice/transcribe", content=b"")
        self.assertEqual(resp.status_code, 400)

    def test_voice_transcribe_raw_audio_forwards_metadata(self):
        mocked = {
            "text": "Avvia il report termico",
            "language": "it",
            "duration_s": 2.4,
            "provider": "faster_whisper",
            "model": "small",
        }
        with (
            patch.dict(os.environ, {"ALFREDO_CONTROL_TOKEN": ""}, clear=False),
            patch("core.voice_service.transcribe_audio_bytes", return_value=mocked) as mock_stt,
        ):
            resp = self.client.post(
                "/api/voice/transcribe?lang=it",
                content=b"fake-webm-bytes",
                headers={
                    "Content-Type": "audio/webm;codecs=opus",
                    "X-Audio-Filename": "note.webm",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["text"], "Avvia il report termico")
        mock_stt.assert_called_once_with(
            b"fake-webm-bytes",
            mime_type="audio/webm;codecs=opus",
            filename="note.webm",
            language="it",
        )

    def test_voice_transcribe_multipart_upload(self):
        mocked = {
            "text": "Approva e continua",
            "language": "it",
            "duration_s": 1.2,
            "provider": "faster_whisper",
            "model": "small",
        }
        with (
            patch.dict(os.environ, {"ALFREDO_CONTROL_TOKEN": ""}, clear=False),
            patch("core.voice_service.transcribe_audio_bytes", return_value=mocked),
        ):
            resp = self.client.post(
                "/api/voice/transcribe",
                files={"file": ("clip.m4a", b"fake-mp4-bytes", "audio/mp4")},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["text"], "Approva e continua")

    def test_voice_synthesize_returns_audio_stream(self):
        with (
            patch.dict(os.environ, {"ALFREDO_CONTROL_TOKEN": ""}, clear=False),
            patch(
                "core.voice_service.synthesize_speech",
                return_value=(b"ID3-fake-mp3", "audio/mpeg"),
            ) as mock_tts,
        ):
            resp = self.client.post(
                "/api/voice/synthesize",
                json={"text": "Ciao, tutto pronto.", "lang": "it", "speed": 1.25},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ID3-fake-mp3")
        self.assertTrue(resp.headers["content-type"].startswith("audio/mpeg"))
        mock_tts.assert_called_once_with("Ciao, tutto pronto.", lang="it", voice=None, speed=1.25)


class VoicePwaWiringTests(unittest.TestCase):
    def test_pwa_wires_voice_controls_into_chat_and_approvals(self):
        api_ts = (ROOT / "mobile_web" / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("/api/voice/transcribe", api_ts)
        self.assertIn("/api/voice/synthesize", api_ts)
        self.assertIn("/api/voice/status", api_ts)

        chat_tsx = (ROOT / "mobile_web" / "src" / "screens" / "Chat.tsx").read_text(encoding="utf-8")
        self.assertIn("VoiceRecorderButton", chat_tsx)
        self.assertIn("VoicePlayerButton", chat_tsx)

        approvals_tsx = (ROOT / "mobile_web" / "src" / "screens" / "Approvals.tsx").read_text(encoding="utf-8")
        self.assertIn("VoiceRecorderButton", approvals_tsx)

        run_detail_tsx = (ROOT / "mobile_web" / "src" / "screens" / "RunDetail.tsx").read_text(encoding="utf-8")
        self.assertIn("VoiceRecorderButton", run_detail_tsx)


if __name__ == "__main__":
    unittest.main()
