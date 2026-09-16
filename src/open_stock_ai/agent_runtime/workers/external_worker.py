from .base import WorkerProfile


# External frameworks are invoked through explicit host adapters and never
# receive a direct model credential or unrestricted Stock AI state.
PROFILE = WorkerProfile("external", "worker_process_with_adapter_process_boundary", 300, 3600)
WORKER_TYPE = PROFILE.worker_type
