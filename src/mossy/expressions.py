"""Small, cancellable conversation gestures using official Reachy recordings.

Only the worker writes base poses. Speech wobbling remains an independent SDK
offset; this module never calls the media or move-cancellation APIs.
"""

import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

logger = logging.getLogger(__name__)

Scope = tuple[str, str]
Pose = tuple[np.ndarray, np.ndarray]
NEUTRAL: Pose = (np.eye(4), np.array([-0.1745, 0.1745]))
FRAME_SECONDS = 0.02
BLEND_SECONDS = 0.35
RESPONSE_COOLDOWN = 4.0
REACTION_COOLDOWN = 8.0
MAX_START_DELAY = 1.5

# Revisions already preloaded by the daemon; loading never downloads on the
# conversation path. Strip sidecar sound by constructing motion-only clips.
LIBRARIES = {
    "pollen-robotics/reachy-mini-dances-library": (
        "3564295e72d41c1271f46bf5540a3fcad9d5f669",
        {"happy": "groovy_sway_and_roll", "agree": "simple_nod"},
    ),
    "pollen-robotics/reachy-mini-emotions-library": (
        "873ae49f0b89114b7e535eff0c1f7560d21d9357",
        {"thinking": "thoughtful1", "listening": "attentive1"},
    ),
}


def load_moves() -> dict[str, Any]:
    from huggingface_hub import snapshot_download
    from reachy_mini.motion.recorded_move import RecordedMove

    moves = {}
    for dataset, (revision, names) in LIBRARIES.items():
        root = Path(
            snapshot_download(
                dataset, repo_type="dataset", revision=revision, local_files_only=True
            )
        )
        for kind, name in names.items():
            path = root / "data" / f"{name}.json"
            if not path.exists():
                path = root / f"{name}.json"
            moves[kind] = RecordedMove(json.loads(path.read_text()), sound_path=None)
    return moves


def classify_response(text: str, *, final: bool = False) -> str | None:
    """Recognize explicit expressions in the first complete sentence.

    This is a conservative Chinese rule set, not a general sentiment model.
    Quoted speech, questions and negated/contrasting expressions stay neutral.
    """
    sentence = re.split(r"(?<=[。！？!?；;\n])", text, maxsplit=1)[0].strip()
    if not sentence or (not final and sentence[-1] not in "。！？!?；;\n"):
        return None
    if any(mark in sentence for mark in ("“", "”", '"', "「", "」", "『", "』", "`")):
        return None
    if sentence.endswith(("?", "？")):
        return None
    if re.search(r"不|没(?:有|能|法)|别|难过|遗憾|可惜|失望|抱歉|对不起|但是|不过|然而", sentence):
        return None
    if re.match(
        r"(?:哈哈[，,！! ]*)?(?:太好了|太棒了|真棒|太赞了|好消息|恭喜|祝贺|"
        r"真为你高兴|我(?:很|真|也|非常|特别)?(?:开心|高兴)|很高兴)",
        sentence,
    ):
        return "happy"
    if re.match(r"(?:让我|我来|我)(?:想想|想一想|思考一下|考虑一下)", sentence):
        return "thinking"
    if re.match(
        r"(?:好的|好呀|好啊|没问题|当然可以|可以的|明白了|收到|了解了|我同意|确实如此|是的)"
        r"(?:[，,。！!：:；;\s]|$)",
        sentence,
    ):
        return "agree"
    return None


def scope_of(data: dict[str, Any]) -> Scope | None:
    run, turn = data.get("run_id"), data.get("turn_id")
    return (run, turn) if isinstance(run, str) and run and isinstance(turn, str) and turn else None


def blend_pose(start: Pose, end: Pose, weight: float) -> Pose:
    weight = float(np.clip(weight, 0.0, 1.0))
    weight = weight * weight * (3.0 - 2.0 * weight)
    head = np.eye(4)
    a, b = Rotation.from_matrix(start[0][:3, :3]), Rotation.from_matrix(end[0][:3, :3])
    head[:3, :3] = (a * Rotation.from_rotvec((a.inv() * b).as_rotvec() * weight)).as_matrix()
    head[:3, 3] = start[0][:3, 3] * (1 - weight) + end[0][:3, 3] * weight
    return head, start[1] * (1 - weight) + end[1] * weight


@dataclass
class Response:
    intent: str | None = None
    started_at: float | None = None
    used: bool = False
    closed: bool = False


