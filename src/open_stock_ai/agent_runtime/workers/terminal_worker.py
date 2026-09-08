from .base import WorkerProfile


# terminal.run is backed by the host sandbox subprocess executor, not model
# code.  The supervisor owns cancellation and its durable process record.
PROFILE = WorkerProfile("terminal", "worker_process_and_sandbox_process_group", 120, 1800)
WORKER_TYPE = PROFILE.worker_type
