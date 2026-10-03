from meeting_transcripter.common import DEFAULT_CONFIG, hms
from meeting_transcripter.recorder import drop_echo, meeting_name, merge_turns, model_attempts, parse_setting


def test_meeting_name_from_teams_titles():
    assert meeting_name("Weekly sync | Microsoft Teams - Work - Microsoft Edge") == "Weekly sync"
    assert meeting_name("(3) Budget review | Microsoft Teams") == "Budget review"
    assert meeting_name("Chat | Microsoft Teams") is None
    assert meeting_name("Microsoft Teams") is None


def test_meeting_name_ignores_other_apps():
    assert meeting_name("Meet - abc-defg-hij - Google Chrome") is None
    assert meeting_name("Zoom Meeting") is None


def seg(speaker, start, end, text):
    return {"speaker": speaker, "start": start, "end": end, "text": text}


def test_drop_echo_removes_mic_copies_of_call_audio():
    theirs = [seg("Others", 10, 14, "please send the tabletop report by Thursday")]
    echo = seg("Me", 10.5, 14.2, "send the tabletop report by Thursday")
    own = seg("Me", 20, 22, "sure, I will do that today")
    assert drop_echo([echo, own], theirs) == [own]


def test_drop_echo_keeps_short_answers():
    theirs = [seg("Others", 0, 2, "yes okay")]
    mine = [seg("Me", 0.5, 1, "yes okay")]
    assert drop_echo(mine, theirs) == mine


def test_merge_turns_joins_close_segments_of_one_speaker():
    turns = merge_turns([seg("Me", 0, 2, "Hello"), seg("Me", 2.5, 4, "everyone."),
                         seg("Others", 5, 6, "Hi!"), seg("Me", 9, 10, "Let's start.")])
    assert [(t["speaker"], t["text"]) for t in turns] == [
        ("Me", "Hello everyone."), ("Others", "Hi!"), ("Me", "Let's start.")]


def test_hms():
    assert hms(0) == "00:00:00"
    assert hms(3725.9) == "01:02:05"


def test_model_attempts_cpu():
    cfg = dict(DEFAULT_CONFIG, device="cpu")
    assert model_attempts(cfg) == [("cpu", "int8")]


def test_model_attempts_gpu_falls_back_to_cpu():
    cfg = dict(DEFAULT_CONFIG, device="cuda")
    assert model_attempts(cfg) == [("cuda", "float16"), ("cpu", "int8")]


def test_parse_setting():
    assert parse_setting("0") == 0
    assert parse_setting('["en", "fr"]') == ["en", "fr"]
    assert parse_setting("Jane Doe") == "Jane Doe"
    assert parse_setting("claude") == "claude"
