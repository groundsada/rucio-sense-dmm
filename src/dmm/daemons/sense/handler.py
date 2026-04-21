import logging
from datetime import datetime
from collections import defaultdict
from math import floor

from dmm.core.config import config_get_int
from dmm.daemons.base import DaemonBase
from dmm.models.request import Request, RequestStatus
from dmm.db.session import databased

from dmm.core.sense import (
    get_instance_status,
    affiliate_endpoints,
    is_affiliated_state,
    is_create_ready,
    is_create_failed
)
from dmm.core.utils import release_endpoints_and_addresses

class SENSEHandlerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @staticmethod
    def _split_streams_proportionally(reqs, max_streams: int) -> dict:
        if not reqs:
            return {}

        max_streams = max(0, int(max_streams or 0))
        n_reqs = len(reqs)

        if max_streams == 0:
            return {req.rule_id: 0 for req in reqs}

        priorities = [max(0, int(req.priority or 0)) for req in reqs]
        total_priority = sum(priorities)

        exact_shares = [max_streams * (prio / total_priority) for prio in priorities]
        floored_shares = [floor(share) for share in exact_shares]
        allocation = {req.rule_id: floored_shares[idx] for idx, req in enumerate(reqs)}

        assigned = sum(floored_shares)
        remainder = max_streams - assigned
        if remainder > 0:
            ranked = sorted(
                [(idx, exact_shares[idx] - floored_shares[idx], priorities[idx], reqs[idx].rule_id) for idx in range(n_reqs)],
                key=lambda x: (-x[1], -x[2], x[3])
            )
            for i in range(remainder):
                req_idx = ranked[i][0]
                allocation[reqs[req_idx].rule_id] += 1

        return allocation

    @staticmethod
    def _pair_stream_cap(src_site_name: str, dst_site_name: str) -> int:
        pair_key = f"{src_site_name}-{dst_site_name}"
        default_streams = config_get_int("fts", "default_num_streams", default=20)
        return config_get_int("fts-streams", pair_key, default=default_streams)

    def _rebalance_fts_streams(self, session) -> None:
        active_reqs = Request.get_by_status(
            statuses=[RequestStatus.PROVISIONED],
            session=session,
        )
        if not active_reqs:
            return

        grouped_by_pair = defaultdict(list)
        for req in active_reqs:
            if not is_create_ready(req.sense_circuit_status):
                continue
            if not req.src_site or not req.dst_site:
                continue
            if not req.src_endpoint or not req.dst_endpoint:
                continue
            grouped_by_pair[(req.src_site.name, req.dst_site.name)].append(req)

        for (src_site_name, dst_site_name), reqs in grouped_by_pair.items():
            max_streams = self._pair_stream_cap(src_site_name, dst_site_name)
            allocations = self._split_streams_proportionally(reqs, max_streams)

            for req in reqs:
                desired = allocations.get(req.rule_id, 0)
                if req.fts_streams_desired != desired:
                    req.set_fts_streams(desired=desired, session=session)
                    logging.info(
                        f"Rebalanced FTS streams for {req.rule_id} on {src_site_name}->{dst_site_name}: "
                        f"priority={req.priority}, desired={desired}, pair_max={max_streams}"
                    )

    @staticmethod
    def _retry_target_status(req) -> RequestStatus:
        """
        Decide where to resume after RETRY.
        - If a SENSE instance already exists, resume from DECIDED (re-provision path)
        - Otherwise resume from ALLOCATED (re-stage path)
        """
        return RequestStatus.DECIDED if req.sense_uuid else RequestStatus.ALLOCATED

    @databased
    def run_once(self, session=None):
        reqs = Request.get_by_status(statuses=[RequestStatus.RETRY, RequestStatus.STAGED, RequestStatus.PROVISIONED, RequestStatus.CANCELED, RequestStatus.STALE, RequestStatus.DECIDED], session=session)
        if not reqs:
            return
        
        for req in reqs:
            if req.transfer_status == RequestStatus.RETRY:
                if req.sense_retries < config_get_int("sense", "max_retries", default=3):
                    logging.info(f"Request {req.rule_id} has {req.sense_retries} retries, less than max retries. Retrying.")
                    req.increment_sense_retries(session=session)
                    target_status = self._retry_target_status(req)
                    req.set_status(target_status, session=session)
                else:
                    logging.warning(f"Request {req.rule_id} has reached max SENSE retries. Marking as failed.")
                    req.set_status(RequestStatus.FAILED, session=session)
                    release_endpoints_and_addresses(req, session)
            
            if req.sense_uuid is None:
                continue

            status = get_instance_status(req.sense_uuid)
            req.set_sense_circuit_status(status=status, session=session)

            # Affiliate endpoints when ready
            if not req.sense_affiliated and is_affiliated_state(status):
                logging.debug(f"Request {req.rule_id} is not affiliated with SENSE instance {req.sense_uuid}, affiliating now.")
                affiliate_endpoints(
                    sense_uuid=req.sense_uuid,
                    src_site_name=req.src_site.name,
                    dst_site_name=req.dst_site.name,
                    src_ip_range=req.src_endpoint.ip_range,
                    dst_ip_range=req.dst_endpoint.ip_range,
                    sense_src_uri=req.sense_src_uri,
                    sense_dst_uri=req.sense_dst_uri
                )
                req.update({"sense_affiliated": True}, session=session)

            # Update sense_provisioned_at if the status is READY for monitoring
            if not req.sense_provisioned_at and is_create_ready(status):
                logging.debug(f"Request {req.rule_id} is ready, updating sense_provisioned_at to current time.")
                req.update({"sense_provisioned_at": datetime.now()}, session=session)

            elif req.transfer_status in [RequestStatus.PROVISIONED] and is_create_failed(status):
                logging.warning(
                    f"Request {req.rule_id} reached CREATE_FAILED after being PROVISIONED; "
                    "marking as RETRY to re-enter SENSE retry flow"
                )
                req.set_status(RequestStatus.RETRY, session=session)

        self._rebalance_fts_streams(session)
                
