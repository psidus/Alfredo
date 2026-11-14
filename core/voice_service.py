"""
core/voice_service.py

Free, low-overhead Speech-to-Text (STT) and Text-to-Speech (TTS) service for Alfredo.

Design principles:
- Runs STT (`faster-whisper` int8 on CPU by default) and TTS (`edge-tts` / `piper-tts`)
  without stealing VRAM from Ollama or LM Studio / Bionic managed by `GPU_GATE`.
- Decodes browser audio (`audio/webm`, `audio/mp4`, `audio/ogg`, `audio/wav`) directly
  via PyAV (bundled with `faster-whisper`) — no external `ffmpeg` binary required.
- Also supports any OpenAI-compatible `/v1/audio/*` server (Bionic, Speaches, Groq).
- Completely decoupled from Streamlit, Telegram, and FastAPI transports.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.util
import io
import logging
import os
import re
import subprocess
import tempfile
import threading
import wave
from typing import Any, Dict, Optional, Tuple

import requests

from core.api_verifier import openai_compatible_v1_base

logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_VOICE_DIR = os.path.join(ROOT_DIR, "storage", "voice_models")
WHISPER_CACHE_DIR = os.path.join(STORAGE_VOICE_DIR, "whisper")
PIPER_CACHE_DIR = os.path.join(STORAGE_VOICE_DIR, "piper")

MAX_TTS_CHARS = 4000

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo"]

EDGE_VOICES_IT = [
    "it-IT-DiegoNeural",
    "it-IT-ElsaNeural",
    "it-IT-IsabellaNeural",
    "it-IT-GiuseppeNeural",
]
EDGE_VOICES_EN = [
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-US-AriaNeural",
    "en-GB-RyanNeural",
]

PIPER_VOICE_CATALOG = {
    "it_IT-paola-medium": "it/it_IT/paola/medium/it_IT-paola-medium",
    "it_IT-riccardo-x_low": "it/it_IT/riccardo/x_low/it_IT-riccardo-x_low",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
}

PIPER_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

_MIME_TO_EXT = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}

_whisper_lock = threading.Lock()
_whisper_cache: Dict[Tuple[str, str, str], Any] = {}

_piper_lock = threading.Lock()
_piper_cache: Dict[str, Any] = {}


def _env_str(name: str, default: str) -> str:
    val = (os.getenv(name) or "").strip().strip('"').strip("'")
    return val or default


def _is_installed(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _stt_config() -> Dict[str, str]:
    return {
        "provider": _env_str("ALFREDO_STT_PROVIDER", "faster_whisper").lower(),
        "model": _env_str("ALFREDO_STT_MODEL", "small"),
        "device": _env_str("ALFREDO_STT_DEVICE", "cpu").lower(),
        "compute_type": _env_str("ALFREDO_STT_COMPUTE_TYPE", "int8").lower(),
        "language": _env_str("ALFREDO_STT_LANGUAGE", "auto").lower(),
        "api_base": _env_str(
            "ALFREDO_STT_API_BASE",
            _env_str("LMSTUDIO_API_BASE", "http://127.0.0.1:1234/v1"),
        ),
        "api_key": _env_str("ALFREDO_STT_API_KEY", _env_str("LMSTUDIO_API_KEY", "lm-studio")),
    }


def _tts_config() -> Dict[str, str]:
    return {
        "provider": _env_str("ALFREDO_TTS_PROVIDER", "edge_tts").lower(),
        "voice_it": _env_str("ALFREDO_TTS_VOICE_IT", EDGE_VOICES_IT[0]),
        "voice_en": _env_str("ALFREDO_TTS_VOICE_EN", EDGE_VOICES_EN[0]),
        "piper_model": _env_str("ALFREDO_TTS_PIPER_MODEL", "it_IT-paola-medium"),
        "api_base": _env_str(
            "ALFREDO_TTS_API_BASE",
            _env_str("LMSTUDIO_API_BASE", "http://127.0.0.1:1234/v1"),
        ),
        "api_key": _env_str("ALFREDO_TTS_API_KEY", _env_str("LMSTUDIO_API_KEY", "lm-studio")),
    }


# ---------------------------------------------------------------------------
# Text normalization for TTS
# ---------------------------------------------------------------------------


def clean_text_for_tts(text: str) -> str:
    """Strip tool calls, code fences, Markdown markup and raw URLs for natural speech."""
    if not text:
        return ""

    cleaned = str(text)
    # Remove assistant tool_call blocks and fenced code blocks
    cleaned = re.sub(r"```(?:tool_call|json)?\s*\{.*?\}\s*```", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<tool_call>.*?</tool_call>", " ", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)

    # Convert Markdown links [label](url) -> label, then drop bare URLs
    cleaned = re.sub(r"\[([^\]]+)\]\((?:https?://|file://)[^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)

    # Remove inline backticks, headings, blockquotes, emphasis markers and table pipes
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*>\s*", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*[-*•]\s+", "", cleaned)
    cleaned = re.sub(r"(\*\*|__|\*|~~)", "", cleaned)
    cleaned = cleaned.replace("|", ", ")

    # Normalize whitespace while keeping sentence breaks natural
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) > MAX_TTS_CHARS:
        slice_text = cleaned[:MAX_TTS_CHARS]
        last_dot = max(slice_text.rfind("."), slice_text.rfind("!"), slice_text.rfind("?"))
        if last_dot > MAX_TTS_CHARS // 2:
            cleaned = slice_text[: last_dot + 1]
        else:
            cleaned = slice_text.rsplit(" ", 1)[0] + "..."

    return cleaned.strip()


# ---------------------------------------------------------------------------
# STT (Audio -> Text)
# ---------------------------------------------------------------------------


def _is_whisper_cached(model_name: str) -> bool:
    if not os.path.isdir(WHISPER_CACHE_DIR):
        return False
    slug = model_name.lower().replace("/", "--")
    for entry in os.listdir(WHISPER_CACHE_DIR):
        if slug in entry.lower():
            return True
    return False


def _get_whisper_model(model_name: str, device: str, compute_type: str) -> Any:
    key = (model_name, device, compute_type)
    with _whisper_lock:
        model = _whisper_cache.get(key)
        if model is None:
            from faster_whisper import WhisperModel

            os.makedirs(WHISPER_CACHE_DIR, exist_ok=True)
            logger.info(
                "Loading faster-whisper model '%s' (device=%s, compute=%s)",
                model_name,
                device,
                compute_type,
            )
            model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                download_root=WHISPER_CACHE_DIR,
            )
            _whisper_cache[key] = model
        return model


def _infer_audio_ext(filename: str, mime_type: str) -> str:
    _, ext = os.path.splitext((filename or "").strip())
    if ext:
        return ext.lower()
    base_mime = (mime_type or "").split(";")[0].strip().lower()
    return _MIME_TO_EXT.get(base_mime, ".webm")


def _decode_source_with_av(source: Any, sampling_rate: int = 16000) -> Any:
    """Decode an audio file/stream to mono float32 PCM using PyAV.

    Decoded directly instead of calling faster_whisper.audio.decode_audio because
    PyAV >= 16 removed the metadata_errors='ignore' keyword used by faster-whisper 1.2.1.
    """
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(
        format="s16",
        layout="mono",
        rate=sampling_rate,
    )
    chunks = []
    try:
        with av.open(source, mode="r") as container:
            if not container.streams.audio:
                raise ValueError("Audio stream not found in media container.")
            for frame in container.decode(audio=0):
                frame.pts = None
                for r_frame in resampler.resample(frame) or ():
                    chunks.append(np.asarray(r_frame.to_ndarray(), dtype=np.int16).reshape(-1))
            for r_frame in resampler.resample(None) or ():
                chunks.append(np.asarray(r_frame.to_ndarray(), dtype=np.int16).reshape(-1))
    finally:
        gc.collect()

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    audio = np.concatenate(chunks)
    return audio.astype(np.float32) / 32768.0


def _decode_audio_to_float32(
    audio_bytes: bytes,
    filename: str = "",
    mime_type: str = "",
    sampling_rate: int = 16000,
) -> Any:
    try:
        return _decode_source_with_av(io.BytesIO(audio_bytes), sampling_rate=sampling_rate)
    except Exception as mem_exc:
        # Some fragmented MP4/WebM browser blobs require a seekable real file for demuxing
        ext = _infer_audio_ext(filename, mime_type)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            return _decode_source_with_av(tmp_path, sampling_rate=sampling_rate)
        except Exception as file_exc:
            raise RuntimeError(f"Audio decode failed: {file_exc}") from mem_exc
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def _transcribe_faster_whisper(
    audio_bytes: bytes,
    filename: str,
    mime_type: str,
    language: Optional[str],
    cfg: Dict[str, str],
) -> Dict[str, Any]:
    if not _is_installed("faster_whisper"):
        raise RuntimeError("faster-whisper is not installed. Run: pip install faster-whisper")

    audio_np = _decode_audio_to_float32(audio_bytes, filename, mime_type, sampling_rate=16000)
    lang_arg = None if not language or language == "auto" else language[:2].lower()
    if audio_np.size == 0:
        return {
            "text": "",
            "language": lang_arg or "",
            "duration_s": 0.0,
            "provider": "faster_whisper",
            "model": cfg["model"],
        }

    model = _get_whisper_model(cfg["model"], cfg["device"], cfg["compute_type"])
    segments, info = model.transcribe(
        audio_np,
        language=lang_arg,
        vad_filter=True,
        beam_size=5,
    )
    text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip()).strip()

    return {
        "text": text,
        "language": getattr(info, "language", "") or (lang_arg or ""),
        "duration_s": round(float(getattr(info, "duration", 0.0) or 0.0), 2),
        "provider": "faster_whisper",
        "model": cfg["model"],
    }


def _transcribe_openai_compatible(
    audio_bytes: bytes,
    filename: str,
    mime_type: str,
    language: Optional[str],
    cfg: Dict[str, str],
) -> Dict[str, Any]:
    provider = cfg["provider"]
    if provider == "groq":
        api_key = _env_str("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured in .env")
        base_v1 = "https://api.groq.com/openai/v1"
        model_name = cfg["model"] if "whisper" in cfg["model"].lower() else "whisper-large-v3-turbo"
    else:
        base_v1 = openai_compatible_v1_base(cfg["api_base"])
        api_key = cfg["api_key"]
        model_name = cfg["model"]

    url = f"{base_v1}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    ext = _infer_audio_ext(filename, mime_type)
    safe_name = filename if "." in (filename or "") else f"voice{ext}"
    clean_mime = (mime_type or "audio/webm").split(";")[0].strip() or "audio/webm"

    form_data: Dict[str, str] = {"model": model_name, "response_format": "verbose_json"}
    if language and language != "auto":
        form_data["language"] = language[:2].lower()

    resp = requests.post(
        url,
        headers=headers,
        files={"file": (safe_name, audio_bytes, clean_mime)},
        data=form_data,
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"STT server error ({resp.status_code}): {resp.text[:300]}")

    payload = resp.json()
    return {
        "text": str(payload.get("text") or "").strip(),
        "language": str(payload.get("language") or (language or "")),
        "duration_s": round(float(payload.get("duration") or 0.0), 2),
        "provider": provider,
        "model": model_name,
    }


def transcribe_audio_bytes(
    audio_bytes: bytes,
    mime_type: str = "audio/webm",
    filename: str = "voice.webm",
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Transcribe raw audio bytes into text using the configured STT provider."""
    if not audio_bytes:
        raise ValueError("Empty audio payload")

    cfg = _stt_config()
    eff_lang = (language or cfg["language"] or "auto").strip().lower()

    if cfg["provider"] in ("openai_compatible", "groq"):
        return _transcribe_openai_compatible(audio_bytes, filename, mime_type, eff_lang, cfg)
    return _transcribe_faster_whisper(audio_bytes, filename, mime_type, eff_lang, cfg)


