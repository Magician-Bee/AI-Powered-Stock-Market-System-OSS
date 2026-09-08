from .base import WorkerProfile


PROFILE = WorkerProfile(
    "broker",
    "dedicated_broker_sdk_process",
    120,
    900,
)
