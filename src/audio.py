"""Parallel audio playback worker and the ``play_audio`` decorator."""

from __future__ import annotations

import functools
import inspect
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIOS_DIR = Path(__file__).resolve().parent.parent / "resources" / "audios"

_AUDIO_FILES: dict[str, Path] = {
    "acknowledge": AUDIOS_DIR / "acknowledge.wav",
    "process": AUDIOS_DIR / "process.wav",
}


def _find_ffplay() -> str | None:
    ffmpeg = os.getenv("FFMPEG_PATH")
    if ffmpeg:
        ffmpeg_dir = os.path.dirname(ffmpeg)
        candidate = os.path.join(ffmpeg_dir, "ffplay.exe" if os.name == "nt" else "ffplay")
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ffplay")


def _atempo_chain(speed: float) -> str:
    """Build an ``atempo`` filter chain (each factor must be within [0.5, 2.0])."""
    if speed <= 0:
        speed = 1.0
    factors: list[float] = []
    remaining = float(speed)
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6g}" for factor in factors)


class AudioSubWorker(threading.Thread):
    """A sub-worker that plays a single audio file, in parallel with the others."""

    def __init__(
        self,
        path: str | Path,
        speed: float = 1.0,
        volume: float = 1.0,
        loop: bool = False,
    ) -> None:
        super().__init__(daemon=True, name=f"audio-{Path(path).stem}")
        self.path = Path(path)
        self.speed = float(speed)
        self.volume = float(volume)
        self.loop = bool(loop)
        self._terminate = threading.Event()
        self._process: subprocess.Popen | None = None

    def run(self) -> None:
        if not self.path.exists():
            logger.warning("Audio file not found at %s", self.path)
            return
        try:
            self._play()
        except Exception as exc:  # noqa: BLE001 - playback must never crash the worker
            logger.debug("Audio playback error for %s: %s", self.path.name, exc)

    def _play(self) -> None:
        ffplay = _find_ffplay()
        if ffplay:
            self._run_ffplay(ffplay)
        elif os.name == "nt":
            self._run_powershell()
        else:
            self._run_aplay()

    def _run_ffplay(self, ffplay: str) -> None:
        command = [ffplay, "-nodisp"]
        if self.loop:
            command += ["-loop", "0"]
        else:
            command += ["-autoexit"]
        volume = max(0, min(100, int(round(self.volume * 100))))
        command += ["-volume", str(volume)]
        if abs(self.speed - 1.0) > 1e-6:
            command += ["-af", _atempo_chain(self.speed)]
        command.append(str(self.path))
        self._process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._wait_process()

    def _run_powershell(self) -> None:
        # SoundPlayer does not support speed/volume; loop is simulated manually.
        if self.speed != 1.0 or self.volume != 1.0:
            logger.debug("speed/volume ignored: PowerShell playback for %s", self.path.name)
        while True:
            if self._terminate.is_set():
                return
            path_escaped = str(self.path.resolve()).replace("'", "''")
            ps_cmd = f"(New-Object System.Media.SoundPlayer '{path_escaped}').PlaySync()"
            self._process = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._wait_process()
            if not self.loop:
                return

    def _run_aplay(self) -> None:
        # aplay does not support speed/volume; loop is simulated manually.
        if self.speed != 1.0 or self.volume != 1.0:
            logger.debug("speed/volume ignored: aplay playback for %s", self.path.name)
        while True:
            if self._terminate.is_set():
                return
            self._process = subprocess.Popen(
                ["aplay", str(self.path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._wait_process()
            if not self.loop:
                return

    def _wait_process(self) -> None:
        while self._process.poll() is None:
            if self._terminate.is_set():
                self._kill_process()
                return
            self._terminate.wait(timeout=0.1)

    def _kill_process(self) -> None:
        proc = self._process
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        """Ask the sub-worker to stop playing (terminates the underlying process)."""
        self._terminate.set()
        self._kill_process()


class AudioOrchestrator:
    """Takes playback inputs, spawns sub-workers and reaps/terminates them."""

    def __init__(self) -> None:
        self._workers: list[AudioSubWorker] = []
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """Start the audio worker (no audio is played by this call)."""
        with self._lock:
            self._started = True

    def stop(self) -> None:
        """Kill every running sub-worker and stop the audio worker."""
        with self._lock:
            self._started = False
        self.stop_all()

    def is_started(self) -> bool:
        with self._lock:
            return self._started

    def play(
        self,
        path: str | Path,
        speed: float = 1.0,
        volume: float = 1.0,
        loop: bool = False,
    ) -> AudioSubWorker | None:
        """Create and start a sub-worker to play ``path`` in parallel, if started."""
        if not self.is_started():
            logger.debug("Audio worker not started; ignoring playback of %s", Path(path).name)
            return None
        worker = AudioSubWorker(path, speed=speed, volume=volume, loop=loop)
        with self._lock:
            self._workers.append(worker)
        worker.start()
        self.reap_finished()
        return worker

    def reap_finished(self) -> None:
        """Remove sub-workers that have already finished their job."""
        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]

    def terminate(self, worker: AudioSubWorker) -> None:
        """Terminate a specific sub-worker because another entity asked to."""
        worker.stop()
        self.reap_finished()

    def stop_all(self) -> None:
        """Terminate every sub-worker currently running."""
        with self._lock:
            workers = list(self._workers)
        for worker in workers:
            worker.stop()
        self.reap_finished()


_orchestrator = AudioOrchestrator()


def get_audio_orchestrator() -> AudioOrchestrator:
    """Return the process-wide audio orchestrator."""
    return _orchestrator


def set_audio_worker_enabled(enabled: bool) -> None:
    """Start or stop the audio worker based on whether audios should be played.

    When ``enabled`` is true the worker is started (without playing any audio).
    When false the worker kills every running sub-worker and stops itself.
    """
    if enabled:
        _orchestrator.start()
    else:
        _orchestrator.stop()


def play_sound(name: str) -> None:
    """Trigger playback of the sound associated with ``name`` (fire-and-forget)."""
    path = _AUDIO_FILES.get(name)
    if path is None:
        logger.debug(
            "No audio is associated with %r (available: %s)",
            name, ", ".join(sorted(_AUDIO_FILES)),
        )
        return
    _orchestrator.play(path)


def play_audio(name: str):
    """Decorator factory that marks a callable to trigger audio playback.

    ``name`` is one of ``"acknowledge"``, ``"warn"``, ``"process"``, ``"success"``
    or ``"error"``. Only ``"acknowledge"`` and ``"process"`` have an associated
    audio for now.

    Applied to a method/endpoint, the audio plays the moment it is called::

        @play_audio("acknowledge")
        def handler():
            ...

    Called directly (``play_audio("acknowledge")()``), the audio plays immediately,
    which marks a single line of code or the end of a method that has no return.
    """
    def decorator(func=None):
        if func is None:
            play_sound(name)
            return None

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                play_sound(name)
                return await func(*args, **kwargs)
            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            play_sound(name)
            return func(*args, **kwargs)
        return wrapper
    return decorator
