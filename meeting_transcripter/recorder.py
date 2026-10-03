"""Meeting recorder: records calls on this PC and transcribes them locally.

How it works
- Windows logs which apps hold the microphone (CapabilityAccessManager registry key).
  A call is "on" while a meeting app (Teams, Zoom) holds the mic, or while a browser
  holds it and one of that browser's windows has a meeting title ("... Microsoft Teams",
  "Meet - ...").
- During a call it records two tracks: the default microphone ("me") and the
  loopback of every output device ("others").
- When the call releases the mic it transcribes both tracks with faster-whisper and
  writes <meetings_dir>/<YYYY-MM-DD_HHMM>[_title]/transcript.md plus meta.json with
  status "transcribed". If a summarizer is set up, summarize.py then writes notes.md.

Usage
  pythonw -m meeting_transcripter                    tray icon + automatic recording
  python  -m meeting_transcripter --test-seconds 20  record 20 s now, transcribe, print the result
  python  -m meeting_transcripter --finalize DIR     (re)transcribe a meeting folder
  python  -m meeting_transcripter --notes DIR        (re)write notes.md for a meeting folder
"""
import argparse
import ctypes
import datetime as dt
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

from . import __version__, summarize
from .common import CONFIG_PATH, DATA_DIR, LOG_PATH, hms, load_config, read_json, update_config, write_json

SAMPLE_RATE = 16000
ACTIVE_STATUSES = ("recording", "recorded", "transcribing")

log = logging.getLogger("recorder")


# ---------------------------------------------------------------- helpers

