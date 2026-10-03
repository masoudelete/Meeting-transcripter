"""Paths, settings and file helpers shared by the recorder and the note writer."""
import json
import os
from pathlib import Path

# Settings, log and venv live here; meetings go to "meetings_dir".
DATA_DIR = Path(os.environ.get("MEETING_TRANSCRIPTER_HOME")
                or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MeetingTranscripter")
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "recorder.log"

DEFAULT_CONFIG = {
    "meetings_dir": str(Path.home() / "Meetings"),
    "me_label": "Me",
    "others_label": "Others",
    # Packaged-app family names or exe names whose mic use always means a call.
    "always_record_apps": ["MSTeams_8wekyb3d8bbwe", "ms-teams.exe", "teams.exe", "zoom.exe"],
    # Browsers count only while one of their windows has a title keyword below.
    "browser_apps": ["msedge.exe", "chrome.exe", "firefox.exe", "brave.exe"],
    "title_keywords": ["Microsoft Teams", "Meet - ", "Meet – "],
    "title_grace_seconds": 120,
    "poll_seconds": 2,
    "end_after_mic_released_seconds": 30,
    "min_minutes": 1.0,
    "max_hours": 4,
    "model": "large-v3-turbo",
    "device": "auto",  # "cuda", "cpu" or "auto"
    "compute_type": "auto",  # "auto" = float16 on the GPU, int8 on the CPU
    "language": None,  # e.g. "en" to skip language detection
    "languages": [],  # limit language detection to these, e.g. ["en", "fr"]
    "hotwords": "",
    "keep_audio_days": 14,
    # Notes: "claude", "ollama" or "none" (transcripts only).
    "summarizer": "none",
    "notes_language": None,  # None = the meeting's language
    "anthropic_api_key": "",  # or set ANTHROPIC_API_KEY
    "claude_model": "claude-opus-5-5",
    "claude_effort": "medium",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1:8b",
    "ollama_num_ctx": 32768,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    return cfg


def update_config(changes):
    """Merge changes into config.json, keeping keys the user already set.

    A new config.json starts with every default, so the user sees all settings when editing it."""
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    else:
        cfg = dict(DEFAULT_CONFIG)
    cfg.update(changes)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(CONFIG_PATH, cfg)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def hms(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
