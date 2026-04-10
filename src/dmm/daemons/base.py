import logging
import threading
from time import sleep
import os

class DaemonBase:
    def __init__(self, frequency, kwargs=None):
        self.frequency = frequency
        self.kwargs = kwargs or {}
        self.thread = None
        self.running = True

    def process(self):
        raise NotImplementedError("Subclasses must implement this method")
    
    def run_once(self, **kwargs):
        raise NotImplementedError("Subclasses must implement this method")

    def run_daemon(self, process, lock, **kwargs):
        if self.frequency < 0:
            logging.info(f"frequency is set to negative, not starting the daemon.")
            return
        
        while self.running:
            try:
                with lock:
                    logging.debug(f"acquired lock")
                    try:
                        process(**kwargs)
                    except Exception as e:
                        logging.error(f"Error in {self.__class__.__name__}: {e}", exc_info=True)
                logging.debug(f"released lock, sleeping for {self.frequency} seconds")
            except Exception as e:
                logging.error(f"Unexpected error: {e}", exc_info=True)
            
            # Sleep in smaller intervals to allow for faster shutdown
            sleep_remaining = self.frequency
            while sleep_remaining > 0 and self.running:
                sleep(min(1, sleep_remaining))
                sleep_remaining -= 1

    def start(self, lock):
        logging.info(f"Starting {self.__class__.__name__}")
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