from time import time

from dmm.daemons.base import DaemonBase
from dmm.db.session import databased
from dmm.core.allocation import refresh_all_sites
from dmm.core.metrics import SITES_LAST_REFRESH

class RefreshSiteDBDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
    
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, client=None, session=None) -> None:
        """
        Refresh sites from Rucio and SENSE.
        Delegates to core/allocation.refresh_all_sites for the actual logic.
        """
        refresh_all_sites(client, session)
        SITES_LAST_REFRESH.set(time())