"""Owned deadline signals; an external cancellation is never a delivery request."""
from contextlib import contextmanager
import math
import signal
import time


class RunDeadlineExceeded(TimeoutError):
    """A controller-owned absolute deadline expired, not an arbitrary SDK timeout."""


@contextmanager
def time_limit(seconds, *, error_type=TimeoutError):
    if seconds is None:
        yield  # Preserve an enclosing platform/caller timer.
        return
    if not hasattr(signal, 'SIGALRM'):
        raise RuntimeError('This submission targets Linux/macOS with SIGALRM support')
    if seconds <= 0:
        raise error_type('HAiKnow total run time budget exhausted')
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def restore():
        left, interval = previous_timer
        if left:
            left -= time.monotonic() - started
            if left <= 0:
                left = left + (math.floor(-left / interval) + 1) * interval if interval else 0
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, max(0, left), interval)

    prior_first = bool(previous_timer[0] and previous_timer[0] <= seconds)
    def expired(signum, frame):
        # Re-arm the outer hard limit BEFORE unwinding SDK/validator cleanup.
        restore()
        if prior_first:
            if callable(previous):
                previous(signum, frame)
            # Even a handler that returns must not turn external cancellation into success.
            raise TimeoutError('External run time limit reached')
        raise error_type('HAiKnow total run time budget exhausted')

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, previous_timer[0] if prior_first else seconds)
    try:
        yield
    finally:
        restore()
