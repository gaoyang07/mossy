# mossy

MOSS turns a Reachy Mini into a microphone and a speaker for
[MOSS-Live](https://platform.moss.cn/docs/scenarios/moss-live/). Recorded audio is streamed to the
MOSS-Live websocket API, the speech that comes back is played on the robot, and
everything in between — voice activity detection, ASR, the LLM turn, TTS — runs
server-side. It is the same conversation the browser page gives you, moved onto
the robot.

## Runtime UI

A runtime status page (live transcript, connection state, mute and expression controls) is served at
`http://<robot>:7863/`. Talking to the robot needs no browser.

![MOSS runtime UI](docs/assets/runtime-ui.png)

## Requirements

- Python 3.10+
- A Reachy Mini runtime environment
- Network access to a MOSS-Live deployment

A MOSS API key is required. The client uses the public `moss-live-1.0` model at
`wss://api.mosi.cn/v1/realtime` with Bearer authentication in the handshake header.
It waits for successful `session.update` configuration before enabling audio.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

For use from Reachy Mini Control, save the key outside the repository:

```bash
mkdir -p ~/.config/mossy
chmod 700 ~/.config/mossy
# On first setup, copy the template, then set MOSS_API_KEY in the file.
cp -n .env.example ~/.config/mossy/.env
chmod 600 ~/.config/mossy/.env
```

## Run

```bash
mossy
```

The app reads `~/.config/mossy/.env` automatically, including when launched from
the desktop app. Exported environment variables take priority, followed by that
file, followed by `.env` in the working directory. Keep API keys out of source
control; the runtime status page never returns them.

To check the status page without robot hardware, with the API key configured:

```bash
PYTHONPATH=src python -c 'from mossy.main import Moss; Moss()._run_server()'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MOSS_API_KEY` | required | API key, sent only in the Authorization header |
| `MOSS_LIVE_URL` | `https://api.mosi.cn` | MOSS-Live API origin |
| `MOSS_LIVE_MODEL` | `moss-live-1.0` | Realtime model |
| `MOSS_LIVE_USER` | `reachy-mini` | Stable user ID retained on reconnect |
| `MOSS_LIVE_VERIFY_TLS` | `1` | Verify the server certificate |
| `MOSS_LIVE_NAME` | `MOSS` | Assistant name |
| `MOSS_LIVE_PROMPT` | server default | Optional replacement persona prompt |
| `MOSS_LIVE_INSTRUCTIONS` | concise Chinese replies | Session instructions |
| `MOSS_HALF_DUPLEX` | `1` | Close the microphone while the robot speaks |
| `MOSS_WEB_UI_PORT` | `7863` | Local status page port |
| `MOSS_AUDIO_GAIN` | `1.0` | Playback gain before clipping |
| `MOSS_EXPRESSIONS` | `1` | Enable conversation gestures |
| `MOSS_EXPRESSION_INTENSITY` | `0.45` | Gesture amplitude, between 0 and 1 |

### TLS

Certificate verification is enabled by default for the public API.

### Network

MOSS-Live is reached directly: the client ignores `HTTP_PROXY` / `HTTPS_PROXY` /
`ALL_PROXY` from the environment rather than tunnelling a realtime audio stream
through whatever proxy the launching shell happens to export.

Reachy's SDK has a separate local WebSocket connection. If the desktop machine
uses a system or shell proxy, add `NO_PROXY=localhost,127.0.0.1,::1` and
`no_proxy=localhost,127.0.0.1,::1` to the app's `.env` so SDK traffic reaches the
local daemon directly. Preserve any other hosts your environment needs to bypass.

### Half duplex

With `MOSS_HALF_DUPLEX=1` the microphone sends silence while the robot is speaking
and for 350 ms afterwards. Manual mute also sends silence, keeping the realtime
input clock running. Without half duplex, any audio path that lacks working echo
cancellation feeds the robot's own voice back into MOSS-Live's VAD and the robot
interrupts itself.

The cost is that you cannot interrupt the robot mid-sentence. Set
`MOSS_HALF_DUPLEX=0` to get barge-in back — the interruption path is implemented
and works — on hardware whose echo cancellation is effective. Reachy Mini
wireless routes capture through the XMOS AEC loopback declared in `.asoundrc`;
setups that fall back to `autoaudiosrc` get GStreamer's `webrtcdsp` instead, and
setups with a named audio card do not get the SDK's software AEC path.

## Layout

| File | Role |
| --- | --- |
| `live_client.py` | MOSS-Live websocket protocol, including playback acknowledgements |
| `audio.py` | 16 kHz mic → int16, and streaming 48 kHz → 16 kHz decimation of the reply |
| `main.py` | Reachy Mini app: microphone pump, speaker, status page |
| `expressions.py` | Chinese expression rules and a cancellable motion worker |

### Conversation expressions

The app uses official Reachy motion recordings for four behaviours:

| Behaviour | Trigger | Recording |
| --- | --- | --- |
| Happy | Explicit positive reply such as “太好了！” or “恭喜你…” | `groovy_sway_and_roll` |
| Agree | Explicit acknowledgement such as “好的…” or “没问题。” | `simple_nod` |
| Thinking | Waiting at least 0.8 s after user transcription, or “让我想想。” | `thoughtful1` |
| Listening | User transcription while the assistant is not speaking | `attentive1` |

Chinese classification uses the first complete sentence (or a final short
reply). Questions, quoted speech, negation and contrasting language stay
neutral. This is a conservative rule set; it does not infer arbitrary emotion,
sarcasm or prosody. Replies continue normally when no rule matches.

Reply gestures wait for the corresponding local audio playback to start, run at
most once per `run_id`/`turn_id`, and are dropped if classification arrives more
than 1.5 s after playback starts. Reply gestures have a 4 s cooldown; listening
and thinking each have an 8 s cooldown. Full-duplex backchannels do not replace
an active response gesture. A server interruption cancels only that response's
gesture, and a disconnect clears pending gestures.

A single worker samples poses at up to 50 Hz. It plays at the recordings' original
speed, taking at most 2.4 s for reply gestures or 1.6 s for listening/thinking,
then returns smoothly over 350 ms. Base head rotation is capped at 10 degrees,
translation at 10 mm per axis and antenna deviation at 0.65 radians. These are
gesture bounds before the SDK adds speech-wobble offsets. Gestures do not change
body yaw. Sidecar sound effects are never played, and cancellation does not call
`cancel_move()` or stop the shared audio pipeline.

The runtime page shows the current gesture and has an independent expression
toggle. Turning gestures off retains voice and speech wobbling. For a persistent
disable, set `MOSS_EXPRESSIONS=0` and restart the app. `/state` includes expression
availability, the last gesture, start/cancel counts and any motion-worker error.
An unavailable motion library or a failed motion worker leaves voice running.

Recordings are loaded from local Hugging Face cache only, pinned to the revisions
tested with SDK 1.8.4. On another machine, preload them once before starting:

```bash
python - <<'PY'
from huggingface_hub import snapshot_download
from mossy.expressions import LIBRARIES
for dataset, (revision, _) in LIBRARIES.items():
    snapshot_download(dataset, repo_type="dataset", revision=revision)
PY
```

### Playback and billing

The app pairs `response.audio.context` with PCM chunks and sends
`playback.chunk.completed`, `playback.completed`, and `playback.stopped` receipts.
The Reachy SDK exposes audio enqueueing but no hardware playback-completion
callback, so completion timing is estimated from PCM duration. Hardware output
latency is not measured by these receipts.

This app currently uses voice only. It does not upload camera frames.
MOSS bills an attached session by connection time. Muting does not close the
connection; stop the app when finished.

### macOS without Location permission

Reachy Mini Control 0.9.37 includes Location in its desktop startup permission
gate for Wi-Fi discovery. For a USB-connected robot, keep Location disabled and
use the already-running daemon's dashboard at `http://127.0.0.1:8000/` to start
and stop installed apps. Camera, microphone, and local-network permissions are
still managed by macOS. The robot also needs its independent power supply on.

## Checks

```bash
make check
```

`make check` runs:

- `ruff check src tests`
- `ruff format --check src tests`
- `python -m unittest discover -s tests -v` (local WebSocket protocol tests)
- `python -m compileall -q src/mossy`

The GitHub workflow runs the same checks with only Ruff, NumPy, SciPy and
WebSockets installed. Tests import the checkout through `PYTHONPATH=src`, use a
local WebSocket peer and simulated robot output, and require no API key, robot,
GStreamer installation or downloaded motion assets.

Install the local pre-commit hooks with:

```bash
pre-commit install
```
