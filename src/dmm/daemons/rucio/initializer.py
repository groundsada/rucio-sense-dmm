import logging

from dmm.daemons.base import DaemonBase
from dmm.models.request import Request
from dmm.models.site import Site
from dmm.db.session import databased

from dmm.core.config import config_get_int

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
            rules = client.list_replication_rules()
        except Exception as e:
            logging.error(f"Failed to list Rucio rules: {e}", exc_info=True)
            return
            
        for rule in rules:
            try:
                if self._is_rule_in_db(rule, session):
                    logging.debug(f"Rule {rule['id']} already exists in the database.")
                    continue
                
                rule_state = rule.get("state")
                if rule_state == "OK":
                    logging.debug(f"Rule {rule['id']} is already finished; skipping.")
                    continue
                elif rule_state == "STUCK":
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

    def _get_rule_size(self, rule, client) -> int:
        """
        Get the total size of the files in the rule (in bytes).
        """
        try:
            files = list(client.list_files(scope=rule["scope"], name=rule["name"]))
            total_bytes = sum([f.get("bytes", 0) for f in files if f.get("bytes") is not None])
            return total_bytes if total_bytes > 0 else 0
        except Exception as e:
            logging.error(f"Failed to get rule size for rule {rule['id']}: {e}", exc_info=True)
            return 0  # Return 0 instead of None to avoid database issues

    def _create_request_from_rule(self, rule, client, session) -> Request:
        """
        Create a new request from the given rule.
        """
        src_site_name = rule.get("source_replica_expression")
        dst_site_name = rule.get("rse_expression")
        
        if not src_site_name or not dst_site_name:
            raise ValueError(f"Rule {rule['id']} missing source or destination site expression")
        
        src_site = Site.get_by_name(src_site_name, session=session, use_lock=False)
        dst_site = Site.get_by_name(dst_site_name, session=session, use_lock=False)
        
        if not src_site:
            raise ValueError(f"Source site '{src_site_name}' not found in database for rule {rule['id']}")
        if not dst_site:
            raise ValueError(f"Destination site '{dst_site_name}' not found in database for rule {rule['id']}")

        priority = rule.get("priority", 3)  # Default priority if not specified
        fts_streams_desired = config_get_int("fts", "default_num_streams", default=20)

        activity = rule.get("activity")  # activity for SENSE rules contains SENSE
        if activity and "sense" in activity.lower():
            logging.debug(f"Rule {rule['id']} identified as a SENSE rule.")
            transfer_status = "INIT"
        else:
            logging.debug(f"Rule {rule['id']} is not a SENSE rule; setting status to 'NOT_SENSE'.")
            transfer_status = "NOT_SENSE"

        rule_size = self._get_rule_size(rule, client)

        return Request(
            rule_id=rule["id"],
            src_site=src_site,
            dst_site=dst_site,
            priority=priority,
            rule_size=rule_size,
            transfer_status=transfer_status,
            fts_streams_desired=fts_streams_desired,
        )    