def preload_stt_model() -> Dict[str, Any]:
    """Download/warm up the configured local STT model and report readiness."""
    cfg = _stt_config()
    if cfg["provider"] in ("openai_compatible", "groq"):
        return {
            "success": True,
            "provider": cfg["provider"],
            "model": cfg["model"],
            "message": f"Configured remote/OpenAI-compatible STT ({cfg['provider']}).",
        }
    if not _is_installed("faster_whisper"):
        return {
            "success": False,
            "error": "faster-whisper is not installed. Run: pip install faster-whisper",
        }
    try:
        _get_whisper_model(cfg["model"], cfg["device"], cfg["compute_type"])
        return {
            "success": True,
            "provider": "faster_whisper",
            "model": cfg["model"],
            "message": f"Faster-Whisper '{cfg['model']}' ({cfg['device']}/{cfg['compute_type']}) ready in {WHISPER_CACHE_DIR}.",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# TTS (Text -> Audio)
# ---------------------------------------------------------------------------


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine safely from any sync/worker thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: Dict[str, Any] = {}
    err_box: Dict[str, BaseException] = {}

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result_box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            err_box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in err_box:
        raise err_box["error"]
    return result_box.get("value")


def _speed_to_edge_rate(speed: float) -> str:
    clamped = max(0.5, min(2.0, float(speed or 1.0)))
    pct = round((clamped - 1.0) * 100)
    return f"+{pct}%" if pct >= 0 else f"{pct}%"


def _synthesize_edge_tts(text: str, voice: str, speed: float) -> Tuple[bytes, str]:
    if not _is_installed("edge_tts"):
        raise RuntimeError("edge-tts is not installed")
    import edge_tts

    rate = _speed_to_edge_rate(speed)

    async def _collect() -> bytes:
        comm = edge_tts.Communicate(text, voice=voice, rate=rate)
        buf = io.BytesIO()
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    audio = _run_coro_sync(_collect())
    if not audio:
        raise RuntimeError("edge-tts returned an empty audio stream")
    return audio, "audio/mpeg"


def _ensure_piper_model(model_name: str) -> Tuple[str, str]:
    os.makedirs(PIPER_CACHE_DIR, exist_ok=True)
    onnx_path = os.path.join(PIPER_CACHE_DIR, f"{model_name}.onnx")
    json_path = os.path.join(PIPER_CACHE_DIR, f"{model_name}.onnx.json")

    if os.path.isfile(onnx_path) and os.path.isfile(json_path):
        return onnx_path, json_path

    rel_prefix = PIPER_VOICE_CATALOG.get(model_name)
    if not rel_prefix:
        # Infer standard rhasspy/piper-voices layout: <lang>/<locale>/<speaker>/<quality>/<name>
        parts = model_name.split("-")
        if len(parts) < 3:
            raise RuntimeError(f"Unknown Piper voice '{model_name}'")
        locale, speaker, quality = parts[0], parts[1], "-".join(parts[2:])
        lang_code = locale.split("_")[0]
        rel_prefix = f"{lang_code}/{locale}/{speaker}/{quality}/{model_name}"

    for target_path, suffix in ((onnx_path, ".onnx"), (json_path, ".onnx.json")):
        if os.path.isfile(target_path):
            continue
        url = f"{PIPER_HF_BASE}/{rel_prefix}{suffix}"
        logger.info("Downloading Piper voice asset: %s", url)
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        tmp = target_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(resp.content)
        os.replace(tmp, target_path)

    return onnx_path, json_path


def _get_piper_voice(model_name: str) -> Any:
    with _piper_lock:
        voice = _piper_cache.get(model_name)
        if voice is None:
            from piper import PiperVoice

            onnx_path, json_path = _ensure_piper_model(model_name)
            voice = PiperVoice.load(onnx_path, config_path=json_path, use_cuda=False)
            _piper_cache[model_name] = voice
        return voice


def _synthesize_piper(text: str, lang: str, speed: float, cfg: Dict[str, str]) -> Tuple[bytes, str]:
    if not _is_installed("piper"):
        raise RuntimeError("piper-tts is not installed")
    from piper import SynthesisConfig

    default_model = cfg["piper_model"] if lang.startswith("it") else "en_US-lessac-medium"
    voice = _get_piper_voice(default_model)
    clamped = max(0.5, min(2.0, float(speed or 1.0)))
    syn_cfg = SynthesisConfig(length_scale=round(1.0 / clamped, 3))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_cfg)
    data = buf.getvalue()
    if not data:
        raise RuntimeError("Piper returned empty WAV output")
    return data, "audio/wav"


