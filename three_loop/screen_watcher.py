"""Periodic screen reading for the companion's research-assistant mode.

When the user turns the mode on (the mascot puts its hat on), this reads the
screen every few minutes, turns what it finds into a web search, and hands the
results back so the companion can offer them in a bubble.

Two things keep that from becoming annoying, and both are the reason this is a
class with state rather than a bare loop:

* **Nothing is offered twice.** Screens barely change between two readings of
  the same document, so a naive loop would suggest the same three articles
  every interval. Each pass is fingerprinted as a word set and compared with
  the previous one; anything above ``similarity_threshold`` is skipped
  silently. URLs already shown are filtered out too.
* **Nothing is offered about nothing.** A locked screen, a wallpaper or a
  video player produce almost no text, and searching the web for six stray
  characters returns noise, so passes below ``min_chars`` are dropped.

Every dependency is injected, so the scheduling and de-duplication logic can
be exercised without Windows, OCR or the network.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

#: How often the screen is read, in seconds. Five minutes is a compromise: at
#: one minute the same context is still on screen and almost every pass is
#: discarded as a duplicate, while a quarter of an hour is too late to be
#: useful for what the user is doing right now.
DEFAULT_INTERVAL_SECONDS = 300

#: Offered intervals, in minutes, surfaced in the companion's context menu.
INTERVAL_CHOICES_MINUTES = (2, 5, 10, 15)

_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def fingerprint(text: str) -> frozenset[str]:
    """Reduce a capture to the set of words worth comparing.

    Case and short tokens are dropped: OCR is not stable enough for an exact
    comparison to ever match, and single characters flip between passes as
    antialiasing changes.
    """

    return frozenset(match.group(0).lower() for match in _WORD_RE.finditer(text or ""))


def similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard overlap of two fingerprints, 0.0 (nothing shared) to 1.0."""

    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


@dataclass
class WatchResult:
    """One accepted pass: what was read and what the web returned for it."""

    text: str
    results: list[Any] = field(default_factory=list)


class ScreenWatcher:
    """Read the screen on a timer and report fresh, non-repeating findings."""

    def __init__(
        self,
        *,
        capture: Callable[[], Any],
        ocr: Callable[[Any], str],
        search: Callable[[str], Sequence[Any]],
        on_result: Callable[[WatchResult], None],
        on_skip: Callable[[str], None] | None = None,
        before_capture: Callable[[], None] | None = None,
        after_capture: Callable[[], None] | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        min_chars: int = 60,
        similarity_threshold: float = 0.82,
        read_immediately: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._capture = capture
        self._ocr = ocr
        self._search = search
        self._on_result = on_result
        self._on_skip = on_skip
        self._before_capture = before_capture
        self._after_capture = after_capture
        self._interval = max(30.0, float(interval_seconds))
        self._min_chars = min_chars
        self._similarity_threshold = similarity_threshold
        #: Read once as soon as the loop starts, instead of after one interval.
        self._read_immediately = read_immediately
        self._sleep = sleep

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_fingerprint: frozenset[str] = frozenset()
        self._seen_urls: set[str] = set()
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def set_interval(self, seconds: float) -> None:
        """Change the cadence; takes effect on the next wait, not mid-wait."""

        self._interval = max(30.0, float(seconds))

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="3loop-screen-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger_now(self) -> None:
        """Cut the current wait short and read the screen immediately."""

        self._wake.set()

    # -- internals ------------------------------------------------------

    def _loop(self) -> None:
        # First pass immediately, before any wait. Turning the mode on is an
        # explicit request to be helped *now*: waiting a full interval made the
        # feature look broken, since nothing at all happened for minutes after
        # the user enabled it. Subsequent passes keep the cadence.
        first = self._read_immediately
        while not self._stop.is_set():
            if not first:
                # Waiting on an Event rather than sleeping the full interval
                # keeps stop() and trigger_now() responsive instead of blocking
                # for minutes.
                self._wake.wait(timeout=self._interval)
                self._wake.clear()
                if self._stop.is_set():
                    return
            first = False
            try:
                self.run_once()
            except Exception as exc:  # a bad pass must not kill the timer
                self._notify_skip(f"lecture impossible: {exc}")

    def run_once(self) -> WatchResult | None:
        """Read, filter and search once. Returns the accepted result, if any."""

        if self._before_capture is not None:
            self._before_capture()
        try:
            image = self._capture()
        finally:
            if self._after_capture is not None:
                self._after_capture()
        if image is None:
            self._notify_skip("aucune capture disponible")
            return None

        text = (self._ocr(image) or "").strip()
        if len(text) < self._min_chars:
            self._notify_skip("trop peu de texte lisible a l'ecran")
            return None

        current = fingerprint(text)
        with self._lock:
            previous = self._last_fingerprint
        if similarity(current, previous) >= self._similarity_threshold:
            self._notify_skip("ecran inchange depuis la derniere lecture")
            return None

        results = [item for item in self._search(text) or () if self._is_new(item)]
        with self._lock:
            self._last_fingerprint = current
            for item in results:
                url = _url_of(item)
                if url:
                    self._seen_urls.add(url)
        if not results:
            self._notify_skip("aucune piste nouvelle trouvee")
            return None

        result = WatchResult(text=text, results=results)
        self._on_result(result)
        return result

    def _is_new(self, item: Any) -> bool:
        url = _url_of(item)
        if not url:
            return False
        with self._lock:
            return url not in self._seen_urls

    def _notify_skip(self, reason: str) -> None:
        if self._on_skip is not None:
            try:
                self._on_skip(reason)
            except Exception:
                pass


def _url_of(item: Any) -> str:
    """Read a URL off a SearchResult, a dict, or anything with a .url."""

    if isinstance(item, dict):
        return str(item.get("url", "")).strip()
    return str(getattr(item, "url", "") or "").strip()
