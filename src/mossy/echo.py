"""Enable the Reachy board's echo processing and the local WebRTC AEC.

The SDK enables software AEC only when the Reachy audio card is absent. For
the LOCAL backend, insert it before capture reaches the app and feed it the
mixed speaker signal in the same GstPipeline. Configure before audio starts.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# These are switches/modes, not maximum suppression or gain settings. In
# particular, AEC is enabled with BYPASS=0; NLAEC_MODE=0 is normal operation.
HARDWARE_ECHO_CONFIG = (
    ("SHF_BYPASS", (0,)),
    ("PP_ECHOONOFF", (1,)),
    ("PP_NLATTENONOFF", (1,)),
    ("PP_NLAEC_MODE", (0,)),
)
SOFTWARE_ECHO_CONFIG = {
    "echo-cancel": True,
    # Avoid another AGC/noise suppressor after the board's own processing.
    "gain-control": False,
    "noise-suppression": False,
}
LEGACY_ECHO_CONFIG = {
    "extended-filter": True,
    "delay-agnostic": True,
    # On older WebRTC versions, high suppression sacrifices double-talk.
    "echo-suppression-level": 2,
}


class EchoCancellation:
    """Startup configuration and inspectable state of the active audio path."""

    def __init__(self, audio: Any, *, software: bool = True) -> None:
        self.hardware_verified = audio.apply_audio_config(HARDWARE_ECHO_CONFIG, verify=True)
        if not self.hardware_verified:
            logger.warning("Could not verify the Reachy board's hardware echo configuration")
        self.requested = software
        self.dsp = self.probe = None
        if software:
            self.dsp, self.probe = enable_software_aec(audio)
        logger.info("Echo cancellation configured: %s", self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        """Read actual DSP properties; hardware values were verified at startup."""
        software: dict[str, Any] = {
            "requested": self.requested,
            "enabled": self.dsp is not None and bool(self.dsp.get_property("echo-cancel")),
        }
        if self.dsp is not None:
            configuration, unsupported = _dsp_configuration(self.dsp)
            software["properties"] = {key: self.dsp.get_property(key) for key in configuration}
            software["unsupported_options"] = unsupported
            software["plugin_version"] = self.dsp.get_factory().get_plugin().get_version()
            software["probe"] = self.dsp.get_property("probe")
            for name, element in (("capture_caps", self.dsp), ("reference_caps", self.probe)):
                caps = element.get_static_pad("sink").get_current_caps()
                software[name] = caps.to_string() if caps is not None else None
        return {
            "hardware": {
                "verified_at_startup": bool(self.hardware_verified),
                "parameters": dict(HARDWARE_ECHO_CONFIG) if self.hardware_verified else None,
            },
            "software": software,
        }


def enable_software_aec(audio: Any) -> tuple[Any, Any]:
    """Wire AEC into the SDK 1.8.4 LOCAL pipeline without editing the SDK.

    The SDK has no public AEC switch for this backend. Keep its private graph
    dependency here, validate the expected links, and fail startup if it has
    changed. An unconnected echo probe would silently provide no cancellation.
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    pipeline = getattr(audio, "_pipeline", None)
    capture = getattr(audio, "_appsink_audio", None)
    if pipeline is None or capture is None:
        raise RuntimeError("Software AEC requires Reachy's LOCAL GStreamer audio backend")
    _, state, pending = pipeline.get_state(0)
    if state != Gst.State.NULL or pending not in (Gst.State.VOID_PENDING, Gst.State.NULL):
        raise RuntimeError("Configure software AEC before the audio pipeline starts")

    # Distribution PyGObject may expose only the introspection API, without
    # gst-python's iterable/exception overrides. Use the common iterator API.
    elements = []
    iterator = pipeline.iterate_elements()
    while True:
        result, element = iterator.next()
        if result == Gst.IteratorResult.DONE:
            break
        if result != Gst.IteratorResult.OK:
            raise RuntimeError("Audio pipeline changed while configuring software AEC")
        elements.append(element)

    def find(factory: str) -> list[Any]:
        return [
            element
            for element in elements
            if element.get_factory() and element.get_factory().get_name() == factory
        ]

    existing = find("webrtcdsp")
    if existing:
        if len(existing) != 1:
            raise RuntimeError("Expected one software AEC in the Reachy audio pipeline")
        dsp = existing[0]
        probe = pipeline.get_by_name(dsp.get_property("probe"))
        if probe is None or probe.get_factory().get_name() != "webrtcechoprobe":
            raise RuntimeError("Existing software AEC has no echo probe in its pipeline")
        _configure_dsp(dsp)
        return dsp, probe

    mixers = find("audiomixer")
    capture_sink = capture.get_static_pad("sink")
    capture_source = capture_sink.get_peer()
    if len(mixers) != 1 or capture_source is None:
        raise RuntimeError("Unexpected Reachy audio graph: missing capture link or playback mixer")
    playback_source = mixers[0].get_static_pad("src")
    playback_sink = playback_source.get_peer()
    if playback_sink is None:
        raise RuntimeError("Unexpected Reachy audio graph: playback mixer is not connected")

    def make(factory: str, name: str) -> Any:
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise RuntimeError(f"Software AEC needs the GStreamer {factory} plugin")
        return element

    # Create the probe before the DSP's sink pad can be activated. Both sides
    # use 16 kHz S16LE in this one pipeline, sharing its clock and running time.
    probe = make("webrtcechoprobe", "moss_echo_reference")
    dsp = make("webrtcdsp", "moss_echo_dsp")
    dsp.set_property("probe", probe.get_name())
    _configure_dsp(dsp)
    caps = Gst.Caps.from_string(
        f"audio/x-raw,format=S16LE,rate=16000,channels={audio.CHANNELS},layout=interleaved"
    )

    def convert(prefix: str) -> list[Any]:
        result = [
            make("audioconvert", f"{prefix}_convert"),
            make("audioresample", f"{prefix}_resample"),
            make("capsfilter", f"{prefix}_caps"),
        ]
        result[-1].set_property("caps", caps)
        return result

    reference = [*convert("moss_reference"), probe]
    microphone = [*convert("moss_capture"), dsp, make("audioconvert", "moss_capture_output")]
    links = [
        (playback_source, playback_sink, reference),
        (capture_source, capture_sink, microphone),
    ]
    added, detached, connected = [], [], []
    try:
        for element in reference + microphone:
            # Plain introspection returns bool; gst-python returns None on
            # success and raises AddError on failure.
            if pipeline.add(element) is False:
                raise RuntimeError("Cannot add software AEC to the audio pipeline")
            added.append(element)
        for source, sink, chain in links:
            if not source.unlink(sink):
                raise RuntimeError("Cannot insert software AEC into the audio pipeline")
            detached.append((source, sink))
            pairs = [(source, chain[0].get_static_pad("sink"))]
            pairs += [
                (left.get_static_pad("src"), right.get_static_pad("sink"))
                for left, right in zip(chain, chain[1:], strict=False)
            ]
            pairs.append((chain[-1].get_static_pad("src"), sink))
            for left, right in pairs:
                if left.link(right) != Gst.PadLinkReturn.OK:
                    raise RuntimeError("Cannot link software AEC with its playback reference")
                connected.append((left, right))
    except Exception as exc:
        for left, right in reversed(connected):
            left.unlink(right)
        for element in reversed(added):
            pipeline.remove(element)
        for source, sink in detached:
            source.link(sink)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("Cannot link software AEC with its playback reference") from exc
    return dsp, probe


def _configure_dsp(dsp: Any) -> None:
    configuration, unsupported = _dsp_configuration(dsp)
    for key, value in configuration.items():
        if dsp.find_property(key) is None:
            raise RuntimeError(f"Installed WebRTC AEC does not support {key}")
        dsp.set_property(key, value)
        if dsp.get_property(key) != value:
            raise RuntimeError(f"Installed WebRTC AEC did not accept {key}={value}")
    if unsupported:
        logger.info("WebRTC AEC legacy options are unavailable: %s", ", ".join(unsupported))


def _dsp_configuration(dsp: Any) -> tuple[dict[str, Any], list[str]]:
    from gi.repository import GObject

    configuration = dict(SOFTWARE_ECHO_CONFIG)
    unsupported = []
    for key, value in LEGACY_ECHO_CONFIG.items():
        spec = dsp.find_property(key)
        # GStreamer 1.28's newer WebRTC backend retains these properties as
        # no-ops. Do not claim they are enabled just because set_property works.
        if spec is None or spec.flags & GObject.ParamFlags.DEPRECATED:
            unsupported.append(key)
        else:
            configuration[key] = value
    return configuration, unsupported
