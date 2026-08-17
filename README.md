# mossy

MOSS turns a Reachy Mini into a microphone and a speaker for
[MOSS-Live](https://47.116.143.69:7635/). Recorded audio is streamed to the
MOSS-Live websocket API, the speech that comes back is played on the robot, and
everything in between — voice activity detection, ASR, the LLM turn, TTS — runs
server-side. It is the same conversation the browser page gives you, moved onto
the robot.

## Runtime UI

A read-only status page (live transcript, connection state, mute) is served at
`http://<robot>:7863/`. Talking to the robot needs no browser.

![MOSS runtime UI](docs/assets/runtime-ui.png)

## Requirements

- Python 3.10+
- A Reachy Mini runtime environment
- Network access to a MOSS-Live deployment

No API keys: MOSS-Live's `/api/auth/login` issues an anonymous token.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

The defaults in `src/mossy/config.py` already point at the deployment above, so
`.env` is only needed to override something:

```bash
cp .env.example .env
```

## Run

```bash
set -a
source .env
set +a
mossy
```

To check the status page without robot hardware, start the FastAPI server on its
own:

```bash
PYTHONPATH=src python -c 'from mossy.main import Moss; Moss()._run_server()'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MOSS_LIVE_URL` | `https://47.116.143.69:7635` | MOSS-Live base URL |
| `MOSS_LIVE_VERIFY_TLS` | `0` | Verify the server certificate |
| `MOSS_LIVE_PERSONA` | server default | Persona id from `GET /api/personas` |
| `MOSS_LIVE_VOICE` | server default | Voice name from `GET /api/voices` |
| `MOSS_HALF_DUPLEX` | `1` | Close the microphone while the robot speaks |
| `MOSS_WEB_UI_PORT` | `7863` | Local status page port |
| `MOSS_AUDIO_GAIN` | `3.0` | Playback gain before clipping |

### TLS

The deployment serves a self-signed certificate (`CN=MOSS-Live`, issued by
`MOSS-Live Local CA`) for a bare IP, so verification is off by default and the
app logs a warning at startup. Set `MOSS_LIVE_VERIFY_TLS=1` once the server
presents a certificate the robot trusts.

### Network

MOSS-Live is reached directly: the client ignores `HTTP_PROXY` / `HTTPS_PROXY` /
`ALL_PROXY` from the environment rather than tunnelling a realtime audio stream
through whatever proxy the launching shell happens to export.

### Half duplex

With `MOSS_HALF_DUPLEX=1` the microphone is muted while the robot is speaking
and for 350 ms afterwards. Without it, any audio path that lacks working echo
cancellation feeds the robot's own voice back into MOSS-Live's VAD and the robot
interrupts itself.

The cost is that you cannot interrupt the robot mid-sentence. Set
`MOSS_HALF_DUPLEX=0` to get barge-in back — the interruption path is implemented
and works — on hardware whose echo cancellation is effective. Reachy Mini
wireless routes capture through the XMOS AEC loopback declared in `.asoundrc`;
setups that fall back to `autoaudiosrc` get GStreamer's `webrtcdsp` instead, and
setups with a named audio card get no cancellation at all.

## Layout

| File | Role |
| --- | --- |
| `live_client.py` | MOSS-Live websocket protocol, including playback acknowledgements |
| `audio.py` | 16 kHz mic → int16, and streaming 48 kHz → 16 kHz decimation of the reply |
| `main.py` | Reachy Mini app: microphone pump, speaker, status page |

## Checks

```bash
make check
```

`make check` runs:

- `ruff check src`
- `ruff format --check src`
- `python -m compileall -q src/mossy`

Install the local pre-commit hooks with:

```bash
pre-commit install
```
