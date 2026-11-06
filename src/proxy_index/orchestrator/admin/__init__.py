"""The admin JSON API (/admin/api) behind the Astro operator UI.

routes.py serves it; views.py and views_dashboard.py hold the readers and writers.py the
actions.
"""

from . import routes, views

__all__ = ["routes", "views"]
