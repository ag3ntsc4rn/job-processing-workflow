"""Worker: consumes jobs from Kafka and runs them.

The only type-aware component. A registry maps ``job_type`` -> a handler
function; adding a new job type means adding a handler here and redeploying the
worker, nothing else. The worker claims each job with a compare-and-set (so a
redelivered message can't double-run), executes the handler, and records
``completed``/``failed``.
"""
