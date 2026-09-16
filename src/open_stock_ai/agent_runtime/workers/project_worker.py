from .base import WorkerProfile


# File and Git changes are executed only through host project capabilities;
# their before/after evidence and rollback token remain in the same Run.
PROFILE = WorkerProfile("project", "dedicated_worker_process", 90, 600)
WORKER_TYPE = PROFILE.worker_type