def setup_logging(console):
    handlers = [logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    for noisy in ("httpx", "httpx2", "anthropic", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def add_cuda_dll_dirs():
    """ctranslate2 loads cuBLAS/cuDNN at runtime; they come from the nvidia-* wheels."""
    if os.name != "nt":
        return
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn", "cuda_nvrtc"):
        bin_dir = root / sub / "bin"
        if bin_dir.is_dir() and str(bin_dir) not in os.environ.get("PATH", ""):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def model_attempts(cfg):
    """(device, compute_type) pairs to try in order; the CPU is always the last resort."""
    device = cfg["device"]
    if device == "auto":
        add_cuda_dll_dirs()
        import ctranslate2
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    compute_type = cfg["compute_type"]
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"
    attempts = [(device, compute_type)]
    if device != "cpu":
        attempts.append(("cpu", "int8"))
    return attempts


# ---------------------------------------------------------------- call detection

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _holds_mic(key):
    import winreg
    try:
        start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
        stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
    except OSError:
        return False
    return start != 0 and stop == 0


def mic_users():
    """Apps holding the microphone right now, lower-case: packaged family names and exe names."""
    import winreg
    users = set()
    for path, exe_names in ((MIC_KEY, False), (MIC_KEY + r"\NonPackaged", True)):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    if name == "NonPackaged":
                        continue
                    with winreg.OpenKey(key, name) as sub:
                        if _holds_mic(sub):
                            users.add((name.split("#")[-1] if exe_names else name).lower())
        except OSError:
            pass
    return users


_user32 = None
_EnumProc = None


def user32():
    global _user32, _EnumProc
    if _user32 is None:
        import ctypes.wintypes as wt
        _EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.EnumWindows.argtypes = [_EnumProc, wt.LPARAM]
        lib.IsWindowVisible.argtypes = [wt.HWND]
        lib.GetWindowTextLengthW.argtypes = [wt.HWND]
        lib.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        lib.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        _user32 = lib
    return _user32


def visible_windows():
    """(title, exe name lower-case) for every visible titled top-level window."""
    import ctypes.wintypes as wt

    import psutil
    lib = user32()
    found = []

    def callback(hwnd, _):
        if lib.IsWindowVisible(hwnd):
            n = lib.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                lib.GetWindowTextW(hwnd, buf, n + 1)
                pid = wt.DWORD()
                lib.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((buf.value, pid.value))
        return True

    lib.EnumWindows(_EnumProc(callback), 0)
    names = {}
    out = []
    for title, pid in found:
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name().lower()
            except Exception:
                names[pid] = ""
        out.append((title, names[pid]))
    return out


GENERIC_NAMES = {"", "chat", "calendar", "activity", "teams", "calls", "files", "home", "meet",
                 "meeting", "call", "microsoft teams", "apps", "onedrive", "communities"}


def meeting_name(title):
    """'Weekly sync | Microsoft Teams - Work - Microsoft Edge' -> 'Weekly sync'; None if generic.

    Only Teams titles carry the meeting's name; Google Meet titles carry a room code."""
    if "microsoft teams" not in title.lower():
        return None
    name = re.split(r"(?:^|\s*[|\-–]\s*)Microsoft Teams", title, maxsplit=1, flags=re.I)[0]
    name = re.sub(r"^\(\d+\)\s*", "", name).strip()
    parts = [p.strip() for p in name.split("|") if p.strip().lower() not in GENERIC_NAMES]
    name = " | ".join(parts)
    return None if name.lower() in GENERIC_NAMES else name


def device_signature():
    """Changes when the default speaker/mic changes or an output device appears or goes away."""
    try:
        from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
        from pycaw.pycaw import AudioUtilities
        enumerator = AudioUtilities.GetDeviceEnumerator()
        defaults = []
        for flow in (EDataFlow.eRender.value, EDataFlow.eCapture.value):
            try:
                defaults.append(enumerator.GetDefaultAudioEndpoint(flow, ERole.eMultimedia.value).GetId())
            except Exception:
                defaults.append(None)
        active = sorted(d.id for d in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value))
        return (*defaults, *active)
    except Exception:
        log.exception("device check failed")
        return None


# ---------------------------------------------------------------- recording

class TrackWriter:
    """Writes one input stream to a mono WAV part, padding silence where the stream stalled.

    WASAPI loopback delivers nothing while no audio is playing, so gaps are measured
    against the wall clock and filled with zeros to keep the tracks aligned.
    """

    def __init__(self, path, rate, channels, offset):
        self.rate = rate
        self.channels = channels
        self.offset = offset
        self.t0 = time.perf_counter()
        self.frames = 0
        self.q = queue.SimpleQueue()
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(rate)

    def callback(self, in_data, frame_count, time_info, status):
        self.q.put((time.perf_counter(), in_data, frame_count))
        return (None, 0)  # paContinue

    def drain(self):
        while True:
            try:
                t, data, n = self.q.get_nowait()
            except queue.Empty:
                return
            samples = np.frombuffer(data, dtype=np.int16)
            if self.channels > 1:
                samples = samples.reshape(-1, self.channels).mean(axis=1).astype(np.int16)
            gap = int((t - self.t0) * self.rate) - n - self.frames
            if gap > self.rate * 0.3:
                while gap > 0:
                    chunk = min(gap, self.rate)
                    self.wav.writeframes(bytes(chunk * 2))
                    self.frames += chunk
                    gap -= chunk
            self.wav.writeframes(samples.tobytes())
            self.frames += n

    def close(self):
        self.drain()
        self.wav.close()


class Recording:
    def __init__(self, cfg, trigger, apps=(), titles=()):
        self.cfg = cfg
        self.trigger = trigger  # "auto" or "manual"
        self.apps = set(apps)
        self.titles = []
        self.add_titles(titles)
        self.started = dt.datetime.now().astimezone()
        self.ended = None
        self.t0 = time.perf_counter()
        base = Path(cfg["meetings_dir"]) / self.started.strftime("%Y-%m-%d_%H%M")
        folder, n = base, 2
        while folder.exists():
            folder = base.with_name(f"{base.name}-{n}")
            n += 1
        self.folder = folder
        (folder / "parts").mkdir(parents=True)
        self.parts = []
        self.writers = []
        self.streams = []
        self.pa = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.device_sig = device_signature()
        self._open_streams()
        if not self.streams:
            shutil.rmtree(folder, ignore_errors=True)
            raise RuntimeError("no audio device could be opened")
        self._write_meta("recording")
        threading.Thread(target=self._drain_loop, daemon=True).start()

    @property
    def minutes(self):
        return (time.perf_counter() - self.t0) / 60

    def add_titles(self, titles):
        for t in titles:
            if t not in self.titles:
                self.titles.append(t)

    def _open_streams(self):
        import pyaudiowpatch as pyaudio
        self.pa = pyaudio.PyAudio()  # a fresh instance rescans the device list
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        devices = []
        if wasapi["defaultInputDevice"] >= 0:
            devices.append(("mic", self.pa.get_device_info_by_index(wasapi["defaultInputDevice"])))
        devices += [("others", d) for d in self.pa.get_loopback_device_info_generator()]
        for kind, dev in devices:
            channels = max(1, min(2, int(dev["maxInputChannels"])))
            rate = int(dev["defaultSampleRate"])
            path = self.folder / "parts" / f"{len(self.parts) + 1:03d}_{kind}.wav"
            writer = TrackWriter(path, rate, channels, time.perf_counter() - self.t0)
            try:
                stream = self.pa.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                                      input_device_index=dev["index"], frames_per_buffer=rate // 20,
                                      stream_callback=writer.callback)
            except Exception as exc:
                log.warning("cannot open %s device %r: %s", kind, dev["name"], exc)
                writer.close()
                path.unlink(missing_ok=True)
                continue
            with self.lock:
                self.writers.append(writer)
            self.streams.append(stream)
            self.parts.append({"kind": kind, "file": path.name, "offset": round(writer.offset, 3),
                               "device": dev["name"]})
            log.info("recording %s from %s (%d Hz, %d ch)", kind, dev["name"], rate, channels)

    def _close_streams(self):
        for stream in self.streams:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                log.exception("closing stream")
        self.streams = []
        with self.lock:
            for writer in self.writers:
                writer.close()
            self.writers = []
        if self.pa:
            self.pa.terminate()
            self.pa = None

    def _drain_loop(self):
        while not self.stopped.wait(0.25):
            with self.lock:
                for writer in self.writers:
                    try:
                        writer.drain()
                    except Exception:
                        log.exception("writing audio")

    def check_devices(self):
        sig = device_signature()
        if sig is not None and sig != self.device_sig:
            log.info("audio devices changed; reopening streams")
            self.device_sig = sig
            self._close_streams()
            self._open_streams()
            self._write_meta("recording")

    def stop(self, ended=None):
        self.stopped.set()
        self._close_streams()
        self.ended = ended or dt.datetime.now().astimezone()
        self._write_meta("recorded")

    def discard(self):
        self.stopped.set()
        self._close_streams()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _write_meta(self, status):
        write_json(self.folder / "meta.json", {
            "status": status,
            "trigger": self.trigger,
            "apps": sorted(self.apps),
            "window_titles": self.titles,
            "started_at": self.started.isoformat(timespec="seconds"),
            "ended_at": self.ended.isoformat(timespec="seconds") if self.ended else None,
            "parts": self.parts,
        })