def _synthesize_windows_sapi(text: str, speed: float) -> Tuple[bytes, str]:
    """Last-resort 100% offline Windows fallback via System.Speech."""
    if os.name != "nt":
        raise RuntimeError("No offline TTS backend available on this platform")

    rate = max(-10, min(10, int(round((float(speed or 1.0) - 1.0) * 6))))
    tmp_wav = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tmp_wav = tf.name
        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {rate}; "
            "$s.SetOutputToWaveFile($env:ALFREDO_TTS_TMP); "
            "$s.Speak($env:ALFREDO_TTS_TEXT); "
            "$s.Dispose()"
        )
        env = {**os.environ, "ALFREDO_TTS_TMP": tmp_wav, "ALFREDO_TTS_TEXT": text}
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or not os.path.isfile(tmp_wav):
            raise RuntimeError(completed.stderr or "Windows SAPI TTS failed")
        with open(tmp_wav, "rb") as fh:
            data = fh.read()
        if not data:
            raise RuntimeError("Windows SAPI returned empty WAV")
        return data, "audio/wav"
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass


def _synthesize_offline(text: str, lang: str, speed: float, cfg: Dict[str, str]) -> Tuple[bytes, str]:
    try:
        return _synthesize_piper(text, lang, speed, cfg)
    except Exception as piper_exc:
        logger.warning("Piper offline TTS unavailable (%s); trying OS fallback.", piper_exc)
        return _synthesize_windows_sapi(text, speed)


