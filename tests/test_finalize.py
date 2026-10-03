"""finalize() end to end with the speech model stubbed out (the real model runs in the Windows CI job)."""
import wave

import numpy as np
import pytest

from meeting_transcripter import recorder
from meeting_transcripter.common import DEFAULT_CONFIG, read_json, write_json

pytest.importorskip("faster_whisper")


def write_wav(path, seconds, rate, amplitude):
    t = np.arange(int(seconds * rate)) / rate
    samples = (amplitude * 32767 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def make_recording(tmp_path, titles, trigger="auto", seconds=90):
    folder = tmp_path / "2026-10-02_1400"
    (folder / "parts").mkdir(parents=True)
    write_wav(folder / "parts" / "001_mic.wav", seconds, 48000, 0.2)
    write_wav(folder / "parts" / "002_others.wav", seconds - 2, 48000, 0.3)
    write_json(folder / "meta.json", {
        "status": "recorded", "trigger": trigger, "apps": ["ms-teams.exe"], "window_titles": titles,
        "started_at": "2026-10-02T14:00:00-04:00", "ended_at": "2026-10-02T14:01:30-04:00",
        "parts": [{"kind": "mic", "file": "001_mic.wav", "offset": 0.0, "device": "mic"},
                  {"kind": "others", "file": "002_others.wav", "offset": 2.0, "device": "speakers"}]})
    return folder


@pytest.fixture
def fake_model(monkeypatch):
    seen = {}

    def fake_transcribe(tracks, cfg):
        seen["tracks"] = {label: len(audio) for label, audio in tracks.items()}
        return [
            {"speaker": "Jane", "start": 1.0, "end": 3.0, "text": "Hi, thanks for joining."},
            {"speaker": "Others", "start": 4.0, "end": 8.0, "text": "Can you send the budget by Friday?"},
            {"speaker": "Jane", "start": 4.2, "end": 8.1, "text": "send the budget by Friday"},  # echo
            {"speaker": "Jane", "start": 9.0, "end": 10.0, "text": "Yes, I will."},
        ], "en"

    monkeypatch.setattr(recorder, "transcribe", fake_transcribe)
    return seen


def test_finalize_writes_transcript_and_audio(tmp_path, fake_model):
    cfg = dict(DEFAULT_CONFIG, meetings_dir=str(tmp_path), me_label="Jane", summarizer="none")
    folder = make_recording(tmp_path, ["Budget review | Microsoft Teams - Microsoft Edge"])

    result = recorder.finalize(folder, cfg)

    done = tmp_path / "2026-10-02_1400_budget-review"
    assert result["folder"] == str(done)
    assert result["notes_pending"] is False
    # Both tracks are resampled to 16 kHz and aligned; the later part is offset by 2 s.
    assert fake_model["tracks"] == {"Jane": 90 * 16000, "Others": 90 * 16000}
    transcript = (done / "transcript.md").read_text(encoding="utf-8")
    assert transcript.startswith("# Budget review — Fri 2026-10-02, 14:00–14:01 (2 min)")
    lines = transcript.split("---\n\n", 1)[1].splitlines()
    assert lines == [
        "[00:00:01] Jane: Hi, thanks for joining.",
        "[00:00:04] Others: Can you send the budget by Friday?",
        "[00:00:09] Jane: Yes, I will.",
    ]
    meta = read_json(done / "meta.json")
    assert meta["status"] == "transcribed"
    assert meta["turns"] == 3
    assert meta["language"] == "en"
    assert sorted(meta["audio"]) == ["mic.opus", "others.opus"]
    assert (done / "others.opus").stat().st_size > 1000
    assert not (done / "parts").exists()


def test_finalize_marks_notes_pending(tmp_path, fake_model):
    cfg = dict(DEFAULT_CONFIG, meetings_dir=str(tmp_path), summarizer="claude", keep_audio_days=0)
    folder = make_recording(tmp_path, [], trigger="manual")
    result = recorder.finalize(folder, cfg)
    assert result["title"] == "Recording"
    assert result["notes_pending"] is True
    assert read_json(folder / "meta.json")["notes_pending"] is True
    assert not list(folder.glob("*.opus"))  # keep_audio_days 0 keeps no audio


def test_finalize_discards_short_automatic_recordings(tmp_path, fake_model):
    cfg = dict(DEFAULT_CONFIG, meetings_dir=str(tmp_path))
    folder = make_recording(tmp_path, [], seconds=20)
    assert recorder.finalize(folder, cfg) is None
    assert not folder.exists()
