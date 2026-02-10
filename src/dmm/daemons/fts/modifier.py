import logging

from dmm.models.request import Request
from dmm.db.session import databased

from dmm.daemons.base import DaemonBase
from dmm.core.fts import modify_fts_config, delete_fts_config

class FTSModifierDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)

    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        self._process_requests(session, ["ALLOCATED", "DECIDED", "PROVISIONED"], self._modify_request)
        self._process_requests(session, ["DELETED"], self._delete_request)

    def _process_requests(self, session, statuses, action):
        reqs = Request.get_by_status(statuses=statuses, session=session)
        if reqs:
            for req in reqs:
                action(req, session)

    def _modify_request(self, req, session):
        if req.fts_streams_current != req.fts_streams_desired:
            logging.debug(f"Modifying FTS limits for request {req.rule_id}, from {req.fts_streams_current} to {req.fts_streams_desired}")
            if modify_fts_config(req.src_endpoint, req.dst_endpoint, req.fts_streams_desired):
                req.set_fts_streams(current=req.fts_streams_desired, session=session)

    def _delete_request(self, req, session):
        if req.fts_streams_current != 0:
            logging.debug(f"Deleting FTS limits for request {req.rule_id}")
            delete_fts_config(req.src_endpoint, req.dst_endpoint)
            req.set_fts_streams(current=0, session=session)
