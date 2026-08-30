"""Compatibility facade for the packaged Phase-3 shadow foundation.

The canonical implementation is ``factory_core.phase3_artifacts``.  This
direct-test-only module intentionally contains no second set of domain rules.
"""

from factory_core.phase3_artifacts import *  # noqa: F401,F403
from factory_core.phase3_artifacts import __all__
