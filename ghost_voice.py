"""
Ghost Voice — Voice Wake + Talk Mode (always-on speech interface).

Voice Wake + Talk Mode:
  - Voice Wake:  always-on listening for configurable wake words, then capture
                 the command, transcribe, process through Ghost, speak response.
  - Talk Mode:   continuous conversation — listen → transcribe → process → speak → repeat.
                 No wake word needed; every utterance goes to Ghost.

Architecture:
  VoiceEngine (singleton coordinator)
  ├── background mic thread  (sounddevice InputStream)
  ├── energy-based VAD       (numpy RMS)
  ├── STT                    (OpenAI Whisper API, Groq Whisper, or vosk offline)
  ├── Ghost integration      (POST to local /api/chat/send)
  └── TTS + playback         (ghost_tts → platform audio player)

Dependencies (pip install):
  sounddevice  numpy  soundfile      — mic capture, audio buffers, WAV I/O
Optional:
  vosk                                — offline STT (no API cost for wake word detection)
"""

import json
import logging
import os
import platform
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("ghost.voice")

GHOST_HOME = Path.home() / ".ghost"
VOICE_DIR = GHOST_HOME / "voice"
VOICE_DIR.mkdir(parents=True, exist_ok=True)

# ── State machine ────────────────────────────────────────────────────
STATE_IDLE = "idle"
STATE_WAKE_LISTENING = "wake_listening"
STATE_TALK_LISTENING = "talk_listening"
STATE_CAPTURING = "capturing"
STATE_PROCESSING = "processing"
STATE_SPEAKING = "speaking"

# ── Defaults ─────────────────────────────────────────────────────────
DEFAULT_WAKE_WORDS = ["quinely", "hey quinely"]
DEFAULT_SILENCE_THRESHOLD = 0.02
DEFAULT_SILENCE_DURATION = 2.0
DEFAULT_CAPTURE_TIMEOUT = 30.0
DEFAULT_POST_SPEAK_COOLDOWN = 0.8
SAMPLE_RATE = 16000
CHANNELS = 1

VOICE_HINT = (
    "[VOICE MODE — The user is speaking via microphone. "
    "Respond concisely in a natural, conversational spoken tone. "
    "Keep responses to 1-3 sentences unless more detail is specifically requested. "
    "Avoid markdown formatting, code blocks, or bullet points — speak naturally.]\n\n"
)


# ── Dependency checks ────────────────────────────────────────────────

def _check_audio_deps() -> list[str]:
    """Return list of missing audio dependencies."""
    missing = []
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        missing.append("sounddevice")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import soundfile  # noqa: F401
    except ImportError:
        missing.append("soundfile")
    return missing


# ── Audio playback ───────────────────────────────────────────────────

def _play_audio_file(filepath: str) -> bool:
    """Play an audio file using platform-appropriate players (no system deps required)."""
    filepath = str(filepath)
    system = platform.system()

    if system == "Darwin":
        players = [["afplay", filepath]]
    elif system == "Windows":
        players = [
            ["powershell", "-c",
             f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'],
        ]
    else:
        players = [
            ["mpv", "--no-video", "--really-quiet", filepath],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", filepath],
            ["paplay", filepath],
            ["aplay", filepath],
        ]

    for cmd in players:
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            continue

    # Python-only fallback using sounddevice + soundfile (WAV/FLAC/OGG only)
    try:
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
        data, sr = sf.read(filepath, dtype="float32")
        sd.play(data, sr)
        sd.wait()
        return True
    except Exception:
        pass

    log.warning("No audio player available for %s", filepath)
    return False


# ── Speech-to-text providers ─────────────────────────────────────────

def _transcribe_moonshine(audio_path: str) -> str:
    """Transcribe audio using Moonshine on-device STT (fast, offline, free)."""
    import numpy as np
    import soundfile as sf
    import moonshine_onnx

    audio, sr = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    result = moonshine_onnx.transcribe(audio, model="moonshine/tiny")
    if isinstance(result, list):
        return result[0].strip() if result else ""
    return str(result).strip()


def _transcribe_whisper_api(audio_path: str, api_key: str,
                            base_url: str = "https://api.openai.com/v1") -> str:
    """Transcribe audio via OpenAI-compatible Whisper endpoint."""
    import requests
    with open(audio_path, "rb") as f:
        resp = requests.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, f, "audio/wav")},
            data={"model": "whisper-1"},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def _transcribe_openrouter(audio_path: str, api_key: str) -> str:
    """Transcribe audio via OpenRouter using an audio-capable model."""
    import base64
    import requests

    with open(audio_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("ascii")

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "google/gemini-2.0-flash-001",
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64,
                            "format": "wav",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Transcribe the audio exactly as spoken. "
                            "Return ONLY the transcription text, nothing else. "
                            "If the audio is silence or noise, return an empty string."
                        ),
                    },
                ],
            }],
            "max_tokens": 200,
        },
        timeout=20,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    if text.lower() in ("", '""', "''", "empty", "none", "(silence)",
                         "(empty)", "[silence]", "[empty]"):
        return ""
    return text


