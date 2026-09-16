from .base import WorkerProfile


# Browser tools keep their Playwright profile under Stock AI's runtime root.
PROFILE = WorkerProfile("browser", "worker_process_with_isolated_playwright_profile", 120, 900)
WORKER_TYPE = PROFILE.worker_type
