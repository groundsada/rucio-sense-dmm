import logging
from dmm.core.allocation import free_address

def release_endpoints_and_addresses(req, session):
    """
    Safely marks DB endpoints as free and attempts to release SENSE-O IP locks.
    """
    try:
        if req.src_endpoint:
            req.src_endpoint.set_allocated(is_allocated=False, session=session)
        if req.dst_endpoint:
            req.dst_endpoint.set_allocated(is_allocated=False, session=session)
            
        if req.src_site:
            try:
                free_address(req.src_site.name, req.rule_id)
            except Exception as e:
                logging.debug(f"Failed or skipped freeing src address for {req.rule_id}: {e}")
                
        if req.dst_site:
            try:
                free_address(req.dst_site.name, req.rule_id)
            except Exception as e:
                logging.debug(f"Failed or skipped freeing dst address for {req.rule_id}: {e}")
    except Exception as e:
        logging.error(f"Error during endpoint release for {req.rule_id}: {e}", exc_info=True)
