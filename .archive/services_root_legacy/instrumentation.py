import time
import threading
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class Instrumentation:
    """Lightweight in-process instrumentation helper.

    Not a full metrics backend — records simple counters/timings and exposes
    a summary() method for periodic logging or tests.
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._jobs = {}
        self._api_timings = defaultdict(list)
        self._counters = defaultdict(int)

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = Instrumentation()
        return cls._instance

    def start_job(self, job_type: str, job_id: str, meta: dict = None):
        key = f"{job_type}:{job_id}"
        self._jobs[key] = {'start': time.time(), 'meta': meta}
        self._counters[f'jobs_started:{job_type}'] += 1

    def record_api_call(self, endpoint: str, duration_ms: float, status_code: int = None):
        self._api_timings[endpoint].append({'ms': duration_ms, 'status': status_code})
        self._counters['api_calls'] += 1

    def end_job(self, job_type: str, job_id: str, success: bool = True):
        key = f"{job_type}:{job_id}"
        rec = self._jobs.pop(key, None)
        if rec:
            total = (time.time() - rec['start']) * 1000.0
            self._counters[f'jobs_total_ms:{job_type}'] += int(total)
            self._counters[f'jobs_finished:{job_type}'] += 1
            if not success:
                self._counters[f'jobs_failed:{job_type}'] += 1

    def summary(self):
        # Simple textual summary useful for logs or tests
        out = []
        out.append('Instrumentation summary:')
        out.append(f"  counters: {dict(self._counters)}")
        api_summary = {k: {'count': len(v), 'avg_ms': sum(x['ms'] for x in v) / len(v) if v else 0} for k, v in self._api_timings.items()}
        out.append(f"  api_summary: {api_summary}")
        s = '\n'.join(out)
        logger.info(s)
        return s


instr = Instrumentation.get()
