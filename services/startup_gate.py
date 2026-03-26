import threading

# Set when the startup source-of-truth sync completes (success or failure).
# Worker threads wait on this event before processing any queued jobs, ensuring
# the initial sync has a chance to populate/update the database first.
startup_sync_complete = threading.Event()
