import logging
import re

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request

from sense.client.workflow_combined_api import WorkflowCombinedApi

class SENSEDeleterDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
    
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_cancelled = Request.get_by_status(statuses=["CANCELED"], session=session)
        if reqs_cancelled == []:
            return
            
        for req in reqs_cancelled:
            if req.sense_uuid is None:
                logging.debug(f"Request {req.rule_id} has no SENSE UUID, marking as DELETED")
                req.set_status(status="DELETED", session=session)
                continue
                
            try:
                workflow_api = WorkflowCombinedApi()
                status = req.sense_circuit_status
                
                # Only delete if in CANCEL-READY or if status is None/UNKNOWN
                if status and not re.match(r"(CANCEL) - READY$", status):
                    logging.debug(f"Request {req.sense_uuid} not in cancel-ready status (current: {status}), will try to delete again later")
                    continue
                    
                logging.info(f"Deleting SENSE instance {req.sense_uuid} for request {req.rule_id}")
                response = workflow_api.instance_delete(si_uuid=req.sense_uuid)
                req.set_status(status="DELETED", session=session)
                logging.info(f"Successfully deleted SENSE instance for request {req.rule_id}")
                
            except Exception as e:
                logging.error(f"Failed to delete link for {req.rule_id}: {e}", exc_info=True)