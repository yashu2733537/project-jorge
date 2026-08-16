"""jorge pursuit — a persistent multi-step task queue with background workers.

Pieces:
  schema  — the JSON workflow DSL, validation, template rendering, safe expressions
  store   — SQLite-backed task queue with per-step checkpoints
  notify  — notification hooks (email, ntfy.sh, gotify)
  steps   — step executors mapping DSL tools onto jorge's capabilities
  worker  — the background worker that polls the queue and runs workflows
"""

__version__ = "0.1.0"

from .schema import validate_definition, normalize_definition, render_params  # noqa: F401
from .store import submit_workflow, get_task, list_tasks, cancel_task, requeue_task  # noqa: F401