# ---------------------------------------------------------------- transcription

def is_hallucination(seg, audio):
    """Whisper invents text on noise and quiet stretches (often echoing the hotwords);
    those segments score a low average log-probability."""
    if seg.avg_logprob < -1.0 or seg.compression_ratio > 2.4:
        return True
    if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.5:
        return True
    clip = audio[int(seg.start * SAMPLE_RATE):int(seg.end * SAMPLE_RATE)]
    return clip.size > 0 and float(np.sqrt(np.mean(clip ** 2))) < 0.003 and seg.avg_logprob < -0.7


def transcribe(tracks, cfg):
    """tracks: {speaker label: float32 16 kHz array, all the same length}. Returns (segments, language)."""
    add_cuda_dll_dirs()
    from faster_whisper import WhisperModel
    attempts = model_attempts(cfg)
    for i, (device, compute_type) in enumerate(attempts):
        try:
            model = WhisperModel(cfg["model"], device=device, compute_type=compute_type)
            language = cfg.get("language")
            if not language:
                # Detect once on the mix: a mostly-silent mic track alone gets misdetected.
                _, _, probs = model.detect_language(sum(tracks.values()), vad_filter=True,
                                                    language_detection_segments=4)
                allowed = cfg.get("languages") or []
                language = max((p, lang) for lang, p in probs if not allowed or lang in allowed)[1]
            segments = []
            for speaker, audio in tracks.items():
                segs, _ = model.transcribe(audio, language=language, vad_filter=True, beam_size=5,
                                           condition_on_previous_text=False,
                                           hotwords=cfg.get("hotwords") or None)
                for s in segs:
                    text = s.text.strip()
                    if text and not is_hallucination(s, audio):
                        segments.append({"speaker": speaker, "start": s.start, "end": s.end, "text": text})
            del model
            return segments, language
        except Exception:
            log.exception("transcription on %s failed", device)
            if i == len(attempts) - 1:
                raise


