import time
from collections import deque


current_status = "대기 중..."
_job_status = {}
_job_events = {}


def set_status(msg: str, job_id: str = None, metadata: dict = None):
    global current_status
    current_status = msg
    if job_id:
        _job_status[job_id] = msg
        event = {
            "ts": time.time(),
            "status": msg,
        }
        if metadata:
            event["metadata"] = metadata
        _job_events.setdefault(job_id, deque(maxlen=500)).append(event)


def get_status(job_id: str = None) -> str:
    if job_id:
        return _job_status.get(job_id, current_status)
    return current_status


def get_events(job_id: str):
    return list(_job_events.get(job_id, []))