def _transcribe_groq(audio_path: str, api_key: str) -> str:
    """Transcribe audio via Groq's Whisper endpoint (free tier available)."""
    return _transcribe_whisper_api(
        audio_path, api_key,
        base_url="https://api.groq.com/openai/v1",
    )


def _transcribe_vosk(audio_path: str) -> str:
    """Transcribe audio using vosk offline model."""
    import vosk
    import numpy as np
    import soundfile as sf

    model_path = VOICE_DIR / "vosk-model"
    if not model_path.exists():
        raise RuntimeError(
            f"Vosk model not found at {model_path}. "
            "Download from https://alphacephei.com/vosk/models "
            "and extract into ~/.ghost/voice/vosk-model/"
        )

    audio, sr = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]

    model = vosk.Model(str(model_path))
    rec = vosk.KaldiRecognizer(model, int(sr))
    audio_int16 = (audio * 32767).astype(np.int16).tobytes()
    rec.AcceptWaveform(audio_int16)
    result = json.loads(rec.FinalResult())
    return result.get("text", "").strip()


# ── Voice Engine ─────────────────────────────────────────────────────

class VoiceEngine:
    """Coordinates Voice Wake and Talk Mode.

    Lifecycle:
      IDLE → start_wake()/start_talk() → background thread runs → stop() → IDLE
    """

    def __init__(self, cfg: dict, auth_store=None):
        self.cfg = cfg
        self.auth_store = auth_store
        self.state = STATE_IDLE
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self.wake_words: list[str] = list(
            cfg.get("voice_wake_words", DEFAULT_WAKE_WORDS)
        )
        self.silence_threshold: float = cfg.get(
            "voice_silence_threshold", DEFAULT_SILENCE_THRESHOLD
        )
        self.silence_duration: float = cfg.get(
            "voice_silence_duration", DEFAULT_SILENCE_DURATION
        )
        self.capture_timeout: float = cfg.get(
            "voice_capture_timeout", DEFAULT_CAPTURE_TIMEOUT
        )
        self.stt_provider: str = cfg.get("voice_stt_provider", "auto")
        self.dashboard_port: int = cfg.get("dashboard_port", 3333)
        self.chime_enabled: bool = cfg.get("voice_chime", True)

        self.utterances_processed: int = 0
        self.last_transcript: str = ""
        self.last_response: str = ""
        self.last_user_message: str = ""
        self.active_message_id: str | None = None
        self.started_at: float | None = None

        # Push-to-talk state (independent of wake/talk modes)
        self._ptt_lock = threading.Lock()
        self._ptt_thread: threading.Thread | None = None
        self._ptt_state: str = "idle"   # idle, listening, transcribing, done, error
        self._ptt_text: str = ""
        self._ptt_error: str = ""

    # ── Public API ───────────────────────────────────────────────

    def start_wake(self) -> str:
        missing = _check_audio_deps()
        if missing:
            return (
                f"Missing audio dependencies: {', '.join(missing)}. "
                f"Install with: pip install {' '.join(missing)}"
            )
        with self._lock:
            if self.state != STATE_IDLE:
                return f"Voice engine already active (state: {self.state})"
            self.state = STATE_WAKE_LISTENING

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, args=("wake",),
            daemon=True, name="ghost-voice-wake",
        )
        self._thread.start()
        self.started_at = time.time()
        words = ", ".join(f'"{w}"' for w in self.wake_words)
        return f"Voice Wake started. Listening for: {words}"

    def start_talk(self) -> str:
        missing = _check_audio_deps()
        if missing:
            return (
                f"Missing audio dependencies: {', '.join(missing)}. "
                f"Install with: pip install {' '.join(missing)}"
            )
        with self._lock:
            if self.state != STATE_IDLE:
                return f"Voice engine already active (state: {self.state})"
            self.state = STATE_TALK_LISTENING

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, args=("talk",),
            daemon=True, name="ghost-voice-talk",
        )
        self._thread.start()
        self.started_at = time.time()
        return "Talk Mode started. Speak naturally — Ghost is listening."

    def stop(self) -> str:
        with self._lock:
            if self.state == STATE_IDLE:
                return "Voice engine is not running."
            prev_state = self.state
            self.state = STATE_IDLE

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self.started_at = None
        return f"Voice engine stopped (was: {prev_state})."

    # ── Push-to-talk (one-shot capture for the chat mic button) ──

    def start_ptt(self) -> dict:
        """Start a single push-to-talk capture. Returns immediately; poll ptt_status()."""
        missing = _check_audio_deps()
        if missing:
            return {"ok": False, "error": f"Missing: {', '.join(missing)}"}

        with self._ptt_lock:
            if self._ptt_state in ("listening", "transcribing"):
                return {"ok": False, "error": "PTT already active"}
            self._ptt_state = "listening"
            self._ptt_text = ""
            self._ptt_error = ""

        self._ptt_thread = threading.Thread(
            target=self._ptt_worker, daemon=True, name="ghost-ptt",
        )
        self._ptt_thread.start()
        return {"ok": True}

    def ptt_status(self) -> dict:
        with self._ptt_lock:
            return {
                "state": self._ptt_state,
                "text": self._ptt_text,
                "error": self._ptt_error,
            }

    def _ptt_worker(self):
        """Background worker: capture one utterance → transcribe → store text."""
        try:
            audio = self._capture_utterance()
            if audio is None:
                with self._ptt_lock:
                    self._ptt_state = "done"
                    self._ptt_text = ""
                return

            with self._ptt_lock:
                self._ptt_state = "transcribing"

            wav_path = self._save_wav(audio)
            if not wav_path:
                with self._ptt_lock:
                    self._ptt_state = "error"
                    self._ptt_error = "Failed to save audio"
                return

            try:
                text = self._transcribe(wav_path)
            finally:
                try:
                    Path(wav_path).unlink(missing_ok=True)
                except Exception:
                    pass

            with self._ptt_lock:
                self._ptt_state = "done"
                self._ptt_text = text.strip() if text else ""

        except Exception as exc:
            log.error("PTT error: %s", exc)
            with self._ptt_lock:
                self._ptt_state = "error"
                self._ptt_error = str(exc)

    def get_status(self) -> dict:
        return {
            "state": self.state,
            "mode": (
                "wake" if "wake" in self.state
                else "talk" if "talk" in self.state
                else self.state
            ),
            "wake_words": self.wake_words,
            "stt_provider": self.stt_provider,
            "utterances_processed": self.utterances_processed,
            "last_transcript": self.last_transcript,
            "last_response": self.last_response[:300] if self.last_response else "",
            "last_user_message": self.last_user_message,
            "active_message_id": self.active_message_id,
            "uptime_seconds": (
                round(time.time() - self.started_at)
                if self.started_at else 0
            ),
            "silence_threshold": self.silence_threshold,
            "silence_duration": self.silence_duration,
        }

    def set_wake_words(self, words: list[str]) -> str:
        sanitized = list(dict.fromkeys(
            w.strip().lower() for w in words if w.strip()
        ))
        if not sanitized:
            return "At least one wake word is required."
        self.wake_words = sanitized
        return f"Wake words updated: {', '.join(sanitized)}"

    # ── Background loop ──────────────────────────────────────────

    def _run_loop(self, mode: str):
        label = "Voice Wake" if mode == "wake" else "Talk Mode"
        log.info("%s loop started", label)
        print(f"  [VOICE] {label} active")

        try:
            while not self._stop_event.is_set():
                self._run_once(mode)
        except Exception as exc:
            log.error("Voice loop crashed: %s", exc, exc_info=True)
            print(f"  [VOICE] Error: {exc}")
        finally:
            with self._lock:
                self.state = STATE_IDLE
            log.info("%s loop stopped", label)
            print(f"  [VOICE] {label} stopped")

    def _run_once(self, mode: str):
        """Process one utterance: capture → transcribe → (wake check) → Ghost → speak."""
        listen_state = (
            STATE_WAKE_LISTENING if mode == "wake" else STATE_TALK_LISTENING
        )
        with self._lock:
            if self._stop_event.is_set():
                return
            self.state = listen_state

        audio = self._capture_utterance()
        if audio is None or self._stop_event.is_set():
            return

        wav_path = self._save_wav(audio)
        if not wav_path:
            return

        try:
            with self._lock:
                self.state = STATE_PROCESSING

            transcript = self._transcribe(wav_path)
            if not transcript:
                return

            log.info("Transcript: %s", transcript)
            self.last_transcript = transcript

            if mode == "wake":
                message = self._handle_wake(transcript)
                if message is None:
                    return
            else:
                message = transcript

            self.last_user_message = message

            response = self._send_to_ghost(message)
            self.active_message_id = None
            if not response or self._stop_event.is_set():
                return
            self.last_response = response
            self.utterances_processed += 1

            with self._lock:
                self.state = STATE_SPEAKING
            log.info("Speaking response (%d chars)", len(response))
            print(f"  [VOICE] Speaking: {response[:120]}{'…' if len(response) > 120 else ''}")
            self._speak(response)
            log.info("Finished speaking")

            if mode == "talk":
                time.sleep(DEFAULT_POST_SPEAK_COOLDOWN)

        finally:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except Exception:
                pass

    # ── Wake word handling ───────────────────────────────────────

    def _handle_wake(self, transcript: str) -> str | None:
        """Check transcript for a wake word. Returns the command or None."""
        command = self._extract_command(transcript)
        if command is None:
            return None

        if command:
            self._play_chime()
            print(f"  [VOICE] Wake → command: {command[:80]}")
            return command

        # Wake word detected but no command yet — listen for the command.
        self._play_chime()
        print("  [VOICE] Wake word detected, listening for command…")
        cmd_audio = self._capture_utterance()
        if cmd_audio is None or self._stop_event.is_set():
            return None
        cmd_path = self._save_wav(cmd_audio)
        if not cmd_path:
            return None
        try:
            command = self._transcribe(cmd_path)
        finally:
            Path(cmd_path).unlink(missing_ok=True)
        if not command:
            return None
        print(f"  [VOICE] Command: {command[:80]}")
        return command

    def _extract_command(self, transcript: str) -> str | None:
        """If transcript contains a wake word, return the command after it."""
        import re
        # Strip punctuation that STT adds ("Hey, ghost." → "hey ghost")
        normalized = re.sub(r'[,.\!\?;:\-]', '', transcript.lower()).strip()
        normalized = re.sub(r'\s+', ' ', normalized)

        for wake in self.wake_words:
            wl = wake.lower()
            if normalized == wl:
                return ""
            if normalized.startswith(wl + " "):
                return normalized[len(wl):].strip()
            if normalized.startswith(wl):
                rest = normalized[len(wl):].strip()
                if rest:
                    return rest
                return ""
        return None

    # ── Mic capture ──────────────────────────────────────────────

    def _capture_utterance(self):
        """Block until an utterance is captured (speech → silence), return numpy array."""
        import numpy as np
        import sounddevice as sd

        chunk_sec = 0.1
        chunk_samples = int(SAMPLE_RATE * chunk_sec)
        silence_chunks_needed = int(self.silence_duration / chunk_sec)
        max_chunks = int(self.capture_timeout / chunk_sec)

        audio_chunks: list = []
        silent_count = 0
        speech_started = False

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="float32", blocksize=chunk_samples,
            ) as stream:
                for _ in range(max_chunks):
                    if self._stop_event.is_set():
                        return None
                    data, _ = stream.read(chunk_samples)
                    energy = float(np.sqrt(np.mean(data ** 2)))

                    if energy > self.silence_threshold:
                        if not speech_started:
                            speech_started = True
                            with self._lock:
                                self.state = STATE_CAPTURING
                        silent_count = 0
                        audio_chunks.append(data.copy())
                    elif speech_started:
                        silent_count += 1
                        audio_chunks.append(data.copy())
                        if silent_count >= silence_chunks_needed:
                            break
        except Exception as exc:
            log.error("Mic capture error: %s", exc)
            return None

        if not speech_started or not audio_chunks:
            return None
        return np.concatenate(audio_chunks, axis=0)

    # ── File helpers ─────────────────────────────────────────────

    def _save_wav(self, audio) -> str | None:
        try:
            import soundfile as sf
            path = VOICE_DIR / f"utt_{int(time.time())}_{os.getpid()}.wav"
            sf.write(str(path), audio, SAMPLE_RATE)
            return str(path)
        except Exception as exc:
            log.error("Failed to save WAV: %s", exc)
            return None

    # ── STT ──────────────────────────────────────────────────────

    def _transcribe(self, audio_path: str) -> str:
        """Transcribe audio using the configured provider chain."""
        provider = self.stt_provider
        tried = []

        default_chain = ["moonshine", "openrouter", "whisper", "groq", "vosk"]
        chain = (self.cfg.get("provider_chains") or {}).get("voice_stt", default_chain)

        stt_dispatch = {
            "moonshine": self._try_moonshine,
            "openrouter": self._try_openrouter,
            "whisper": self._try_whisper,
            "groq": self._try_groq,
            "vosk": self._try_vosk,
        }

        order = chain if provider == "auto" else [provider]
        for pid in order:
            fn = stt_dispatch.get(pid)
            if not fn:
                continue
            result, err = fn(audio_path)
            if result is not None:
                return result
            if err:
                tried.append(f"{pid}: {err}")

        log.error("All STT providers failed for %s — %s", audio_path,
                  "; ".join(tried))
        return ""

    def _try_moonshine(self, audio_path):
        try:
            return _transcribe_moonshine(audio_path), None
        except ImportError:
            return None, "not installed"
        except Exception as exc:
            log.warning("Moonshine STT failed: %s", exc)
            return None, str(exc)

    def _try_openrouter(self, audio_path):
        key = self._get_key("openrouter")
        if not key:
            return None, "no key"
        try:
            return _transcribe_openrouter(audio_path, key), None
        except Exception as exc:
            log.warning("OpenRouter Whisper failed: %s", exc)
            return None, str(exc)

    def _try_whisper(self, audio_path):
        key = self._get_key("openai")
        if not key:
            return None, "no key"
        try:
            return _transcribe_whisper_api(audio_path, key), None
        except Exception as exc:
            log.warning("Whisper API failed: %s", exc)
            return None, str(exc)

    def _try_groq(self, audio_path):
        key = self._get_key("groq")
        if not key:
            return None, "no key"
        try:
            return _transcribe_groq(audio_path, key), None
        except Exception as exc:
            log.warning("Groq Whisper failed: %s", exc)
            return None, str(exc)

    def _try_vosk(self, audio_path):
        try:
            return _transcribe_vosk(audio_path), None
        except Exception as exc:
            log.warning("Vosk STT failed: %s", exc)
            return None, str(exc)

    def _get_key(self, provider: str) -> str:
        env_map = {
            "openai": "OPENAI_API_KEY",
            "groq": "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        key = os.environ.get(env_map.get(provider, ""), "")
        if not key and self.auth_store:
            try:
                key = self.auth_store.get_api_key(provider) or ""
            except Exception:
                pass
        return key

    # ── Ghost integration ────────────────────────────────────────

    def _send_to_ghost(self, message: str) -> str:
        """Send transcribed speech to Ghost's chat API and wait for response."""
        import requests as _req

        prefixed = VOICE_HINT + message
        port = self.dashboard_port
        base = f"http://127.0.0.1:{port}"

        log.info("Sending to Ghost chat: %s", message[:120])
        print(f"  [VOICE] Sending to Ghost: {message[:80]}")

        try:
            resp = _req.post(
                f"{base}/api/chat/send",
                json={"message": prefixed},
                timeout=10,
            )
            resp.raise_for_status()
            msg_id = resp.json().get("message_id")
            if not msg_id:
                log.warning("Chat API returned no message_id")
                return ""
        except Exception as exc:
            log.error("Chat send failed: %s", exc)
            return f"Sorry, I couldn't reach Ghost: {exc}"

        self.active_message_id = msg_id
        log.info("Chat message queued: %s, polling…", msg_id)

        for _ in range(600):
            if self._stop_event.is_set():
                return ""
            time.sleep(0.25)
            try:
                sr = _req.get(f"{base}/api/chat/status/{msg_id}", timeout=5)
                data = sr.json()
                status = data.get("status")
                if status in ("complete", "cancelled"):
                    result = data.get("result", "")
                    log.info("Ghost responded (%d chars): %s", len(result), result[:100])
                    print(f"  [VOICE] Ghost responded: {result[:120]}{'…' if len(result) > 120 else ''}")
                    return result
                if status == "error":
                    err = data.get("error", "")
                    log.error("Chat error: %s", err)
                    return f"Sorry, an error occurred: {err}"
            except Exception:
                continue

        log.warning("Chat poll timed out for %s", msg_id)
        return "Sorry, the request timed out."

    # ── TTS + playback ───────────────────────────────────────────

    def _speak(self, text: str):
        """Convert response text to speech and play it."""
        from ghost_tts import text_to_speech

        clean = text.strip()
        if len(clean) > 800:
            clean = clean[:800] + "…"

        log.info("TTS request (%d chars)…", len(clean))
        result = text_to_speech(
            text=clean, auth_store=self.auth_store, cfg=self.cfg,
        )
        if "error" in result:
            log.warning("TTS failed: %s", result["error"])
            print(f"  [VOICE] TTS error: {result['error']}")
            return

        audio_file = result.get("file")
        if audio_file:
            log.info("TTS produced %s, playing…", audio_file)
            played = _play_audio_file(audio_file)
            if played:
                log.info("Audio playback finished")
            else:
                log.warning("Audio playback failed for %s", audio_file)
                print(f"  [VOICE] Playback failed: {audio_file}")
        else:
            log.warning("TTS returned no audio file: %s", result)

    def _play_chime(self):
        """Short tone to acknowledge wake word detection."""
        if not self.chime_enabled:
            return
        try:
            import numpy as np
            import sounddevice as sd

            duration = 0.12
            freq = 880.0
            t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
            tone = 0.25 * np.sin(2 * np.pi * freq * t)
            fade = int(0.01 * SAMPLE_RATE)
            tone[:fade] *= np.linspace(0, 1, fade)
            tone[-fade:] *= np.linspace(1, 0, fade)
            sd.play(tone.astype(np.float32), SAMPLE_RATE)
            sd.wait()
        except Exception:
            pass


# ── Singleton ────────────────────────────────────────────────────────

_engine: VoiceEngine | None = None
_engine_lock = threading.Lock()


def _get_engine(cfg: dict, auth_store=None) -> VoiceEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = VoiceEngine(cfg=cfg, auth_store=auth_store)
        return _engine


def stop_voice_engine():
    """Cleanly shut down the voice engine (called on daemon stop)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.stop()
            _engine = None


# ── Tool definitions ─────────────────────────────────────────────────

def build_voice_tools(auth_store=None, cfg=None):
    """Build LLM-callable tools for Voice Wake + Talk Mode."""
    cfg = cfg or {}

    def voice_wake_start_exec(wake_words=None):
        engine = _get_engine(cfg, auth_store)
        if wake_words:
            engine.set_wake_words(wake_words)
        return engine.start_wake()

    def voice_wake_stop_exec():
        engine = _get_engine(cfg, auth_store)
        return engine.stop()

    def voice_talk_start_exec():
        engine = _get_engine(cfg, auth_store)
        return engine.start_talk()

    def voice_talk_stop_exec():
        engine = _get_engine(cfg, auth_store)
        return engine.stop()

    def voice_status_exec():
        engine = _get_engine(cfg, auth_store)
        return json.dumps(engine.get_status(), indent=2)

    def voice_config_exec(
        wake_words=None, silence_threshold=None,
        silence_duration=None, stt_provider=None, chime=None,
    ):
        engine = _get_engine(cfg, auth_store)
        results = []
        if wake_words is not None:
            results.append(engine.set_wake_words(wake_words))
        if silence_threshold is not None:
            engine.silence_threshold = max(0.001, min(1.0, float(silence_threshold)))
            results.append(f"Silence threshold → {engine.silence_threshold}")
        if silence_duration is not None:
            engine.silence_duration = max(0.5, min(10.0, float(silence_duration)))
            results.append(f"Silence duration → {engine.silence_duration}s")
        if stt_provider is not None:
            valid = ("auto", "moonshine", "openrouter", "whisper", "groq", "vosk")
            if stt_provider in valid:
                engine.stt_provider = stt_provider
                results.append(f"STT provider → {stt_provider}")
            else:
                results.append(f"Invalid provider '{stt_provider}'. Choose from: {', '.join(valid)}")
        if chime is not None:
            engine.chime_enabled = bool(chime)
            results.append(f"Wake chime → {'on' if engine.chime_enabled else 'off'}")
        return "\n".join(results) if results else "No changes made."

    return [
        {
            "name": "voice_wake_start",
            "description": (
                "Start Voice Wake mode — always-on microphone listening for wake words. "
                "When a wake word is detected (e.g. 'Quinely'), captures the spoken command, "
                "transcribes it, processes it through Quinely, and speaks the response aloud. "
                "Requires: pip install sounddevice numpy soundfile. "
                "STT requires an OpenAI or Groq API key, or offline vosk model."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wake_words": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Custom wake words to listen for. "
                            "Default: ['quinely', 'hey quinely']"
                        ),
                    },
                },
            },
            "execute": voice_wake_start_exec,
        },
        {
            "name": "voice_wake_stop",
            "description": "Stop Voice Wake mode (stop listening for wake words).",
            "parameters": {"type": "object", "properties": {}},
            "execute": voice_wake_stop_exec,
        },
        {
            "name": "voice_talk_start",
            "description": (
                "Start Talk Mode — continuous voice conversation with Ghost. "
                "No wake word needed. Every spoken utterance is transcribed, processed "
                "by Ghost, and the response is spoken aloud. A natural voice conversation. "
                "Requires: pip install sounddevice numpy soundfile."
            ),
            "parameters": {"type": "object", "properties": {}},
            "execute": voice_talk_start_exec,
        },
        {
            "name": "voice_talk_stop",
            "description": "Stop Talk Mode (stop continuous listening).",
            "parameters": {"type": "object", "properties": {}},
            "execute": voice_talk_stop_exec,
        },
        {
            "name": "voice_status",
            "description": (
                "Get the current voice engine status — mode, wake words, "
                "last transcript, utterance count, uptime."
            ),
            "parameters": {"type": "object", "properties": {}},
            "execute": voice_status_exec,
        },
        {
            "name": "voice_config",
            "description": (
                "Configure voice settings: wake words, silence detection, "
                "STT provider, chime on/off."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "wake_words": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Wake words to listen for.",
                    },
                    "silence_threshold": {
                        "type": "number",
                        "description": (
                            "Audio energy threshold for silence detection "
                            "(0.001–1.0, default 0.02). Lower = more sensitive."
                        ),
                    },
                    "silence_duration": {
                        "type": "number",
                        "description": (
                            "Seconds of silence before ending capture "
                            "(0.5–10.0, default 2.0)."
                        ),
                    },
                    "stt_provider": {
                        "type": "string",
                        "description": (
                            "Speech-to-text provider: 'auto' (try all), "
                            "'moonshine' (on-device, fast, free), "
                            "'openrouter', 'whisper' (OpenAI), 'groq', 'vosk' (offline)."
                        ),
                    },
                    "chime": {
                        "type": "boolean",
                        "description": "Play a chime when wake word is detected.",
                    },
                },
            },
            "execute": voice_config_exec,
        },
    ]
