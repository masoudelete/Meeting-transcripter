import http.server
import json
import threading
from types import SimpleNamespace

import pytest

from meeting_transcripter import summarize
from meeting_transcripter.common import DEFAULT_CONFIG, read_json, write_json

TRANSCRIPT = """# Call — Fri 2026-10-02, 14:00–14:30 (30 min)

---

[00:00:01] Jane Doe: Hi Sam, thanks for joining.
[00:00:05] Others: Thanks Jane. Can you send me the budget by Friday?
[00:00:09] Jane Doe: Yes, I will send it on Thursday.
"""

NOTES = {
    "title": "Budget check-in",
    "context": "Budget planning with Sam",
    "summary": ["Sam asked for the budget."],
    "decisions": ["Budget goes out this week."],
    "action_items": [
        {"owner": "Jane Doe", "action": "Send the budget to Sam", "due": "2026-10-08", "mine": True},
        {"owner": "Sam", "action": "Review | approve the budget", "due": "", "mine": False},
    ],
    "open_questions": [],
    "attendees": [{"name": "Jane Doe", "inferred": False}, {"name": "Sam", "inferred": True}],
    "drafts": [{"title": "Email to Sam", "text": "To: Sam\nSubject: Budget\n\nHi Sam,\n\nAttached.\n\nJane Doe"}],
    "transcript_doubts": [],
}


@pytest.fixture
def cfg(tmp_path):
    return dict(DEFAULT_CONFIG, meetings_dir=str(tmp_path), me_label="Jane Doe", summarizer="claude")


def make_meeting(tmp_path, name="2026-10-02_1400", turns=3):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
    write_json(folder / "meta.json", {
        "status": "transcribed", "trigger": "auto", "apps": ["ms-teams.exe"], "window_titles": [],
        "started_at": "2026-10-02T14:00:00-04:00", "ended_at": "2026-10-02T14:30:00-04:00",
        "duration_min": 30.0, "title_hint": None, "turns": turns, "notes_pending": True})
    return folder


def test_process_writes_notes_and_action_items(tmp_path, cfg):
    folder = make_meeting(tmp_path)
    calls = []

    def fake_ask(system, user, cfg):
        calls.append((system, user))
        return NOTES, "Test model"

    result = summarize.process(folder, cfg, ask=fake_ask)

    renamed = tmp_path / "2026-10-02_1400_budget-check-in"
    assert result["folder"] == str(renamed)
    assert not folder.exists()
    meta = read_json(renamed / "meta.json")
    assert meta["status"] == "processed"
    assert meta["title"] == "Budget check-in"
    assert meta["notes_pending"] is False
    notes = (renamed / "notes.md").read_text(encoding="utf-8")
    assert notes.startswith("# Budget check-in\n")
    assert "| Sam | Review \\| approve the budget | — |" in notes
    assert "Sam (inferred)" in notes
    assert "### Email to Sam" in notes
    items = (tmp_path / "ACTION-ITEMS.md").read_text(encoding="utf-8")
    assert items.splitlines()[-1] == "- [ ] 2026-10-08 | Send the budget to Sam | 2026-10-02_1400_budget-check-in"
    assert "Review" not in items  # not the user's item

    system, user = calls[0]
    assert '"Jane Doe" are Jane Doe\'s own microphone' in system
    assert "<transcript>" in user and "Can you send me the budget" in user


def test_action_items_are_not_duplicated(tmp_path, cfg):
    for name in ("2026-10-02_1400", "2026-10-02_1500"):
        summarize.process(make_meeting(tmp_path, name), cfg, ask=lambda s, u, c: (NOTES, "Test"))
    summarize.process(tmp_path / "2026-10-02_1400_budget-check-in", cfg, ask=lambda s, u, c: (NOTES, "Test"))
    lines = (tmp_path / "ACTION-ITEMS.md").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Open action items"
    assert len([line for line in lines if line.startswith("- [ ]")]) == 2


def test_named_folder_is_not_renamed(tmp_path, cfg):
    folder = make_meeting(tmp_path, "2026-10-02_1400_weekly-sync")
    result = summarize.process(folder, cfg, ask=lambda s, u, c: (NOTES, "Test"))
    assert result["folder"] == str(folder)


