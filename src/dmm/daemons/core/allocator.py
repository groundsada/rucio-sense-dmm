import logging

from dmm.daemons.base import DaemonBase

from dmm.models.request import Request, RequestStatus
from dmm.models.endpoint import Endpoint

from dmm.db.session import databased

from dmm.core.allocation import (
    allocate_address,
    free_address,
    format_ipv6_compressed
)

class AllocatorDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_init = Request.get_by_status(statuses=[RequestStatus.INIT], session=session)
        if not reqs_init:
            return

        # Track rule_ids of FINISHED/FINISHED_R requests already claimed this cycle
        # so that two INIT requests for the same site pair don't both try to reuse
        # the same finished request.
        claimed_rule_ids = set()
        
        for new_request in reqs_init:  
            if self._reuse_finished_request(new_request, session, claimed_rule_ids):  # check if we can reuse a finished request
                continue
            
            self._allocate_new_endpoints(new_request, session)  # if not found, allocate new endpoints

    def _reuse_finished_request(self, new_request, session, claimed_rule_ids: set) -> bool:
        reqs_finished_r = Request.get_by_status(statuses=[RequestStatus.FINISHED_R], session=session)
        for req_fin in reqs_finished_r:
            if req_fin.rule_id in claimed_rule_ids:
                continue

            if req_fin.sense_circuit_status == "UNKNOWN":
                logging.debug(
                    f"Skipping reuse of circuit {req_fin.sense_uuid} from {req_fin.rule_id}: "
                    "circuit_status is UNKNOWN — likely cleaned up by SENSE-O ConsistencyService"
                )
                continue

            same_direction = (
                req_fin.src_site == new_request.src_site
                and req_fin.dst_site == new_request.dst_site
            )
            reverse_direction = (
                req_fin.src_site == new_request.dst_site
                and req_fin.dst_site == new_request.src_site
            )

            if same_direction or reverse_direction:
                reused_src_endpoint = req_fin.src_endpoint if same_direction else req_fin.dst_endpoint
                reused_dst_endpoint = req_fin.dst_endpoint if same_direction else req_fin.src_endpoint
                reused_src_uri = req_fin.sense_src_uri if same_direction else req_fin.sense_dst_uri
                reused_dst_uri = req_fin.sense_dst_uri if same_direction else req_fin.sense_src_uri
                reused_src_site = req_fin.src_site if same_direction else req_fin.dst_site
                reused_dst_site = req_fin.dst_site if same_direction else req_fin.src_site

                logging.info(
                    f"Request {new_request.rule_id} found a live circuit from FINISHED request "
                    f"{req_fin.rule_id} for the same site pair — reusing circuit {req_fin.sense_uuid}."
                )

                # Re-register the SENSE-O address pool allocations under the new rule_id.
                # The old entries were registered under req_fin.rule_id; if we don't re-register,
                # release_endpoints_and_addresses will try to free the new rule_id (which doesn't
                # exist in the pool) and leave the old entry allocated forever.
                self._reregister_address_allocations(
                    old_rule_id=req_fin.rule_id,
                    new_rule_id=new_request.rule_id,
                    src_site_name=reused_src_site.name,
                    dst_site_name=reused_dst_site.name,
                )

                new_request.update({
                    "src_endpoint": reused_src_endpoint,
                    "dst_endpoint": reused_dst_endpoint,
                    "sense_uuid": req_fin.sense_uuid,
                    "sense_src_uri": reused_src_uri,
                    "sense_dst_uri": reused_dst_uri,
                    "allocated_bandwidth_mbps": req_fin.allocated_bandwidth_mbps,
                    "available_bandwidth_mbps": req_fin.available_bandwidth_mbps,
                    "sense_circuit_status": req_fin.sense_circuit_status,
                    "sense_affiliated": req_fin.sense_affiliated,
                    "transfer_status": RequestStatus.PROVISIONED
                }, session=session)
                req_fin.set_status(status=RequestStatus.DELETED, session=session)
                claimed_rule_ids.add(req_fin.rule_id)
                return True

        return False

    @staticmethod
    def _reregister_address_allocations(old_rule_id, new_rule_id, src_site_name, dst_site_name):
        """Free the SENSE-O pool entries registered under old_rule_id and re-register under new_rule_id."""
        try:
            free_address(src_site_name, old_rule_id)
            free_address(dst_site_name, old_rule_id)
            allocate_address(src_site_name, new_rule_id)
            allocate_address(dst_site_name, new_rule_id)
            logging.debug(
                f"Re-registered SENSE-O address allocations from {old_rule_id} to {new_rule_id}"
            )
        except Exception as e:
            logging.warning(
                f"Could not re-register SENSE-O address allocations from {old_rule_id} "
                f"to {new_rule_id}: {e}. The circuit is still functional but the pool "
                f"entry for {old_rule_id} may remain allocated."
            )

    def _allocate_new_endpoints(self, new_request, session) -> None:
        """
        Allocate new endpoints for a new request, get endpoints and ip ranges from SENSE-O
        """
        logging.info(f"Allocating endpoints for request {new_request.rule_id}")
        
        # Validate request has required fields
        if not new_request.src_site or not new_request.dst_site:
            new_request.set_status(RequestStatus.FAILED, session=session)
            logging.error(f"Request {new_request.rule_id} is missing source or destination site")
            return
        
        src_allocation = None
        dst_allocation = None
        src_endpoint = None
        dst_endpoint = None
        endpoints_marked = False
        
        try:
            # Get allocations using core allocation functions
            src_allocation = allocate_address(new_request.src_site.name, new_request.rule_id)
            dst_allocation = allocate_address(new_request.dst_site.name, new_request.rule_id)

            # Format IP addresses consistently
            free_src_ipv6 = format_ipv6_compressed(src_allocation)
            free_dst_ipv6 = format_ipv6_compressed(dst_allocation)

            # Atomically check and lock endpoints using SELECT FOR UPDATE
            src_endpoint = Endpoint.get_for_allocation(
                site_name=new_request.src_site.name,
                ip_range=free_src_ipv6,
                session=session
            )
            dst_endpoint = Endpoint.get_for_allocation(
                site_name=new_request.dst_site.name,
                ip_range=free_dst_ipv6,
                session=session
            )

            if not src_endpoint:
                raise ValueError(f"Could not find source endpoint with IP range {free_src_ipv6}")
            if not dst_endpoint:
                raise ValueError(f"Could not find destination endpoint with IP range {free_dst_ipv6}")

            if src_endpoint.is_allocated:
                raise ValueError(f"Source endpoint {free_src_ipv6} is already in use")
            if dst_endpoint.is_allocated:
                raise ValueError(f"Destination endpoint {free_dst_ipv6} is already in use")

            src_endpoint.set_allocated(is_allocated=True, session=session)
            dst_endpoint.set_allocated(is_allocated=True, session=session)
            endpoints_marked = True

            new_request.update({
                "src_endpoint": src_endpoint,
                "dst_endpoint": dst_endpoint,
                "transfer_status": RequestStatus.ALLOCATED
            }, session=session)

            # Let the @databased decorator on run_once own the final commit.
            logging.info(f"Successfully allocated endpoints for request {new_request.rule_id}")

        except Exception as e:
            # Roll back the in-flight DB state so no partial writes escape.
            session.rollback()

            # After rollback, SQLAlchemy expires all objects on the session.
            # Re-fetch the endpoints before touching them so we work with
            # fresh DB state, not stale in-memory copies.
            if endpoints_marked:
                try:
                    if src_endpoint:
                        session.refresh(src_endpoint)
                        src_endpoint.set_allocated(is_allocated=False, session=session)
                    if dst_endpoint:
                        session.refresh(dst_endpoint)
                        dst_endpoint.set_allocated(is_allocated=False, session=session)
                    session.commit()
                    logging.info(f"Freed endpoints for {new_request.rule_id}")
                except Exception as endpoint_err:
                    logging.error(f"Failed to free endpoints for {new_request.rule_id}: {endpoint_err}")
                    session.rollback()

            # Clean up SENSE-O allocations
            if src_allocation:
                try:
                    free_address(new_request.src_site.name, new_request.rule_id)
                    logging.info(f"Freed source allocation for {new_request.rule_id}")
                except Exception as free_err:
                    logging.error(f"Failed to free source allocation for {new_request.rule_id}: {free_err}")

            if dst_allocation:
                try:
                    free_address(new_request.dst_site.name, new_request.rule_id)
                    logging.info(f"Freed destination allocation for {new_request.rule_id}")
                except Exception as free_err:
                    logging.error(f"Failed to free destination allocation for {new_request.rule_id}: {free_err}")

            session.refresh(new_request)
            new_request.set_status(RequestStatus.FAILED, session=session)
            session.commit()
            logging.error(
                f"Failed to allocate endpoints for request {new_request.rule_id}: {str(e)}",
                exc_info=True,
            )