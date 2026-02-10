import logging

from dmm.daemons.base import DaemonBase

from dmm.models.request import Request
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
        reqs_init = Request.get_by_status(statuses=["INIT"], session=session)
        if not reqs_init:
            return
        
        for new_request in reqs_init:  
            if self._reuse_finished_request(new_request, session):  # check if we can reuse a finished request
                continue
            
            self._allocate_new_endpoints(new_request, session)  # if not found, allocate new endpoints

    def _reuse_finished_request(self, new_request, session) -> bool:
        """
        Check if there is a finished request with the same src and dst site. if found: reuse its endpoints and return True
        """
        reqs_finished = Request.get_by_status(statuses=["FINISHED"], session=session)
        for req_fin in reqs_finished:
            if req_fin.src_site == new_request.src_site and req_fin.dst_site == new_request.dst_site:
                logging.debug(f"Request {new_request.rule_id} found a finished request {req_fin.rule_id} with same endpoints, reusing ipv6 blocks and urls.")
                new_request.update({
                    "src_endpoint": req_fin.src_endpoint,
                    "dst_endpoint": req_fin.dst_endpoint,
                    "transfer_status": "ALLOCATED"
                })
                req_fin.set_status(status="DELETED", session=session)
                return True
        return False

    def _allocate_new_endpoints(self, new_request, session) -> None:
        """
        Allocate new endpoints for a new request, get endpoints and ip ranges from SENSE-O
        """
        logging.info(f"Allocating endpoints for request {new_request.rule_id}")
        
        # Validate request has required fields
        if not new_request.src_site or not new_request.dst_site:
            new_request.set_status("FAILED", session=session)
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
            # This prevents race conditions between checking allocation status and marking as allocated
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
            
            # Validate endpoints exist
            if not src_endpoint:
                raise ValueError(f"Could not find source endpoint with IP range {free_src_ipv6}")
            if not dst_endpoint:
                raise ValueError(f"Could not find destination endpoint with IP range {free_dst_ipv6}")
            
            # Check if endpoints are already in use (now safe because we have the lock)
            if src_endpoint.is_allocated:
                raise ValueError(f"Source endpoint {free_src_ipv6} is already in use")
            if dst_endpoint.is_allocated:
                raise ValueError(f"Destination endpoint {free_dst_ipv6} is already in use")
            
            # Mark endpoints as allocated
            src_endpoint.set_allocated(is_allocated=True, session=session)
            dst_endpoint.set_allocated(is_allocated=True, session=session)
            endpoints_marked = True
                
            # Update request with allocated endpoints
            new_request.update({
                "src_endpoint": src_endpoint,
                "dst_endpoint": dst_endpoint,
                "transfer_status": "ALLOCATED"
            })
            
            # Commit the session to ensure database consistency
            session.commit()
            
            logging.info(f"Successfully allocated endpoints for request {new_request.rule_id}")
            
        except Exception as e:
            # Rollback any database changes
            session.rollback()
            
            # Free endpoints if they were marked as allocated
            if endpoints_marked:
                try:
                    if src_endpoint:
                        src_endpoint.set_allocated(is_allocated=False, session=session)
                    if dst_endpoint:
                        dst_endpoint.set_allocated(is_allocated=False, session=session)
                    session.commit()
                    logging.info(f"Freed endpoints for {new_request.rule_id}")
                except Exception as endpoint_err:
                    logging.error(f"Failed to free endpoints for {new_request.rule_id}: {endpoint_err}")
                    session.rollback()
            
            # Clean up SENSE-O allocations using core allocation functions
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
                
            # Mark request as failed
            new_request.set_status("FAILED", session=session)
            session.commit()
            logging.error(f"Failed to allocate endpoints for request {new_request.rule_id}: {str(e)}", exc_info=True)