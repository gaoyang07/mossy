<p align="center">
  <img src="docs/assets/mossy-logo.png" alt="mossy: a robot with two antennae and an orange M-shaped voice wave" width="220" />
</p>

<h1 align="center">mossy</h1>

Voice and visual conversations with expressive motion for Reachy Mini, powered by the
[MOSS-Live public API](https://platform.moss.cn/docs/scenarios/moss-live/).

MOSS-Live handles speech recognition, conversation and speech generation. mossy
streams the robot's microphone, optional onboard camera input and speaker audio,
manages playback and interruptions, and adds local gestures using official Reachy
motion recordings.

- Real-time voice conversation with `moss-live-1.0`.
- Optional full-duplex conversation with interruption support.
- Visual questions using frames from Reachy Mini's onboard camera.
- Happy, agreeing, thinking and listening gestures, alongside speech-driven head wobbling.
- One `mossy` command to start, stop, inspect and configure the robot.
- A local page for transcripts, connection state and measured latency, with settings for
  the voice, the persona, the robot's resting posture and the API parameters.

Vision is opt-in. When enabled, JPEG frames from the robot's camera are sent to
MOSS-Live on the same connection as audio. The app reads the Reachy SDK camera
feed; it does not select a laptop webcam as a fallback.

## Quick start

These steps are for macOS with Reachy Mini Control installed, the tested setup: Reachy
Mini Lite, Python 3.12 and Reachy Mini SDK 1.8.4. Package metadata allows Python 3.10+,
while CI uses Python 3.12. For other setups see [Running in a terminal](#running-in-a-terminal).

### 1. Prepare the robot

Connect the robot and switch on its independent power supply before starting anything
else: the Reachy daemon looks for the robot's camera and audio card once, when it starts.
Open Reachy Mini Control once so that it sets up its Python environments; it does not have
to stay open. Stop any other app using the robot.

See the [official Reachy installation guide](https://huggingface.co/docs/reachy_mini/SDK/installation)
for SDK, media dependencies and platform permissions.

### 2. Install mossy

From the repository root, with any Python 3.10 or newer:

```bash
PYTHONPATH=src python3 -m mossy.cli install
```

This installs mossy into Reachy Mini Control's app environment using the `uv` that Control
ships, downloads the gesture recordings, and links the `mossy` command into `~/.local/bin`.
Add `--editable` to run straight from this checkout while developing. Run it again after
pulling new code; `mossy doctor` tells you when the installed copy is out of date.

### 3. Configure the API key

```bash
mossy config set MOSS_API_KEY=your-key
```

Settings are kept in `~/.config/mossy/.env`, readable only by you. The app loads them in
this order (highest priority first):

1. Exported environment variables.
2. `~/.config/mossy/.env`.
3. `.env` in the process's working directory.
4. Defaults in [config.py](src/mossy/config.py).

A valid MOSS API key is required. It is sent in the WebSocket Authorization
header, and is never returned by the page or printed by the command. `.env.example`
lists every setting and contains no key.

### 4. Start talking

```bash
mossy start
```

This starts the Reachy daemon if it is not running, starts mossy, waits until the robot is
connected to MOSS-Live and opens <http://127.0.0.1:7863/>. Double-clicking
`start-mossy.command` in Finder does the same. Talking to the robot does not require
keeping the page open.

```bash
mossy stop
```

puts the robot to sleep, releases its motors and closes the MOSS-Live session. MOSS-Live
bills from session attachment until the WebSocket closes; muting or closing the browser
keeps the session connected. To end forgotten sessions automatically, set
`MOSS_IDLE_SHUTDOWN_MIN`. See the
[MOSS-Live guide](https://platform.moss.cn/docs/scenarios/moss-live/) for session and
billing behaviour.

## The mossy command

| Command | What it does |
| --- | --- |
| `mossy start [--no-browser]` | Start the daemon and mossy, or reuse a healthy running conversation |
| `mossy stop [--daemon]`, `mossy end` | Stop mossy; `--daemon` also stops a daemon that `mossy start` launched |
| `mossy restart` | Restart mossy to load new code or settings |
| `mossy status [--json]` | Daemon, app, session and measured latency; exits non-zero when not connected |
| `mossy gui` | Open the page on its settings, starting mossy first if needed |
| `mossy config [show\|get\|set\|unset\|path\|edit]` | Show or change settings; a running conversation applies them at once |
| `mossy voices [--use ID-or-name]` | List the account's voices, or switch to one |
| `mossy logs [-f] [-n N] [--clean]` | Daemon and app log; `--clean` deletes all but the newest |
| `mossy doctor` | Check the installation and say what to fix |
| `mossy install [--editable]` | Install into Control's app environment and link the command |
| `mossy run` | Run the app in this terminal, for development |
| `mossy help [command]` | Help |

The command talks to the Reachy daemon's local HTTP API. It only stops an app named
`mossy`, and only signals a daemon process that it started itself.

## Settings page

`mossy gui` opens the page with its settings panel. The panel is shown only to a browser
on the machine running mossy. From another machine the page shows the status and the
transcript; every control, including microphone mute and the gesture switch, answers
only a local browser.

<p align="center">
  <img src="docs/assets/runtime-ui.png" alt="The mossy page: status, camera preview and transcript on the left, settings on the right" width="760" />
</p>

| Group | What it holds |
| --- | --- |
| Voice and persona | Voice (the account's voices, the two platform presets, or any voice ID), speaking rate, synthesis volume, assistant name, persona prompt, session instructions, time zone |
| Conversation | Half or full duplex, local playback gain, idle shutdown |
| Robot | Resting posture, gestures on or off and their amplitude, wake-up animation, gesture preview |
| Vision | Camera input, frame rate, image size and quality |
| Connection | API key, API origin, model, user ID, TLS verification, downlink sample rate, page port |

Changes apply to the running conversation and are saved to `~/.config/mossy/.env`:

- Voice, rate, volume, persona, instructions and time zone are sent to MOSS-Live and
  saved only after the server confirms them. A voice or prompt the server refuses is
  reported and not saved, so it cannot break the next start.
- Connection settings open a new MOSS-Live connection and session.
- The page port and the wake-up animation take effect at the next start.

A setting fixed by an exported environment variable is shown but cannot be changed.
The three buttons above the transcript are quick switches for the current run; the
panel's switches are the saved defaults.

## Launching from Reachy Mini Control

`mossy install` puts mossy into the app environment that Reachy Mini Control uses, so it
also appears in Control's installed apps list and in the running daemon's dashboard at
<http://127.0.0.1:8000/>. Starting it there is equivalent to `mossy start`. Stopping it
there also ends the conversation, but the daemon then moves the robot back to its upright
zero pose with the motors on, as it does after stopping any app. `mossy stop` and the
page's stop button ask mossy to exit by itself, which leaves the robot asleep with its
motors released. On other Reachy deployment types, install into the matching app
environment with `uv pip install --python <its python> .`.

## Running in a terminal

For development without Reachy Mini Control's environments, start a Reachy daemon
yourself, then from the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mossy config set MOSS_API_KEY=your-key
mossy run
```

`mossy run` keeps the app in the foreground; stop it with `Ctrl+C`. Gestures need the
recordings in the Hugging Face cache of the same OS user:

```bash
python - <<'PY'
from huggingface_hub import snapshot_download
from mossy.expressions import LIBRARIES

for dataset, (revision, _) in LIBRARIES.items():
    snapshot_download(dataset, repo_type="dataset", revision=revision)
PY
```

Expressions load recordings from local cache only. Missing recordings make expressions
unavailable while voice continues to work. To use voice alone, set `MOSS_EXPRESSIONS=0`.

## Voice, vision and expressions

### Conversation mode

The default is **half duplex** (`MOSS_HALF_DUPLEX=1`): while the robot speaks, and
for 350 ms afterwards, microphone input is replaced with silence. Manual mute
also sends silence so the realtime input stream keeps running.

For **full duplex**, switch it off on the settings page or run:

```bash
mossy config set MOSS_HALF_DUPLEX=0
```

The microphone then remains active during replies, allowing you to interrupt.
If speaker echo causes the robot to interrupt itself, use half duplex while
checking the audio setup. Echo handling depends on the Reachy runtime and device.

### Visual questions

Click **开启视觉** on the local status page, or set `MOSS_CAMERA=1` in the local
configuration and restart mossy. Point Reachy Mini at an object and ask a question
aloud, for example “你面前是什么，是什么颜色？”. The page shows the robot's current
view and the number of frames sent to MOSS.

The default is 1 frame per second, JPEG quality 80 and a maximum image edge of
640 pixels. Capture and encoding run separately from audio; only the latest frame
is queued and old frames are dropped. Audio stays active when visual input is
disabled. Frames are kept in memory for preview and are not saved by the app.

Preview and camera controls are available only from a local browser on the machine
running mossy. **关闭视觉** stops frame uploads and clears the preview; it does not
turn off the camera hardware used by the Reachy daemon. The browser switch affects
the current process; use `MOSS_CAMERA` for the persistent setting.

On macOS, the app launching the Reachy daemon needs Camera permission even for
the robot's USB camera. Allow the relevant launcher, such as Reachy Mini Control
or Terminal, in System Settings → Privacy & Security → Camera. Restart the daemon
from that authorized launcher if it previously failed to open the camera. Camera
failure leaves voice available and is shown in the status page.

### Expression behaviour

| Behaviour | Trigger | Official recording |
| --- | --- | --- |
| Happy | Explicit positive replies such as “太好了！” or “恭喜你…” | `groovy_sway_and_roll` |
| Agree | Acknowledgements such as “好的…” or “没问题。” | `simple_nod` |
| Thinking | A pause after the user's transcription, or “让我想想。” | `thoughtful1` |
| Listening | User transcription while the assistant is not speaking | `attentive1` |

Reply gestures use conservative rules on the first complete Chinese sentence,
with a final short reply also eligible. Questions, quoted speech and matched
negative expressions stay neutral. The rules cover explicit expressions;
sarcasm and nuanced sentiment are outside their current coverage.

Gestures are short, amplitude-limited and subject to cooldowns. A reply gesture
can start once that response's local audio begins playing, at most once per
response; late triggers are skipped. Interruptions cancel the matching gesture
and return its target toward neutral; old response events do not cancel a newer
response's gesture. Recorded
sound effects are not played over the conversation.

The page's expression toggle applies to the current process and keeps voice and
speech wobbling active. For a persistent change, use the settings panel or
`MOSS_EXPRESSIONS=0`. Motion worker errors are shown in `/state`; voice can continue without
expressions. Exact limits and dataset revisions live in
[expressions.py](src/mossy/expressions.py).

### Resting posture

`MOSS_POSTURE` is the pose the robot holds between gestures and returns to after each
one: `neutral` (the SDK's pose), `attentive` (head slightly up, antennas upright),
`relaxed` (head slightly down, antennas lowered) or `curious` (head tilted). Each stays
within 8 degrees, 4 mm and 0.35 rad of antenna from the neutral pose, and gestures are
applied relative to it. Changing it on the settings page moves the robot at once.

## Configuration reference

`mossy config set` and the settings page change these for the running conversation and
save them. After editing the file by hand, run `mossy restart`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MOSS_API_KEY` | required | API credential |
| `MOSS_HALF_DUPLEX` | `1` | `0` enables listening during replies |
| `MOSS_IDLE_SHUTDOWN_MIN` | `0` | Minutes without anyone speaking before the robot sleeps and the session closes; `0` never |
| `MOSS_POSTURE` | `neutral` | Resting posture: `neutral`, `attentive`, `relaxed` or `curious` |
| `MOSS_WAKE_ANIMATION` | `1` | `0` skips the wake-up sound and head wiggle, starting about 1.5 s sooner |
| `MOSS_EXPRESSIONS` | `1` | Enable conversation gestures |
| `MOSS_EXPRESSION_INTENSITY` | `0.45` | Gesture amplitude, from `0` to `1` |
| `MOSS_CAMERA` | `0` | Send the robot's camera frames to MOSS-Live |
| `MOSS_CAMERA_FPS` | `1` | Capture/send rate, from `0.5` to `5` FPS |
| `MOSS_CAMERA_MAX_EDGE` | `640` | Maximum JPEG width or height, from `128` to `1920` pixels |
| `MOSS_CAMERA_JPEG_QUALITY` | `80` | JPEG quality, from `1` to `95` |
| `MOSS_AUDIO_GAIN` | `1.0` | Software playback gain before clipping |
| `MOSS_WEB_UI_PORT` | `7863` | Status page port on the machine running mossy |
| `MOSS_LIVE_NAME` | `MOSS` | Assistant name used for address recognition |
| `MOSS_LIVE_PROMPT` | empty | Optional replacement persona prompt |
| `MOSS_LIVE_INSTRUCTIONS` | `请用简洁自然的中文回答。` | Session instructions |
| `MOSS_LIVE_URL` | `https://api.mosi.cn` | API origin; the client adds `/v1/realtime` |
| `MOSS_LIVE_MODEL` | `moss-live-1.0` | Model query parameter |
| `MOSS_LIVE_VOICE_ID` | empty | Optional platform voice ID; wait for `voice_changed` before enabling input, and reapply on reconnect |
| `MOSS_LIVE_SPEED` | `1.0` | Speaking rate, from `0.5` to `1.5` |
| `MOSS_LIVE_VOLUME_DB` | `0` | Synthesis gain in dB, from `-20` to `16`; applied by the server |
| `MOSS_LIVE_OUTPUT_RATE` | `16000` | Downlink PCM rate. `16000` matches the robot's playback rate; `48000` is the server default and is resampled locally |
| `MOSS_TIME_ZONE` | `auto` | IANA time zone sent for questions about the time; `auto` uses this machine's, empty sends none and the server assumes Beijing time |
| `MOSS_LIVE_USER` | `reachy-mini` | Stable user ID used on reconnect |
| `MOSS_LIVE_VERIFY_TLS` | `1` | Verify the API server certificate |

Setting only the assistant name retains the server's default persona prompt.
See [.env.example](.env.example) for the complete template, including local proxy
bypass settings.

## Latency

`mossy status` and the page's footer report what this app measures:

| Figure | Meaning |
| --- | --- |
| Reply | From the end of the user's speech, as MOSS-Live reports it (`input.speech.stopped`, or the final transcript if the server sends no speech events), to the first reply audio arriving at this app. It includes the network both ways and the server's work, and excludes the audio device's output latency |
| Round trip | WebSocket ping time to MOSS-Live, refreshed every 5 s |
| Handshake | Dialing to microphone open for the current connection |
| Startup | App launch to ready to talk |
| Speech breaks | Reply chunks that arrived after the audio before them had finished, and the longest such gap |

Reply audio is handed to the audio device as it arrives, and playback receipts are sent
on a clock derived from PCM duration, because the Reachy SDK exposes enqueue and no
playback-completion callback. The clock does not measure speaker output latency.

Two things outside this app affect latency and are worth checking:

- **Audio path.** SDK 1.8.4 plays and records on the audio device directly only when the
  daemon found the robot's camera at its own start. Otherwise both directions take a
  WebRTC detour through the daemon, with an Opus encode and decode each way and a
  blocking request on every interruption. `mossy status` shows which path is in use. To
  get the direct path back, power the robot and run `mossy stop --daemon && mossy start`.
- **Connection rotation.** MOSS-Live closes each connection on a schedule, every 10
  minutes in our logs; mossy resumes the same session on a new one. The robot does not
  hear or speak for the length of one handshake.

## Troubleshooting and current limits

Start with `mossy doctor`, and `mossy logs` for the daemon and app output.

| Symptom | What to check |
| --- | --- |
| Cannot connect to the robot | Confirm the daemon is running and the robot has independent power. The local dashboard is a separate service on port `8000`. |
| Local SDK connection fails with a proxy enabled | Include `localhost,127.0.0.1,::1` in `NO_PROXY` and `no_proxy`; preserve any other bypass hosts you need. The MOSS API connection itself uses a direct WebSocket connection. |
| No reply sound | Check the robot output device and its speaker volume in the daemon dashboard. `MOSS_AUDIO_GAIN` is software gain, not the device volume. |
| Expressions are unavailable | Run `mossy install` again as the app's OS user, which downloads the recordings, and inspect `expressions.error` in `/state`. |
| Visual input shows no frames | Check Reachy's camera connection and the daemon launcher's Camera permission. `camera.captured_frames` reports capture, while `camera.sent_frames` reports uploads; voice can remain connected with neither. |
| Control is stuck on a permission screen | Follow the runtime's platform permission setup. The browser dashboard can manage apps only if the daemon is already running; opening it does not start the daemon. |

Gesture pose limits apply before the SDK adds speech-wobble offsets;
this app does not calibrate the robot's physical neutral pose.

## Development

Install development tools in the active environment and run the checks:

```bash
python -m pip install -e ".[dev]"
make check
```

`make check` runs Ruff lint and format checks, unit tests and Python compilation.
Tests import the checkout through `PYTHONPATH=src` and use simulated robot output and a
local WebSocket peer; they need no API key, robot, media stack or downloaded recordings.
[test_app.py](tests/test_app.py) runs the whole app against a simulated robot and is
skipped where the Reachy SDK is not installed.

GitHub Actions runs two jobs on Python 3.12. `lint` runs `make check` with only Ruff,
NumPy, SciPy, Pillow, python-dotenv and WebSockets installed, for a quick answer. `app`
installs mossy with Reachy SDK 1.8.4, the version it is tested against, and runs every
test, including the whole-app ones. Hardware checks are separate from CI.

To install the local Git hooks:

```bash
pre-commit install
```

| File | Responsibility |
| --- | --- |
| [cli.py](src/mossy/cli.py) | The `mossy` command: daemon and app lifecycle, status, settings; standard library only |
| [config.py](src/mossy/config.py) | The settings schema shared by the app, the command and the page |
| [live_client.py](src/mossy/live_client.py) | Public MOSS-Live protocol, reconnects, playback receipts and latency figures |
| [audio.py](src/mossy/audio.py) | Convert 16 kHz microphone input and replies for Reachy playback |
| [camera.py](src/mossy/camera.py) | Encode robot BGR frames, manage visual input and local preview |
| [expressions.py](src/mossy/expressions.py) | Expression rules, resting postures, motion assets and the cancellable worker |
| [main.py](src/mossy/main.py) | Robot lifecycle, audio capture/playback, settings and the page's API |
| [static/](src/mossy/static/) | The page: transcript, status and settings |
| [tests/](tests/) | Protocol, expression, settings, command and whole-app regression tests |

[Logo design brief and visual references](docs/assets/mossy-logo.md).
