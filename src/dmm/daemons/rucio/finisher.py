import logging
from datetime import datetime

from dmm.daemons.base import DaemonBase

from dmm.models.request import Request
from dmm.db.session import databased

from rucio.common.exception import RuleNotFound

class RucioFinisherDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)

    def process(self, **kwargs):
        self.run_once(**kwargs)
    
    @databased
    def run_once(self, client=None, session=None):
        reqs = Request.get_by_status(statuses=["ALLOCATED", "STAGED", "DECIDED", "PROVISIONED"], session=session)
        if not reqs:
            return
        
        for req in reqs:
            self._process_request(req, client, session)

    def _process_request(self, req, client, session):
        try:
            status = client.get_replication_rule(req.rule_id)['state']
        except RuleNotFound as e:
            logging.error(f"Request {req.rule_id} not found in Rucio (probably because of the reaper), marking as FINISHED in DMM")
            req.set_status(status="FINISHED", session=session)
            return
            
        if status == "OK":
            logging.debug(f"Request {req.rule_id} finished with status {status}")
            req.set_status(status="FINISHED", session=session)  # Mark request as finished
            req.update({"rucio_finished_at": datetime.now()}, session=session)
            req.set_fts_streams(current=0, session=session)  # Remove FTS limits
        elif status == "STUCK":
            logging.debug(f"Request {req.rule_id} is stuck, marking as FINISHED in DMM so circuit can be taken down")
            req.set_status(status="FINISHED", session=session)