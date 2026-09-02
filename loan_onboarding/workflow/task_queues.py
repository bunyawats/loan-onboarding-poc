"""
Task queue naming and the canonical list of known product types.

Shared by workflow/service.py (starts each workflow on a product-type-
specific queue) and workflow/worker.py (polls one such queue per process,
or all of them for a general-purpose worker). This is the single source
of truth for "what product types exist," so the producer side
(application/service.py, via workflow/service.py) and consumer side
(worker.py) can't drift out of sync on the naming scheme.

Deliberately has ZERO dependency on any other module in this codebase --
worker.py imports this directly, and it must never need to import
anything else. application/schemas.py imports FROM here (that direction
is fine -- everything in application/ depending on workflow/ is the
established pattern per CLAUDE.md's dependency graph; the reverse isn't).
"""

# Every product_type the system knows about (PRD §6.1).
# application/schemas.py asserts its own payload-schema registry has
# exactly these keys at import time -- if you add a new product type, you
# MUST update both this tuple and that registry, or the assertion will
# fail loudly at import time rather than silently routing workflows to a
# queue nothing is polling (see CLAUDE.md's "Breaking the cycle").
KNOWN_PRODUCT_TYPES = ("personal_loan", "auto_loan", "mortgage")

# Local-dev fallbacks for TEMPORAL_HOST/TEMPORAL_NAMESPACE -- every
# process that opens its own Temporal Client (application/service.py,
# workflow/worker.py, both BFFs' routes.py) reads the env vars with
# these same defaults; a shared pair here keeps the four call sites
# from independently retyping the same literals.
DEFAULT_TEMPORAL_HOST = "localhost:7233"
DEFAULT_TEMPORAL_NAMESPACE = "default"


def task_queue_for_product_type(product_type: str) -> str:
    return f"loan-onboarding-{product_type}-task-queue"
