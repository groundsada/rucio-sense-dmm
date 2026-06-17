import logging
from dmm.core.allocation import free_address

def is_sync_timeout(exc) -> bool:
    msg = str(exc).lower()
    return "504" in msg and ("gateway time-out" in msg or "gateway timeout" in msg or "time-out" in msg)

def release_endpoints_and_addresses(req, session):
    """
    Safely marks DB endpoints as free and attempts to release SENSE-O IP locks.
    """
    try:
        if req.src_endpoint:
            req.src_endpoint.set_allocated(is_allocated=False, session=session)
        if req.dst_endpoint:
            req.dst_endpoint.set_allocated(is_allocated=False, session=session)
            
        alloc_rule_id = getattr(req, 'sense_alloc_rule_id', None) or req.rule_id
        if req.src_site:
            try:
                free_address(req.src_site.name, alloc_rule_id)
            except Exception as e:
                logging.debug(f"Failed or skipped freeing src address for {alloc_rule_id}: {e}")

        if req.dst_site:
            try:
                free_address(req.dst_site.name, alloc_rule_id)
            except Exception as e:
                logging.debug(f"Failed or skipped freeing dst address for {alloc_rule_id}: {e}")
    except Exception as e:
        logging.error(f"Error during endpoint release for {req.rule_id}: {e}", exc_info=True)