def test_silent_recording_skips_the_model(tmp_path, cfg):
    folder = make_meeting(tmp_path, turns=0)

    def must_not_run(*args):
        raise AssertionError("model called for an empty transcript")

    result = summarize.process(folder, cfg, ask=must_not_run)
    notes = (tmp_path / "2026-10-02_1400_recording" / "notes.md").read_text(encoding="utf-8")
    assert result["title"] == "Recording"
    assert "No speech was detected" in notes


def test_failure_marks_meeting_failed(tmp_path, cfg):
    folder = make_meeting(tmp_path)

    def offline(*args):
        raise summarize.NotesError("could not reach the Claude API (offline?)", retry=True)

    assert summarize.process(folder, cfg, ask=offline) is None
    meta = read_json(folder / "meta.json")
    assert meta["status"] == "failed"
    assert meta["error"] == "could not reach the Claude API (offline?)"
    assert meta["notes_pending"] is True  # retried on the next start


def test_missing_summarizer_is_reported(tmp_path, cfg):
    folder = make_meeting(tmp_path)
    assert summarize.process(folder, dict(cfg, summarizer="none")) is None
    meta = read_json(folder / "meta.json")
    assert "no summarizer set up" in meta["error"]
    assert meta["notes_pending"] is False


def test_normalize_fills_gaps():
    notes = summarize.normalize({"title": "X", "action_items": [{"action": "Do it"}, {"owner": "A"}],
                                 "summary": ["", "point"], "drafts": [{"title": "t", "text": ""}]})
    assert notes["summary"] == ["point"]
    assert notes["action_items"] == [{"owner": "unknown", "action": "Do it", "due": "", "mine": False}]
    assert notes["drafts"] == []
    assert notes["decisions"] == []


def test_prompt_without_a_name(cfg):
    meta = {"started_at": "2026-10-02T14:00:00", "ended_at": "2026-10-02T14:30:00", "duration_min": 30}
    system, user = summarize.build_prompt("text", meta, dict(cfg, me_label="Me", notes_language="French"))
    assert "notes for the user" in system
    assert 'signed "[your name]"' in system
    assert "Write everything in French." in system
    assert user.startswith("Meeting date: Friday 2026-10-02, 14:00–14:30 (30 min)")


def test_ask_claude_request(monkeypatch, cfg):
    import anthropic
    sent = {}

    class FakeMessages:
        def create(self, **kwargs):
            sent.update(kwargs)
            text = SimpleNamespace(type="text", text=json.dumps(NOTES))
            return SimpleNamespace(stop_reason="end_turn", content=[text], model=kwargs["model"])

    class FakeClient:
        def __init__(self, api_key=None, max_retries=None):
            sent["api_key"] = api_key
            self.beta = SimpleNamespace(messages=FakeMessages())

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    data, written_by = summarize.ask_claude("system", "user", dict(cfg, anthropic_api_key="sk-test"))
    assert data == NOTES
    assert written_by == "Claude (claude-opus-5-5)"
    assert sent["api_key"] == "sk-test"
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": summarize.NOTES_SCHEMA}
    assert sent["fallbacks"] == "default"
    assert sent["betas"] == ["server-side-fallback-2026-07-01"]

    sent.clear()
    summarize.ask_claude("system", "user", dict(cfg, claude_model="claude-haiku-4-5"))
    assert "effort" not in sent["output_config"]
    assert "fallbacks" not in sent


def test_ask_claude_refusal(monkeypatch, cfg):
    import anthropic

    class FakeClient:
        def __init__(self, **kwargs):
            refuse = lambda **kw: SimpleNamespace(stop_reason="refusal", content=[], model=kw["model"])
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=refuse))

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    with pytest.raises(summarize.NotesError, match="declined"):
        summarize.ask_claude("system", "user", cfg)


def test_ask_ollama_request(cfg):
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received["path"] = self.path
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = json.dumps({"message": {"role": "assistant", "content": json.dumps(NOTES)}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        data, written_by = summarize.ask_ollama("system", "user", dict(cfg, ollama_url=url))
    finally:
        server.shutdown()
    assert data == NOTES
    assert written_by == "Ollama (llama3.1:8b)"
    assert received["path"] == "/api/chat"
    assert received["body"]["format"] == summarize.NOTES_SCHEMA
    assert received["body"]["stream"] is False
    assert received["body"]["messages"][0] == {"role": "system", "content": "system"}


def test_ask_ollama_not_running(cfg):
    with pytest.raises(summarize.NotesError, match="is it running") as info:
        summarize.ask_ollama("system", "user", dict(cfg, ollama_url="http://127.0.0.1:9"))
    assert info.value.retry