@dataclass
class Motion:
    kind: str
    scope: Scope | None
    started_at: float
    duration: float
    origin: Pose


class Expressions:
    """Serialize gestures off the WebSocket/audio threads, at 50 Hz."""

    def __init__(
        self,
        robot: Any,
        *,
        enabled: bool = True,
        intensity: float = 0.45,
        moves: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.0 <= intensity <= 1.0:
            raise ValueError("MOSS_EXPRESSION_INTENSITY must be between 0 and 1")
        self.robot = robot
        self.enabled = enabled
        self.intensity = intensity
        self._moves = moves
        self._clock = clock
        self._events: deque[tuple[str, dict[str, Any], float]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._responses: OrderedDict[Scope, Response] = OrderedDict()
        self._speaking: Scope | None = None
        self._motion: Motion | None = None
        self._last_pose = (NEUTRAL[0].copy(), NEUTRAL[1].copy())
        self._return_at: float | None = None
        self._return_from = self._last_pose
        self._last_response_motion = float("-inf")
        self._last_reaction = {"listening": float("-inf"), "thinking": float("-inf")}
        self._listen_until = 0.0
        self._waiting_since: float | None = None
        self._thought_sent = False
        self._last_input_done = ""
        self._status: dict[str, Any] = {
            "enabled": enabled,
            "available": moves is not None,
            "active": None,
            "last_expression": None,
            "started": 0,
            "cancelled": 0,
            "error": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def submit(self, action: str, data: dict[str, Any]) -> None:
        """Nonblocking event handoff; overflow resets gestures, never audio."""
        now = self._clock()
        with self._lock:
            if len(self._events) >= 128:
                self._events.clear()
                self._events.append(("motion.reset", {}, now))
            self._events.append((action, dict(data), now))
        self._wake.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="moss-expressions", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()

    def _update_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _response(self, scope: Scope) -> Response:
        if scope not in self._responses:
            self._responses[scope] = Response()
            if len(self._responses) > 128:
                self._responses.popitem(last=False)
        return self._responses[scope]

    def _reset(self, now: float) -> None:
        self._responses.clear()
        self._speaking = None
        self._listen_until = 0.0
        self._waiting_since = None
        self._last_input_done = ""
        self._return_to_neutral(now, cancelled=True)

    def _handle(self, action: str, data: dict[str, Any], now: float) -> None:
        scope = scope_of(data)
        if action in ("session.disconnected", "motion.reset"):
            self._reset(now)
        elif action == "motion.enabled":
            self.enabled = bool(data["enabled"])
            self._update_status(enabled=self.enabled)
            self._reset(now)
        elif not self.enabled:
            return
        elif action.startswith("input.transcript."):
            if self._speaking is not None or not data.get("text", "").strip():
                return
            if action.endswith(".updated"):
                self._listen_until = now + 0.8
                self._waiting_since = None
                self._last_input_done = ""
            elif data["text"] != self._last_input_done:
                self._last_input_done = data["text"]
                self._listen_until = 0.0
                self._waiting_since = now
                self._thought_sent = False
        elif action.startswith("output.transcript.") and scope is not None:
            response = self._response(scope)
            if not response.closed and not response.used:
                response.intent = classify_response(
                    data.get("text", ""), final=action.endswith(".done")
                )
        elif action == "playback.started" and scope is not None:
            response = self._response(scope)
            if response.closed:
                return
            if self._speaking != scope:
                if self._speaking is not None:
                    self._response(self._speaking).closed = True
                self._return_to_neutral(now, cancelled=True)
                self._speaking = scope
                response.started_at = now
            self._waiting_since = None
            self._listen_until = 0.0
        elif scope is not None and (
            action in ("response.interrupted", "response.audio.stop", "playback.completed")
            or (action == "response.ended" and data.get("status") in ("aborted", "failed"))
        ):
            self._response(scope).closed = True
            if self._speaking == scope:
                self._speaking = None
            if self._motion is not None and self._motion.scope == scope:
                self._return_to_neutral(now, cancelled=action != "playback.completed")

    def _start_motion(self, kind: str, scope: Scope | None, now: float) -> None:
        assert self._moves is not None
        duration = min(float(self._moves[kind].duration), 2.4 if scope else 1.6)
        self._motion = Motion(kind, scope, now, duration, self._last_pose)
        self._return_at = None
        with self._lock:
            self._status.update(active=kind, last_expression=kind)
            self._status["started"] += 1
        logger.info("Expression started: %s scope=%s", kind, scope)

    def _return_to_neutral(self, now: float, *, cancelled: bool = False) -> None:
        if self._motion is None:
            return
        if cancelled:
            with self._lock:
                self._status["cancelled"] += 1
            logger.info("Expression cancelled: %s scope=%s", self._motion.kind, self._motion.scope)
        self._motion = None
        self._return_at, self._return_from = now, self._last_pose
        self._update_status(active="returning")

    def _choose(self, now: float) -> None:
        if not self.enabled or self._stop.is_set():
            return
        if self._speaking is not None:
            response = self._response(self._speaking)
            if response.closed or response.used or response.started_at is None:
                return
            if now - response.started_at > MAX_START_DELAY:
                response.used = True
            elif response.intent is not None:
                response.used = True
                if now - self._last_response_motion >= RESPONSE_COOLDOWN:
                    self._start_motion(response.intent, self._speaking, now)
                    self._last_response_motion = now
            return
        if self._motion is not None:
            return
        kind = None
        if now < self._listen_until:
            kind = "listening"
        elif self._waiting_since is not None and not self._thought_sent:
            delay = now - self._waiting_since
            if 0.8 <= delay <= 3.0:
                kind = "thinking"
                self._thought_sent = True
        if kind is not None and now - self._last_reaction[kind] >= REACTION_COOLDOWN:
            self._start_motion(kind, None, now)
            self._last_reaction[kind] = now

    def _sample(self, motion: Motion, elapsed: float) -> Pose:
        assert self._moves is not None
        move = self._moves[motion.kind]
        head, antennas, _ = move.evaluate(min(elapsed, move.duration - 0.01))
        intensity = self.intensity * (0.55 if motion.scope is None else 1.0)
        target = np.eye(4)
        rotation = Rotation.from_matrix(head[:3, :3]).as_rotvec() * intensity
        limit = np.deg2rad(10.0)
        rotation *= min(1.0, limit / max(np.linalg.norm(rotation), 1e-9))
        target[:3, :3] = Rotation.from_rotvec(rotation).as_matrix()
        target[:3, 3] = np.clip(head[:3, 3] * intensity, -0.01, 0.01)
        antenna_delta = (np.asarray(antennas) - NEUTRAL[1]) * intensity
        target_antennas = NEUTRAL[1] + np.clip(antenna_delta, -0.65, 0.65)
        return blend_pose(motion.origin, (target, target_antennas), elapsed / BLEND_SECONDS)

    def _tick(self, now: float) -> None:
        self._choose(now)
        pose = None
        if self._motion is not None:
            elapsed = now - self._motion.started_at
            if elapsed >= self._motion.duration:
                self._return_to_neutral(now)
            else:
                pose = self._sample(self._motion, elapsed)
        if self._return_at is not None:
            weight = (now - self._return_at) / BLEND_SECONDS
            pose = blend_pose(self._return_from, NEUTRAL, weight)
            if weight >= 1.0:
                self._return_at = None
                self._update_status(active=None)
        if pose is not None:
            self.robot.set_target(head=pose[0], antennas=pose[1])
            self._last_pose = pose

    def _run(self) -> None:
        try:
            if self._moves is None:
                self._moves = load_moves()
            self._update_status(available=True)
            logger.info(
                "Expressions ready: %s (intensity %.2f)", ", ".join(self._moves), self.intensity
            )
            next_tick = self._clock()
            while True:
                now = self._clock()
                with self._lock:
                    events = list(self._events)
                    self._events.clear()
                for action, data, queued_at in events:
                    if now - queued_at > MAX_START_DELAY and (
                        action.startswith(("input.transcript.", "output.transcript."))
                        or action == "playback.started"
                    ):
                        continue
                    self._handle(action, data, now)
                if self._stop.is_set():
                    self._reset(now)
                if now >= next_tick:
                    self._tick(now)
                    next_tick = now + FRAME_SECONDS
                if self._stop.is_set() and self._return_at is None:
                    break
                self._wake.wait(max(0.0, next_tick - self._clock()))
                self._wake.clear()
        except Exception as exc:
            self._update_status(available=False, active=None, error=str(exc))
            logger.exception("Expressions stopped; voice conversation remains available")
