"""The operator's dashboard: one page answering "is collection working, and what does it hold?".

It is served by the overlay service (`GET /` and `GET /api/status`), so devkit's dashboard
gateway, which already starts and sleeps `overlay-serve` on port 8765, serves it too.
Everything it shows is read from artifacts this project already writes; it calls no
provider and spends no quota.
"""

from sports_betting.dashboard.page import PAGE
from sports_betting.dashboard.status import StatusSources, build_status

__all__ = ["PAGE", "StatusSources", "build_status"]
