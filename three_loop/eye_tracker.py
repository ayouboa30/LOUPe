"""Optional local eye tracking with an honest, observable fallback.

The camera and vision packages are deliberately imported only when tracking is
started. The rest of 3loop therefore remains dependency-free and usable when a
camera, OpenCV, or MediaPipe is unavailable. This is a gaze *estimate*, not a
medical or accessibility guarantee: lighting, glasses, head pose, camera
placement, calibration drift, and face detection all affect confidence.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EyeTrackingStatus:
    state: str = "stopped"
    available: bool = False
    backend: str = "mediapipe_face_mesh"
    confidence: float = 0.0
    gaze: tuple[float, float] | None = None
    dwell_seconds: float = 0.0
    event_seq: int = 0
    help_requested: bool = False
    message: str = "Suivi oculaire arrêté."
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "available": self.available,
            "backend": self.backend,
            "confidence": round(max(0.0, min(1.0, self.confidence)), 3),
            "gaze": {"x": round(self.gaze[0], 4), "y": round(self.gaze[1], 4)} if self.gaze else None,
            "dwell_seconds": round(max(0.0, self.dwell_seconds), 2),
            "event_seq": self.event_seq,
            "help_requested": self.help_requested,
            "message": self.message,
            "updated_at": self.updated_at,
        }


class EyeTrackingService:
    """Small local service that turns face/iris observations into help events.

    ``observe`` is public so the dwell state machine can be tested without a
    camera. The MediaPipe worker only supplies normalized iris observations;
    it never sends frames or coordinates outside this process.
    """

    def __init__(self, *, dwell_seconds: float = 3.0, stable_radius: float = 0.035) -> None:
        self.dwell_seconds = max(0.5, float(dwell_seconds))
        self.stable_radius = max(0.005, float(stable_radius))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._camera: Any = None
        self._status = EyeTrackingStatus(updated_at=time.time())
        self._stable_since: float | None = None
        self._last_gaze: tuple[float, float] | None = None
        self._last_observation = 0.0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status.as_dict()

    def start(self, *, camera_index: int = 0) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self._status.as_dict()
        try:
            import cv2  # type: ignore
            import mediapipe as mp  # type: ignore
        except ImportError as exc:
            return self._set_unavailable(
                "Modèle local indisponible : installe l'option `eye-tracking` (OpenCV + MediaPipe)."
                f" Détail: {exc}"
            )

        try:
            camera_index = int(camera_index)
            if camera_index < 0 or camera_index > 3:
                raise ValueError("L'index caméra doit être compris entre 0 et 3.")
            camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
            if not camera.isOpened():
                camera.release()
                return self._set_unavailable("Caméra locale inaccessible ou déjà utilisée.")
        except Exception as exc:
            return self._set_unavailable(f"Impossible d'ouvrir la caméra locale : {exc}")

        with self._lock:
            self._camera = camera
            self._stop.clear()
            self._stable_since = None
            self._last_gaze = None
            self._last_observation = 0.0
            self._status = EyeTrackingStatus(
                state="calibrating", available=True, message="Calibration locale : regarde l'écran quelques instants.", updated_at=time.time()
            )
            self._thread = threading.Thread(
                target=self._run, args=(cv2, mp), name="3loop-eye-tracker", daemon=True
            )
            self._thread.start()
            return self._status.as_dict()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        with self._lock:
            camera = self._camera
            self._camera = None
            thread = self._thread
            self._thread = None
        if camera is not None:
            try:
                camera.release()
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        with self._lock:
            self._status = EyeTrackingStatus(updated_at=time.time())
            self._stable_since = None
            self._last_gaze = None
        return self.status()

    def _set_unavailable(self, message: str) -> dict[str, Any]:
        with self._lock:
            self._status = EyeTrackingStatus(state="unavailable", message=message, updated_at=time.time())
        return self.status()

    def observe(self, gaze: tuple[float, float] | None, confidence: float, *, timestamp: float | None = None) -> dict[str, Any]:
        """Consume one normalized gaze sample and update dwell/help state."""

        now = float(timestamp if timestamp is not None else time.monotonic())
        confidence = max(0.0, min(1.0, float(confidence)))
        with self._lock:
            self._last_observation = now
            if gaze is None or confidence < 0.45:
                self._stable_since = None
                self._last_gaze = None
                self._status = EyeTrackingStatus(
                    state="calibrating" if self._status.available else "unavailable",
                    available=self._status.available,
                    backend=self._status.backend,
                    confidence=confidence,
                    message="Visage/iris non détecté : utilise l'aide manuelle si nécessaire.",
                    event_seq=self._status.event_seq,
                    updated_at=time.time(),
                )
                return self._status.as_dict()

            point = (max(0.0, min(1.0, float(gaze[0]))), max(0.0, min(1.0, float(gaze[1]))))
            stable = self._last_gaze is not None and (
                (point[0] - self._last_gaze[0]) ** 2 + (point[1] - self._last_gaze[1]) ** 2
            ) ** 0.5 <= self.stable_radius
            if not stable or self._stable_since is None:
                self._stable_since = now
                self._status = EyeTrackingStatus(
                    state="tracking", available=True, backend=self._status.backend,
                    confidence=confidence, gaze=point, updated_at=time.time(),
                )
            dwell = max(0.0, now - (self._stable_since or now))
            blocked = dwell >= self.dwell_seconds
            event_seq = self._status.event_seq
            help_requested = self._status.help_requested
            if blocked and not help_requested:
                event_seq += 1
                help_requested = True
            if not blocked:
                help_requested = False
            self._last_gaze = point
            self._status = EyeTrackingStatus(
                state="blocked" if blocked else "tracking", available=True,
                backend=self._status.backend, confidence=confidence, gaze=point,
                dwell_seconds=dwell, event_seq=event_seq, help_requested=help_requested,
                message=("Blocage probable : demande d'aide proposée." if blocked else "Suivi local actif."),
                updated_at=time.time(),
            )
            return self._status.as_dict()

    def _run(self, cv2: Any, mp: Any) -> None:
        face_mesh = None
        try:
            face_mesh = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=True, min_detection_confidence=0.5, min_tracking_confidence=0.5
            )
            while not self._stop.is_set():
                camera = self._camera
                if camera is None:
                    break
                ok, frame = camera.read()
                if not ok:
                    self.observe(None, 0.0)
                    time.sleep(0.08)
                    continue
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = face_mesh.process(frame)
                landmarks = result.multi_face_landmarks[0].landmark if result.multi_face_landmarks else None
                if not landmarks or len(landmarks) < 478:
                    self.observe(None, 0.0)
                    continue
                # MediaPipe iris landmarks: left 468-472, right 473-477.
                iris = [landmarks[index] for index in range(468, 478)]
                gaze = (sum(point.x for point in iris) / len(iris), sum(point.y for point in iris) / len(iris))
                self.observe(gaze, 0.8)
        except Exception as exc:
            self._set_unavailable(f"Le modèle local s'est arrêté : {exc}")
        finally:
            if face_mesh is not None:
                try:
                    face_mesh.close()
                except Exception:
                    pass
            camera = self._camera
            if camera is not None:
                try:
                    camera.release()
                except Exception:
                    pass
            with self._lock:
                self._camera = None
                self._thread = None


_EYE_TRACKER = EyeTrackingService()


def get_eye_tracker() -> EyeTrackingService:
    return _EYE_TRACKER