def _synthesize_openai_compatible(
    text: str, voice: str, speed: float, cfg: Dict[str, str]
) -> Tuple[bytes, str]:
    base_v1 = openai_compatible_v1_base(cfg["api_base"])
    url = f"{base_v1}/audio/speech"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    resp = requests.post(
        url,
        headers=headers,
        json={
            "model": _env_str("ALFREDO_TTS_MODEL", "tts-1"),
            "input": text,
            "voice": voice,
            "speed": max(0.5, min(2.0, float(speed or 1.0))),
            "response_format": "mp3",
        },
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"TTS server error ({resp.status_code}): {resp.text[:300]}")
    mime = resp.headers.get("Content-Type", "audio/mpeg").split(";")[0].strip() or "audio/mpeg"
    return resp.content, mime


def synthesize_speech(
    text: str,
    lang: str = "it",
    voice: Optional[str] = None,
    speed: float = 1.0,
) -> Tuple[bytes, str]:
    """Synthesize spoken audio bytes `(bytes, media_type)` from text."""
    cleaned = clean_text_for_tts(text)
    if not cleaned:
        raise ValueError("No speakable text after cleaning")

    cfg = _tts_config()
    eff_lang = (lang or "it").strip().lower()
    chosen_voice = (voice or "").strip() or (
        cfg["voice_it"] if eff_lang.startswith("it") else cfg["voice_en"]
    )
    provider = cfg["provider"]

    if provider == "piper_local":
        return _synthesize_offline(cleaned, eff_lang, speed, cfg)
    if provider == "openai_compatible":
        return _synthesize_openai_compatible(cleaned, chosen_voice, speed, cfg)

    # Default: edge_tts with automatic offline fallback if network is unreachable
    try:
        return _synthesize_edge_tts(cleaned, chosen_voice, speed)
    except Exception as exc:
        logger.warning("edge-tts failed (%s); falling back to offline TTS.", exc)
        return _synthesize_offline(cleaned, eff_lang, speed, cfg)


