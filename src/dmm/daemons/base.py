import logging
import threading
from time import monotonic, sleep, time
import os

from dmm.core.health import write_heartbeat
from dmm.core.metrics import (
    DAEMON_CYCLE_DURATION,
    DAEMON_ERRORS,
    DAEMON_FREQUENCY,
    DAEMON_LAST_SUCCESS,
    DAEMON_LOCK_WAIT,
    DAEMON_RUNNING,
)
from dmm.core.tracing import MANUAL_ERRORS, get_tracer, record_error

class DaemonBase:
    def __init__(self, frequency, kwargs=None):
        self.frequency = frequency
        self.kwargs = kwargs or {}
        self.thread = None
        self.running = True
        self.started_at = 0
        self.last_success = 0

    def process(self):
        raise NotImplementedError("Subclasses must implement this method")
    
    def run_once(self, **kwargs):
        raise NotImplementedError("Subclasses must implement this method")

    def _publish(self, name, running):
        write_heartbeat(name, self.frequency, self.started_at, self.last_success, running)

    def run_daemon(self, process, lock, **kwargs):
        name = self.__class__.__name__
        if self.frequency < 0:
            logging.info(f"frequency is set to negative, not starting the daemon.")
            self._publish(name, running=False)
            return

        self.started_at = time()
        tracer = get_tracer(__name__)
        DAEMON_FREQUENCY.labels(name).set(self.frequency)
        DAEMON_RUNNING.labels(name).set(1)
        # Instantiate the heartbeat at 0 so a daemon that has never completed a
        # cycle looks infinitely stale instead of absent.
        DAEMON_LAST_SUCCESS.labels(name)
        self._publish(name, running=True)
        try:
            while self.running:
                try:
                    lock_wait_start = monotonic()
                    with lock:
                        lock_wait = monotonic() - lock_wait_start
                        DAEMON_LOCK_WAIT.labels(name).observe(lock_wait)
                        logging.debug(f"acquired lock")
                        cycle_start = monotonic()
                        # One span per cycle, parent of everything the cycle
                        # does. All fourteen daemons share this lock, so the
                        # serialisation shows up here as a shape.
                        with tracer.start_as_current_span(f"daemon.{name}", **MANUAL_ERRORS) as span:
                            span.set_attribute("dmm.daemon", name)
                            span.set_attribute("dmm.lock_wait_seconds", lock_wait)
                            try:
                                process(**kwargs)
                                self.last_success = time()
                                DAEMON_LAST_SUCCESS.labels(name).set(self.last_success)
                                self._publish(name, running=True)
                            except Exception as e:
                                DAEMON_ERRORS.labels(name, type(e).__name__).inc()
                                record_error(span, e)
                                logging.error(f"Error in {name}: {e}", exc_info=True)
                            finally:
                                DAEMON_CYCLE_DURATION.labels(name).observe(monotonic() - cycle_start)
                    logging.debug(f"released lock, sleeping for {self.frequency} seconds")
                except Exception as e:
                    DAEMON_ERRORS.labels(name, type(e).__name__).inc()
                    logging.error(f"Unexpected error: {e}", exc_info=True)

                # Sleep in smaller intervals to allow for faster shutdown
                sleep_remaining = self.frequency
                while sleep_remaining > 0 and self.running:
                    sleep(min(1, sleep_remaining))
                    sleep_remaining -= 1
        finally:
            # Also covers the thread dying outside the loop, which nothing else notices.
            DAEMON_RUNNING.labels(name).set(0)
            self._publish(name, running=False)

    def start(self, lock):
        logging.info(f"Starting {self.__class__.__name__}")
        DAEMON_FREQUENCY.labels(self.__class__.__name__).set(self.frequency)
        DAEMON_RUNNING.labels(self.__class__.__name__).set(0)
        self._publish(self.__class__.__name__, running=False)
        try:
            self.thread = threading.Thread(
                target=self.run_daemon,
                args=(self.process, lock),
                kwargs=self.kwargs,
                name=self.__class__.__name__,
                daemon=True
            )
            self.thread.start()
            return self.thread
        except Exception as e:
            logging.error(f"Error starting {self.__class__.__name__}: {e}", exc_info=True)
            return None

    def stop(self):
        logging.info(f"Stopping {self.__class__.__name__}")
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)