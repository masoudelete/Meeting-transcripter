# Meeting Transcripter

A small Windows app that notices when you're on a call, records it, transcribes it **on your own computer**, and writes meeting notes: summary, decisions, action items and ready-to-send follow-up drafts.

- **Automatic.** Detects calls in the Teams app, Teams or Google Meet in the browser (Edge, Chrome, Firefox, Brave), and the Zoom app. No bot joins the meeting.
- **Local transcription.** Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`large-v3-turbo`). Fast on an NVIDIA GPU; also works on the CPU.
- **Notes your way.** Through the Claude API (best quality), a local model with [Ollama](https://ollama.com) (fully offline), or no notes at all.
- **Plain files.** Everything is Markdown and JSON in a `Meetings` folder you own.

## Install

You need 64-bit Windows 10 or 11 and about 2 GB of free disk space (4.5 GB with an NVIDIA GPU). Press <kbd>Win</kbd>+<kbd>R</kbd>, paste this and press Enter (or paste it into a terminal):

```
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/masoudelete/Meeting-transcripter/HEAD/install.ps1 | iex"
```

The installer:

1. installs Python 3.12 for your user if you don't have Python 3.10–3.14,
2. installs the app in `%LOCALAPPDATA%\MeetingTranscripter` (with GPU support when it finds an NVIDIA card),
3. asks your name and how notes should be written (see [Meeting notes](#meeting-notes)),
4. downloads the speech model (about 1.6 GB, once),
5. adds a Start menu shortcut, starts the app at sign-in, and starts it now.

It needs no administrator rights. To update, run the same command again; your settings are kept.

## Using it

A dot appears in the system tray (click the `^` arrow next to the clock if you don't see it):

| Dot | Meaning |
|---|---|
| Grey | Watching for calls |
| Red | Recording a call |
| Amber | Transcribing or writing notes |
| Grey ring | Paused: calls are not recorded |

When a call starts, a notification says "Recording this call". About 30 seconds after the call ends, the app transcribes it, then writes the notes and tells you when they're ready.

Right-click the dot for:

- **Record now / Stop and keep / Stop and discard**: for in-person meetings, other apps, or a call you don't want kept.
- **Pause automatic recording**: until you untick it or restart the app.
- **Open meetings folder**, **Settings**, **Open log**, **Quit**.

### What you get

Each call gets a folder in `%USERPROFILE%\Meetings`, named after its date, time and title:

```
Meetings\
  ACTION-ITEMS.md                      your open action items from every meeting
  2026-10-02_1400_budget-review\
    notes.md                           summary, decisions, action items, drafts
    transcript.md                      the full transcript with timestamps
    meta.json                          status and details
    mic.opus, others.opus              the audio (deleted after 14 days)
```

The transcript separates **you** (your microphone) from **everyone else** (the call audio). The notes then work out who said what from context and mark those guesses as "inferred".

An example `notes.md`:

```markdown
# Budget review

Friday 2026-10-02, 14:00–14:30 (30 min) · Q4 budget planning with finance

## Summary
- Travel spend is 12% over plan; hiring is on track.
...

## Action items
| Owner | Action | Due |
|---|---|---|
| Jane | Send the revised budget to Sam | 2026-10-08 |
| Sam | Confirm the travel freeze with finance | — |

## Drafts
### Email to Sam
To: Sam
Subject: Revised Q4 budget
...
```

## Meeting notes

Choose during installation, or later in **Settings** (`summarizer`):

| `summarizer` | How it works | Privacy |
|---|---|---|
| `"claude"` | Sends the transcript to the [Claude API](https://console.anthropic.com). Needs an API key (`anthropic_api_key`); you pay per use, roughly US$0.10–0.20 per hour of meeting with the default model. | The transcript text leaves your computer; the audio never does. |
| `"ollama"` | Uses a local model through [Ollama](https://ollama.com/download). Install Ollama, then `ollama pull llama3.1:8b`. Needs a fast PC; notes are simpler. | Nothing leaves your computer. |
| `"none"` | Transcripts only. | Nothing leaves your computer. |

If notes fail (no internet, wrong key), the notification says why. Notes that failed for a temporary reason are retried the next time the app starts, and you can retry any meeting by hand:

```
%LOCALAPPDATA%\MeetingTranscripter\venv\Scripts\python.exe -m meeting_transcripter --notes "%USERPROFILE%\Meetings\2026-10-02_1400"
```

## Settings

Right-click the tray dot → **Settings** opens `config.json` in Notepad. Changes apply as soon as you save. Delete a line to go back to its default.

| Setting | Default | What it does |
|---|---|---|
| `me_label` | your name | How your microphone is labelled in transcripts and notes |
| `meetings_dir` | `%USERPROFILE%\Meetings` | Where meetings are saved |
| `summarizer` | chosen at install | `"claude"`, `"ollama"` or `"none"` |
| `anthropic_api_key` | | Your Claude API key (or set the `ANTHROPIC_API_KEY` environment variable) |
| `claude_model` | `"claude-opus-5-5"` | Claude model for notes |
| `ollama_model` | `"llama3.1:8b"` | Ollama model for notes |
| `notes_language` | `null` | `null` writes notes in the meeting's language; or e.g. `"English"` |
| `always_record_apps` | Teams, Zoom | Apps whose use of the microphone always means a call |
| `browser_apps` | Edge, Chrome, Firefox, Brave | Browsers that count only while a meeting window is open |
| `title_keywords` | Teams, Google Meet | Window-title text that marks a browser window as a meeting |
| `model` | `"large-v3-turbo"` | Speech model. `"small"` is much faster on a CPU but less accurate |
| `device` | `"auto"` | `"cuda"` (NVIDIA GPU), `"cpu"` or `"auto"` |
| `languages` | `[]` | Limit language detection, e.g. `["en", "fr"]` |
| `language` | `null` | Force one language, e.g. `"en"` |
| `hotwords` | `""` | Names and terms the transcriber should favour, e.g. `"Acme, Kubernetes, Priya"` |
| `keep_audio_days` | `14` | Days to keep the audio; `0` keeps none |
| `min_minutes` | `1.0` | Automatic recordings shorter than this are discarded |
| `end_after_mic_released_seconds` | `30` | How long after a call releases the microphone the recording stops |

To record **Webex, Slack huddles or another app**, add its program name (e.g. `"slack.exe"`) to `always_record_apps`. To record a browser meeting app, add a word from its window title to `title_keywords`.

## Recording and consent

Laws on recording calls differ by country and state, and many require **everyone's consent**. The app does not announce itself to the other people in the call: **tell them you are recording**, follow your organization's policies, and use **Stop and discard** when someone objects.

## Troubleshooting

- **A call was not recorded.** Open the log (tray → **Open log**). When a browser uses the microphone but no meeting window is recognised, the log lists the browser's window titles; add a word from the right one to `title_keywords`.
- **The other side is missing from the transcript.** The app records everything your computer plays. Check that the call's sound comes out of your computer and not a phone.
- **Transcription is slow.** Without an NVIDIA GPU a call takes roughly as long as it lasted to transcribe. Set `model` to `"small"` for speed. If you have an NVIDIA GPU but the log says it fell back to the CPU, update the NVIDIA driver and run the installer again.
- **Your voice appears twice.** On speakers, your microphone also picks up the call. Use a headset, or rely on the built-in echo filter.
- **The app is not running.** Start **Meeting Transcripter** from the Start menu.

## Uninstall

**Windows Settings → Apps → Meeting Transcripter → Uninstall**, or run `%LOCALAPPDATA%\MeetingTranscripter\uninstall.ps1`. Your meetings are kept; pass `-RemoveMeetings` to delete them too.

## Command line

All commands use the app's own Python, `%LOCALAPPDATA%\MeetingTranscripter\venv\Scripts\python.exe`:

```
python -m meeting_transcripter                       run the tray app
python -m meeting_transcripter --test-seconds 20     record 20 s now and transcribe it
python -m meeting_transcripter --finalize DIR        transcribe a meeting folder again
python -m meeting_transcripter --notes DIR           write a meeting's notes again
python -m meeting_transcripter --set KEY VALUE       change a setting, e.g. --set summarizer ollama
```

## Agent follow-ups with Claude Code

Set `summarizer` to `"none"` and the app leaves each meeting at status `transcribed`. An agent can then pick it up, for example a scheduled task in the Claude desktop app that drafts emails, documents and tickets for every action item. See [docs/claude-code-followups.md](docs/claude-code-followups.md).

## How it works

- **Detection.** Windows records which apps are using the microphone (`HKCU\...\CapabilityAccessManager\ConsentStore\microphone`). A call is on while a meeting app holds the microphone, or while a browser does and one of its windows has a meeting title. Recording stops 30 seconds after the microphone is released.
- **Recording.** Two tracks: the default microphone, and a WASAPI loopback of every output device. It follows you when you switch headsets mid-call.
- **Transcription.** Both tracks are transcribed separately in the language detected on the mix. Low-confidence segments (Whisper's hallucinations on silence) and microphone echo of the call audio are dropped.
- **Notes.** One request to the chosen model with a JSON schema for the notes; the transcript is treated as data, never as instructions.

## Development

```
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

The tray app and recording are Windows-only; the tests run anywhere. CI also installs the app on a Windows runner with `install.ps1` and transcribes synthetic speech end to end.

## License

[MIT](LICENSE)
