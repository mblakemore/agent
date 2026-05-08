import time
import logging
import random

def retry_on_429(func, log, max_retries=5, base_delay=60):
    """
    Retry wrapper for Foundry backend calls. 
    Specifically handles 'Too Many Requests' errors with a fixed 60s backoff.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            error_msg = str(e)
            if "Too Many Requests" in error_msg and attempt < max_retries:
                delay = base_delay + (random.random() * 5) # Add small jitter
                log.warning(
                    "foundry.retry.429: %s. Attempt %d/%d. Retrying in %.2fs...",
                    error_msg, attempt + 1, max_retries, delay
                )
                time.sleep(delay)
                continue
            raise e
