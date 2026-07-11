"""Built-in job-type handlers.

Importing this package registers every handler as a side effect. Add a new
job type by adding a module here (or a function) decorated with
``@register("your_type")``.
"""

from worker.handlers import demo  # noqa: F401  (import registers the handlers)