# ---------------------------------------------------------------------------
# Diagnostics / Status
# ---------------------------------------------------------------------------


def get_voice_status() -> Dict[str, Any]:
    """Report current STT/TTS availability, active models, and selectable voices."""
    stt = _stt_config()
    tts = _tts_config()

    fw_ok = _is_installed("faster_whisper")
    edge_ok = _is_installed("edge_tts")
    piper_ok = _is_installed("piper")

    stt_available = (
        fw_ok
        if stt["provider"] == "faster_whisper"
        else bool(stt["api_base"] or _env_str("GROQ_API_KEY", ""))
    )
    tts_available = edge_ok or piper_ok or (tts["provider"] == "openai_compatible")

    return {
        "stt_provider": stt["provider"],
        "stt_model": stt["model"],
        "stt_device": stt["device"],
        "stt_language": stt["language"],
        "stt_available": stt_available,
        "stt_model_cached": _is_whisper_cached(stt["model"]),
        "tts_provider": tts["provider"],
        "tts_voice_it": tts["voice_it"],
        "tts_voice_en": tts["voice_en"],
        "tts_piper_model": tts["piper_model"],
        "tts_available": tts_available,
        "piper_available": piper_ok,
        "available_stt_models": list(WHISPER_MODELS),
        "available_tts_voices_it": list(EDGE_VOICES_IT),
        "available_tts_voices_en": list(EDGE_VOICES_EN),
    }
