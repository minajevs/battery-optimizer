"""Proof that the process holding a session is still running.

The lease says a session was started. It cannot say whether whoever started it
is still alive to finish it — and since no expiry ends a SESSION on this
hardware (30408 bounds the command only)
(see ``lease.py``), a session whose owner has stopped running is a session that
never ends.

So the owner stamps a heartbeat while it lives, and something outside it watches
that stamp go stale. A heartbeat is deliberately the weakest of the reaper's
conditions: staleness alone grants nothing at all, and never could — it is only
the trigger for asking whether the *inverter* shows a stranded session that the
*lease* accounts for.

**A missing heartbeat reads as stale, not as healthy.** The alternative would
make "the optimizer never started, but its lease and an armed inverter are both
still here" the one case nothing cleans up.

Written with the same atomic replace as the lease, for the same reason: the
failure being defended against can land between any two instructions.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


class Heartbeat:
    """A timestamp file, stamped by the owner and read by the reaper.

    ``path`` may be None, which disables it: ``stamp()`` becomes a no-op and
    ``age()`` returns None ("unknown"). Callers must treat unknown as stale.
    """

    def __init__(self, path: Optional[str], log_func=None, clock=time.time):
        self.path = path
        self._log_func = log_func
        self._clock = clock
        self.last_error: Optional[str] = None

    def _log(self, message: str, level: str = "INFO") -> None:
        if self._log_func is not None:
            self._log_func(f"[heartbeat] {message}", level=level)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def stamp(self) -> bool:
        """Record that the owner is alive right now."""
        if not self.path:
            return False
        payload = json.dumps({"at": self._clock(), "pid": os.getpid()})
        temp = f"{self.path}.{os.getpid()}.tmp"
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            return True
        except Exception as e:  # noqa: BLE001 - never take the optimizer down
            self.last_error = str(e)
            self._log(f"could not stamp {self.path}: {e}", level="WARNING")
            try:
                os.unlink(temp)
            except OSError:
                pass
            return False

    def read_at(self) -> Optional[float]:
        """The last stamp, or None when there is no readable one."""
        if not self.path:
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return float(json.load(handle)["at"])
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001 - unreadable is unknown is stale
            self.last_error = str(e)
            self._log(f"could not read {self.path}: {e}; treated as STALE",
                      level="WARNING")
            return None

    def age(self) -> Optional[float]:
        """Seconds since the last stamp, or None when that is unknowable.

        None means stale to every caller. A negative age -- a stamp from the
        future, which a clock change can produce -- is reported as 0: the file
        was written, so something was alive, and inventing staleness from a
        clock jump is exactly how a reaper would act on a healthy optimizer.
        """
        stamped = self.read_at()
        if stamped is None:
            return None
        return max(0.0, self._clock() - stamped)

    def clear(self) -> None:
        """Remove the stamp. Used by tests and by a clean shutdown."""
        if not self.path:
            return
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self._log(f"could not remove {self.path}: {e}", level="WARNING")
