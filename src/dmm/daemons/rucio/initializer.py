import logging

from dmm.daemons.base import DaemonBase
from dmm.models.request import Request, RequestStatus
from dmm.models.site import Site
from dmm.db.session import databased

from dmm.core.config import config_get_int
from dmm.core.rucio import (
    list_replication_rules,
    get_rule_size,
    parse_rule_for_request,
    is_sense_rule,
    is_rule_ok,
    is_rule_stuck
)

class RucioInitDaemon(DaemonBase):
    """
    Daemon to initialize Rucio rules and create requests in the database.
    """
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)

    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, client=None, session=None) -> None:
        """
        Process Rucio rules and create requests in the database.
        """
        try:
            rules = list_replication_rules(client)
        except Exception as e:
            logging.error(f"Failed to list Rucio rules: {e}", exc_info=True)
            return
            
        for rule in rules:
            try:
                if self._is_rule_in_db(rule, session):
                    logging.debug(f"Rule {rule['id']} already exists in the database.")
                    continue
                
                rule_state = rule.get("state")
                if is_rule_ok(rule_state):
                    logging.debug(f"Rule {rule['id']} is already finished; skipping.")
                    continue
                elif is_rule_stuck(rule_state):
                    logging.debug(f"Rule {rule['id']} is stuck; skipping.")
                    continue

                logging.debug(f"Processing rule {rule['id']}.")
                new_request = self._create_request_from_rule(rule, client, session)
                new_request.save(session=session)
                session.commit()
                logging.info(f"Created new request for rule {rule['id']}.")
                
            except Exception as e:
                logging.error(f"Failed to create request for rule {rule.get('id', 'UNKNOWN')}: {e}", exc_info=True)
                session.rollback()
                continue

    def _is_rule_in_db(self, rule, session) -> bool:
        """
        Check if the rule already exists in the database.
        """
        return Request.get_by_id(rule["id"], session=session, use_lock=False) is not None

    def _create_request_from_rule(self, rule, client, session) -> Request:
        """
        Create a new request from the given rule.
        """
        rule_info = parse_rule_for_request(rule)
        
        src_site_name = rule_info['src_site_name']
        dst_site_name = rule_info['dst_site_name']
        
        if not src_site_name or not dst_site_name:
            raise ValueError(f"Rule {rule_info['rule_id']} missing source or destination site expression")
        
        src_site = Site.get_by_name(src_site_name, session=session, use_lock=False)
        dst_site = Site.get_by_name(dst_site_name, session=session, use_lock=False)
        
        if not src_site:
            raise ValueError(f"Source site '{src_site_name}' not found in database for rule {rule_info['rule_id']}")
        if not dst_site:
            raise ValueError(f"Destination site '{dst_site_name}' not found in database for rule {rule_info['rule_id']}")

        priority = rule_info['priority']
        fts_streams_desired = config_get_int("fts", "default_num_streams", default=20)

        if is_sense_rule(rule):
            logging.debug(f"Rule {rule_info['rule_id']} identified as a SENSE rule.")
            transfer_status = RequestStatus.INIT
        else:
            logging.debug(f"Rule {rule_info['rule_id']} is not a SENSE rule; setting status to 'NOT_SENSE'.")
            transfer_status = RequestStatus.NOT_SENSE

        rule_size = get_rule_size(client, rule_info['scope'], rule_info['name'])

        return Request(
            rule_id=rule_info['rule_id'],
            src_site=src_site,
            dst_site=dst_site,
            priority=priority,
            rule_size=rule_size,
            transfer_status=transfer_status,
            fts_streams_desired=fts_streams_desired,
        )    