WORD = re.compile(r"\w+")


def drop_echo(mine, theirs, window=2.0):
    """On speakers the mic also hears the call. Drop mic segments whose words were
    mostly spoken in the call audio at the same moment."""
    kept = []
    for s in mine:
        words = set(WORD.findall(s["text"].lower()))
        near = set()
        for o in theirs:
            if o["start"] < s["end"] + window and o["end"] > s["start"] - window:
                near |= set(WORD.findall(o["text"].lower()))
        if len(words) >= 3 and len(words & near) / len(words) >= 0.7:
            continue
        kept.append(s)
    return kept


def merge_turns(segments, gap=1.5):
    turns = []
    for s in sorted(segments, key=lambda s: s["start"]):
        if turns and turns[-1]["speaker"] == s["speaker"] and s["start"] - turns[-1]["end"] <= gap:
            turns[-1]["text"] += " " + s["text"]
            turns[-1]["end"] = max(turns[-1]["end"], s["end"])
        else:
            turns.append(dict(s))
    return turns


def save_audio(path, samples):
    """Opus in Ogg, ~11 MB per hour of 16 kHz mono. Falls back to WAV."""
    import av
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    try:
        with av.open(str(path), "w", format="ogg") as container:
            stream = container.add_stream("libopus", rate=SAMPLE_RATE, layout="mono")
            stream.bit_rate = 24000
            step = SAMPLE_RATE * 10
            for i in range(0, len(pcm), step):
                frame = av.AudioFrame.from_ndarray(pcm[i:i + step].reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = SAMPLE_RATE
                frame.pts = i
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        return path.name
    except Exception:
        log.exception("opus encoding failed; keeping WAV")
        path.unlink(missing_ok=True)
        wav_path = path.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return wav_path.name


def finalize(folder, cfg):
    """Mix the parts, transcribe, write transcript.md, compress the audio. Returns a summary dict."""
    from faster_whisper.audio import decode_audio
    folder = Path(folder)
    meta = read_json(folder / "meta.json")
    meta["status"] = "transcribing"
    write_json(folder / "meta.json", meta)

    loaded, total = [], 0
    for part in meta["parts"]:
        f = folder / "parts" / part["file"]
        if not f.exists() or f.stat().st_size <= 44:
            continue
        audio = decode_audio(str(f), sampling_rate=SAMPLE_RATE)
        start = int(part["offset"] * SAMPLE_RATE)
        loaded.append((part["kind"], start, audio))
        total = max(total, start + len(audio))
    tracks = {}
    for kind, start, audio in loaded:
        track = tracks.setdefault(kind, np.zeros(total, dtype=np.float32))
        track[start:start + len(audio)] += audio
    del loaded

    started = dt.datetime.fromisoformat(meta["started_at"])
    ended = dt.datetime.fromisoformat(meta["ended_at"]) if meta.get("ended_at") else \
        started + dt.timedelta(seconds=total / SAMPLE_RATE)
    minutes = total / SAMPLE_RATE / 60
    if meta["trigger"] == "auto" and minutes < cfg["min_minutes"]:
        log.info("discarding %s: only %.1f min", folder.name, minutes)
        shutil.rmtree(folder, ignore_errors=True)
        return None

    labels = {"mic": cfg["me_label"], "others": cfg["others_label"]}
    audible = {labels[k]: np.clip(v, -1, 1) for k, v in tracks.items() if float(np.sqrt(np.mean(v ** 2))) > 1e-4}
    segments, language = transcribe(audible, cfg) if audible else ([], None)
    mine = [s for s in segments if s["speaker"] == labels["mic"]]
    theirs = [s for s in segments if s["speaker"] == labels["others"]]
    turns = merge_turns(drop_echo(mine, theirs) + theirs)

    name = next((n for n in map(meeting_name, meta.get("window_titles", [])) if n), None)
    title = name or ("Call" if meta["trigger"] == "auto" else "Recording")
    length = f"{round(minutes)} min" if minutes >= 1 else "under 1 min"
    captured = "automatically from " + ", ".join(meta["apps"]) if meta["trigger"] == "auto" else "manual start"
    lines = [
        f"# {title} — {started:%a %Y-%m-%d}, {started:%H:%M}–{ended:%H:%M} ({length})",
        "",
        f"- Captured: {captured}",
        f"- Window titles seen: {'; '.join(meta.get('window_titles') or []) or 'none'}",
        f"- Speakers: \"{labels['mic']}\" = this computer's microphone; \"{labels['others']}\" = call audio "
        "(everyone else, not separated by person)",
        f"- Language: {language or 'n/a'}",
        "",
        "---",
        "",
    ]
    lines += [f"[{hms(t['start'])}] {t['speaker']}: {t['text']}" for t in turns] or ["(no speech detected)"]
    (folder / "transcript.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    audio_files = []
    if cfg.get("keep_audio_days"):
        for kind, track in tracks.items():
            audio_files.append(save_audio(folder / f"{kind}.opus", track))
    shutil.rmtree(folder / "parts", ignore_errors=True)

    if name:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
        target = folder.with_name(f"{folder.name}_{slug}")
        if slug and not target.exists():
            folder.rename(target)
            folder = target

    notes_pending = summarize.enabled(cfg)
    meta.update({
        "status": "transcribed",
        "ended_at": ended.isoformat(timespec="seconds"),
        "duration_min": round(minutes, 1),
        "title_hint": name,
        "language": language,
        "turns": len(turns),
        "transcript": "transcript.md",
        "audio": audio_files,
        "parts": [],
        "transcribed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes_pending": notes_pending,
    })
    write_json(folder / "meta.json", meta)
    log.info("transcribed %s: %.1f min, %d turns", folder.name, minutes, len(turns))
    return {"folder": str(folder), "title": title, "length": length, "turns": len(turns),
            "notes_pending": notes_pending}


def purge_old_audio(cfg):
    days = cfg.get("keep_audio_days")
    if not days:
        return
    cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=days)
    for meta_path in Path(cfg["meetings_dir"]).glob("*/meta.json"):
        try:
            meta = read_json(meta_path)
            if meta.get("status") in ACTIVE_STATUSES or dt.datetime.fromisoformat(meta["started_at"]) > cutoff:
                continue
            for pattern in ("*.opus", "*.wav"):
                for f in meta_path.parent.glob(pattern):
                    f.unlink()
        except Exception:
            log.exception("purging audio in %s", meta_path.parent)


# ---------------------------------------------------------------- monitor + tray

class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rec = None
        self.paused = False
        self.suppressed = False  # user stopped this call by hand; wait until the mic is released
        self.browser_mic_since = None
        self.browser_titles_logged = False
        self.released_at = None
        self.busy = ""  # "Transcribing…" or "Writing notes…" while the worker runs
        self.jobs = queue.Queue()
        self.quit = threading.Event()
        self.lock = threading.RLock()
        self.last_device_check = 0
        self.last_notes_check = 0
        self.config_mtime = self._config_mtime()
        self.announced = None  # folders already "processed" or "failed"
        self.on_change = lambda: None
        self.notify = lambda msg: log.info("notify: %s", msg)

    def status(self):
        if self.rec:
            what = "call" if self.rec.trigger == "auto" else "manual recording"
            return f"Recording {what} — {self.rec.minutes:.0f} min"
        if self.busy:
            return self.busy
        if self.paused:
            return "Paused — calls are not recorded"
        return "Watching for calls"

    def run(self):
        import comtypes
        comtypes.CoInitialize()
        log.info("started %s; watching for calls", __version__)
        for meta_path in sorted(Path(self.cfg["meetings_dir"]).glob("*/meta.json")):
            try:
                meta = read_json(meta_path)
                if meta.get("status") in ACTIVE_STATUSES:
                    log.info("recovering unfinished %s", meta_path.parent.name)
                    self.jobs.put(("transcribe", meta_path.parent))
                elif meta.get("notes_pending") and meta.get("status") in ("transcribed", "processing", "failed"):
                    # notes_pending survives only failures worth retrying (e.g. offline)
                    log.info("writing pending notes for %s", meta_path.parent.name)
                    self.jobs.put(("notes", meta_path.parent))
            except Exception:
                log.exception("reading %s", meta_path)
        purge_old_audio(self.cfg)
        threading.Thread(target=self._worker, daemon=True).start()
        while not self.quit.is_set():
            try:
                self._reload_config()
                with self.lock:
                    self._tick()
                self._announce_notes()
            except Exception:
                log.exception("monitor tick failed")
            self.quit.wait(self.cfg["poll_seconds"])
        with self.lock:
            if self.rec:
                self._stop(keep=True)  # transcribed on next start

    @staticmethod
    def _config_mtime():
        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return None

    def _reload_config(self):
        """Pick up edits to config.json without a restart."""
        mtime = self._config_mtime()
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime
        try:
            cfg = load_config()
        except Exception as exc:
            log.error("config.json not reloaded: %s", exc)
            self.notify(f"Settings not applied: config.json has an error ({exc}).")
            return
        Path(cfg["meetings_dir"]).mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.cfg = cfg
        log.info("settings reloaded")
        self.notify("Settings saved and applied.")

    def _tick(self):
        cfg = self.cfg
        now = time.monotonic()
        users = mic_users()
        call_app = users & {a.lower() for a in cfg["always_record_apps"]}
        browsers = users & {a.lower() for a in cfg["browser_apps"]}
        if browsers:
            self.browser_mic_since = self.browser_mic_since or now
        else:
            self.browser_mic_since = None
            self.browser_titles_logged = False
        titles = []
        if call_app or browsers:
            keywords = [k.lower() for k in cfg["title_keywords"]]
            windows = visible_windows()
            titles = [t for t, exe in windows if any(k in t.lower() for k in keywords)]
            if browsers and not titles and not self.browser_titles_logged and not self.rec:
                self.browser_titles_logged = True
                log.info("browser holds the mic but no meeting window; browser windows: %s",
                         [t for t, exe in windows if exe in browsers])

        rec = self.rec
        if rec is None:
            if not (call_app or browsers):
                self.suppressed = False
            browser_call = bool(browsers and titles) and now - self.browser_mic_since <= cfg["title_grace_seconds"]
            if (call_app or browser_call) and not self.paused and not self.suppressed:
                self._start("auto", call_app | browsers, titles)
            return

        rec.add_titles(titles)
        if rec.trigger == "auto":
            if call_app or (browsers & rec.apps):
                rec.apps |= call_app
                self.released_at = None
            else:
                if self.released_at is None:
                    self.released_at = (now, dt.datetime.now().astimezone())
                if now - self.released_at[0] >= cfg["end_after_mic_released_seconds"]:
                    self._stop(keep=True, ended=self.released_at[1])
                    return
        if rec.minutes >= cfg["max_hours"] * 60:
            log.info("max length reached; starting a new part")
            manual = rec.trigger == "manual"
            self._stop(keep=True)
            if manual:
                self._start("manual")
            return  # an auto call still in progress restarts on the next tick
        if now - self.last_device_check >= 3:
            self.last_device_check = now
            rec.check_devices()

    def _announce_notes(self):
        """Toast once for each meeting whose notes are done (by this app or another tool)."""
        if time.monotonic() - self.last_notes_check < 30:
            return
        self.last_notes_check = time.monotonic()
        done = {}
        for meta_path in Path(self.cfg["meetings_dir"]).glob("*/meta.json"):
            try:
                meta = read_json(meta_path)
            except Exception:
                continue
            # A failure without a transcript is a transcription failure, which the worker reports.
            if meta.get("status") == "processed" or (meta.get("status") == "failed" and meta.get("transcript")):
                done[meta_path.parent.name] = meta
        if self.announced is None:  # first pass: don't re-announce old meetings
            self.announced = set(done)
            return
        for name in sorted(set(done) - self.announced):
            self.announced.add(name)
            meta = done[name]
            if meta["status"] == "failed":
                self.notify(f"Notes failed for {name}: {meta.get('error', 'see recorder.log')}")
            else:
                self.notify(f"Notes ready: {meta.get('title') or name}")

    def _start(self, trigger, apps=(), titles=()):
        try:
            self.rec = Recording(self.cfg, trigger, apps, titles)
        except Exception:
            log.exception("could not start recording")
            self.rec = None
            self.suppressed = True
            self.notify("Could not start recording. See recorder.log.")
            return
        self.released_at = None
        log.info("recording started (%s, apps=%s) in %s", trigger, sorted(apps), self.rec.folder.name)
        self.notify("Recording this call. Right-click the tray icon to stop or discard."
                    if trigger == "auto" else "Recording started.")
        self.on_change()

    def _stop(self, keep, ended=None):
        rec, self.rec = self.rec, None
        if rec is None:
            return
        if keep:
            rec.stop(ended)
            self.jobs.put(("transcribe", rec.folder))
            log.info("recording stopped after %.1f min", rec.minutes)
        else:
            rec.discard()
            log.info("recording discarded")
        self.released_at = None
        self.on_change()

    def _worker(self):
        while True:
            action, folder = self.jobs.get()
            self.busy = "Transcribing…" if action == "transcribe" else "Writing notes…"
            self.on_change()
            try:
                if action == "transcribe":
                    result = finalize(folder, self.cfg)
                    if result and result["notes_pending"]:
                        self.jobs.put(("notes", Path(result["folder"])))
                    elif result:
                        self.notify(f"Transcript ready: {result['title']} ({result['length']})")
                else:
                    summarize.process(folder, self.cfg)  # marks the meeting failed on errors
            except Exception:
                what = "transcription" if action == "transcribe" else "notes"
                log.exception("%s failed for %s", what, folder)
                try:
                    meta = read_json(Path(folder) / "meta.json")
                    meta["status"] = "failed"
                    meta["error"] = f"{what} failed; see recorder.log"
                    write_json(Path(folder) / "meta.json", meta)
                except Exception:
                    pass
                if action == "transcribe":  # failed notes are announced by _announce_notes
                    self.notify("Transcription failed. See recorder.log.")
            finally:
                self.busy = ""
                self.on_change()
                purge_old_audio(self.cfg)

    # tray actions
    def record_now(self):
        with self.lock:
            if self.rec is None:
                self._start("manual")

    def stop_recording(self, keep):
        with self.lock:
            if self.rec:
                if self.rec.trigger == "auto":
                    self.suppressed = True
                self._stop(keep=keep)
                if not keep:
                    self.notify("Recording discarded.")

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused
        self.on_change()


def open_settings():
    if not CONFIG_PATH.exists():
        update_config({})
    subprocess.Popen(["notepad.exe", str(CONFIG_PATH)])


def tray_image(color, hollow=False):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if hollow:
        draw.ellipse((8, 8, 56, 56), outline=color, width=8)
    else:
        draw.ellipse((8, 8, 56, 56), fill=color)
    return img


def run_tray(monitor):
    import pystray

    icons = {"idle": tray_image((128, 128, 128)), "rec": tray_image((220, 38, 38)),
             "busy": tray_image((234, 160, 0)), "paused": tray_image((128, 128, 128), hollow=True)}

    def current_icon():
        if monitor.rec:
            return icons["rec"]
        if monitor.busy:
            return icons["busy"]
        return icons["paused"] if monitor.paused else icons["idle"]

    def quit_app(icon):
        monitor.quit.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: monitor.status(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Record now", lambda: monitor.record_now(), visible=lambda item: monitor.rec is None),
        pystray.MenuItem("Stop and keep", lambda: monitor.stop_recording(True),
                         visible=lambda item: monitor.rec is not None),
        pystray.MenuItem("Stop and discard", lambda: monitor.stop_recording(False),
                         visible=lambda item: monitor.rec is not None),
        pystray.MenuItem("Pause automatic recording", lambda: monitor.toggle_pause(),
                         checked=lambda item: monitor.paused),
        pystray.MenuItem("Open meetings folder", lambda: os.startfile(monitor.cfg["meetings_dir"])),
        pystray.MenuItem("Settings", open_settings),
        pystray.MenuItem("Open log", lambda: os.startfile(LOG_PATH)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("MeetingTranscripter", current_icon(), "Meeting Transcripter", menu)

    def refresh():
        icon.icon = current_icon()
        icon.title = "Meeting Transcripter: " + monitor.status()
        icon.update_menu()

    def setup(icon):
        icon.visible = True
        monitor.on_change = refresh
        monitor.notify = lambda msg: icon.notify(msg, "Meeting Transcripter")
        threading.Thread(target=monitor.run, daemon=True).start()
        threading.Thread(target=lambda: [refresh() for _ in iter(lambda: monitor.quit.wait(30), True)],
                         daemon=True).start()

    icon.run(setup)


def single_instance():
    import ctypes.wintypes as wt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wt.HANDLE
    single_instance.handle = kernel32.CreateMutexW(None, False, "Local\\MeetingTranscripterSingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def parse_setting(value):
    """'0' -> 0, '["en"]' -> ['en'], 'Jane Doe' -> 'Jane Doe'."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def main():
    parser = argparse.ArgumentParser(prog="meeting_transcripter", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--test-seconds", type=float, help="record this many seconds now, then transcribe")
    parser.add_argument("--finalize", metavar="DIR", help="(re)transcribe a meeting folder")
    parser.add_argument("--notes", metavar="DIR", help="(re)write notes.md for a meeting folder")
    parser.add_argument("--download-model", action="store_true",
                        help="download the speech model and print the device it will run on")
    parser.add_argument("--set", nargs=2, action="append", metavar=("KEY", "VALUE"),
                        help="change a setting in config.json, e.g. --set summarizer ollama")
    parser.add_argument("--merge-settings", metavar="FILE", help="merge a JSON object of settings into config.json")
    parser.add_argument("--icon", metavar="PATH", help="write the app icon (.ico) to PATH")
    args = parser.parse_args()
    if sys.stdout is None:  # pythonw
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.set or args.merge_settings:
        changes = {key: parse_setting(value) for key, value in args.set or []}
        if args.merge_settings:
            changes.update(json.loads(Path(args.merge_settings).read_text(encoding="utf-8-sig")))
        update_config(changes)
        print("updated", ", ".join(changes) or "nothing", "in", CONFIG_PATH)
        return
    if args.icon:
        tray_image((220, 38, 38)).save(args.icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        return

    cfg = load_config()
    Path(cfg["meetings_dir"]).mkdir(parents=True, exist_ok=True)
    setup_logging(console=bool(args.test_seconds or args.finalize or args.notes or args.download_model))

    if args.download_model:
        from faster_whisper.utils import download_model
        print("model files:", download_model(cfg["model"]))
        print("device:", "%s (%s)" % model_attempts(cfg)[0])
    elif args.finalize:
        result = finalize(args.finalize, cfg)
        print(result)
        if result and result["notes_pending"]:
            print(summarize.process(result["folder"], cfg))
    elif args.notes:
        print(summarize.process(args.notes, cfg))
    elif args.test_seconds:
        rec = Recording(cfg, "manual")
        print("recording to", rec.folder, flush=True)
        time.sleep(args.test_seconds)
        rec.stop()
        print(finalize(rec.folder, cfg))
    elif single_instance():
        run_tray(Monitor(cfg))
