"""
Core SENSE API operations and logic.
This module contains all SENSE-related API calls and business logic,
extracted from the SENSE daemons for better separation of concerns.
"""
import logging
import json
import re
import ipaddress

from sense.client.workflow_combined_api import WorkflowCombinedApi
from sense.client.address_api import AddressApi

def _good_response(response):
    """Check if SENSE API response is valid (no errors)."""
    if not response:
        return False
    if isinstance(response, dict):
        error_val = response.get("error")
        if error_val:
            return False
    return not any("error" in str(r).lower() for r in response)

def format_ipv6(ip):
    """Format IPv6 address for SENSE API compatibility."""
    exploded_ip = ip.network_address.exploded
    segments = exploded_ip.split(":")
    simplified_segments = []
    for segment in segments:
        if segment == "0000":
            simplified_segments.append("0")
        else:
            simplified_segments.append(segment.lstrip("0") or "0")
    return f"{':'.join(simplified_segments)}/{ip.prefixlen}"

def get_instance_status(sense_uuid):
    """
    Get the status of a SENSE instance.
    
    Args:
        sense_uuid: The SENSE instance UUID
        
    Returns:
        Status string or "UNKNOWN" if not available
    """
    workflow_api = WorkflowCombinedApi()
    return workflow_api.instance_get_status(si_uuid=sense_uuid) or "UNKNOWN"

def stage_link(profile_uuid, src_site, dst_site, src_ip_range, dst_ip_range, vlan_range, rule_id):
    """
    Stage a new SENSE link by creating a new instance.
    
    Args:
        profile_uuid: SENSE service profile UUID
        src_site: Source site object with sense_uri
        dst_site: Destination site object with sense_uri
        src_ip_range: Source endpoint IPv6 range
        dst_ip_range: Destination endpoint IPv6 range
        vlan_range: VLAN range for the connection
        rule_id: Rule ID to use as alias
        
    Returns:
        Response dict containing service_uuid, bandwidth info, and URIs
        
    Raises:
        ValueError: If staging fails
    """
    workflow_api = None
    try:
        workflow_api = WorkflowCombinedApi()
        workflow_api.instance_new()
        intent = {
            "service_profile_uuid": profile_uuid,
            "queries": [
                {
                    "ask": "edit",
                    "options": [
                        {"data.connections[0].terminals[0].uri": src_site.sense_uri},
                        {"data.connections[0].terminals[0].ipv6_prefix_list": src_ip_range},
                        {"data.connections[0].terminals[1].uri": dst_site.sense_uri},
                        {"data.connections[0].terminals[1].ipv6_prefix_list": dst_ip_range},
                        {"data.connections[0].terminals[0].vlan_tag": vlan_range},
                        {"data.connections[0].terminals[1].vlan_tag": vlan_range}
                    ]
                }, 
                {
                    "ask": "total-block-maximum-bandwidth",
                    "options": [
                        {
                            "name": "Connection 1",
                            "start": "now",
                            "end-before": "+24h"
                        }
                    ]
                },
                {
                    "ask": "extract-result-values",
                    "options": [
                        {
                            "sparql": "SELECT DISTINCT ?ipv6_subnet_uri ?ipv6_subnet WHERE {?route mrs:routeFrom ?ipv6_subnet_uri. ?ipv6_subnet_uri mrs:type 'ipv6-prefix-list'. ?ipv6_subnet_uri mrs:value ?ipv6_subnet}"
                        }
                    ]
                }
            ],
            "alias": rule_id
        }
        response = workflow_api.instance_create(json.dumps(intent))
        if not _good_response(response):
            raise ValueError(f"SENSE req staging failed for {rule_id}")
        return response
    except Exception as e:
        logging.error(f"Failed to stage link for {rule_id}: {e}")
        if workflow_api and getattr(workflow_api, "si_uuid", None):
            try:
                workflow_api.instance_delete(si_uuid=workflow_api.si_uuid)  # Clean up any created instance
            except Exception as cleanup_err:
                logging.error(f"Failed to clean up partially created SENSE instance for {rule_id}: {cleanup_err}")
        raise ValueError(f"Failed to stage link for {rule_id}") from e

def parse_staging_response(response):
    """
    Parse the response from stage_link to extract relevant information.
    
    Args:
        response: Response dict from stage_link
        
    Returns:
        Tuple of (sense_uuid, bandwidth_mbps, src_uri, dst_uri)
    """
    if not isinstance(response, dict):
        raise ValueError(f"Invalid staging response type: {type(response)}")

    sense_uuid = response.get("service_uuid")
    queries = response.get("queries")
    if not sense_uuid:
        raise ValueError("Missing service_uuid in staging response")
    if not isinstance(queries, list) or len(queries) < 3:
        raise ValueError("Staging response has incomplete queries payload")

    bw_query = queries[1] if isinstance(queries[1], dict) else {}
    bw_results = bw_query.get("results", [])
    if not bw_results:
        raise ValueError("Missing bandwidth results in staging response")
    bandwidth_mbps = int((bw_results[0] or {}).get("bandwidth", 0))

    extract_query = queries[2] if isinstance(queries[2], dict) else {}
    extract_results = extract_query.get("results", [])
    if len(extract_results) < 2:
        raise ValueError("Missing endpoint extraction results in staging response")

    src_uri = (extract_results[0] or {}).get("ipv6_subnet_uri")
    dst_uri = (extract_results[1] or {}).get("ipv6_subnet_uri")
    if not src_uri or not dst_uri:
        raise ValueError("Missing source or destination IPv6 subnet URI in staging response")

    return sense_uuid, bandwidth_mbps, src_uri, dst_uri

def provision_link(sense_uuid, profile_uuid, bandwidth_mbps, src_site, dst_site, 
                   src_ip_range, dst_ip_range, vlan_range, rule_id):
    """
    Provision a SENSE link (commit and activate).
    
    Args:
        sense_uuid: SENSE instance UUID
        profile_uuid: SENSE service profile UUID
        bandwidth_mbps: Bandwidth to provision in Mbps
        src_site: Source site object with sense_uri
        dst_site: Destination site object with sense_uri
        src_ip_range: Source endpoint IPv6 range
        dst_ip_range: Destination endpoint IPv6 range
        vlan_range: VLAN range for the connection
        rule_id: Rule ID for logging
        
    Returns:
        API response
        
    Raises:
        ValueError: If provisioning fails
    """
    try:
        workflow_api = WorkflowCombinedApi()
        workflow_api.si_uuid = sense_uuid
        intent = {
            "service_profile_uuid": profile_uuid,
            "queries": [
                {
                    "ask": "edit",
                    "options": [
                        {"data.connections[0].bandwidth.capacity": str(int(bandwidth_mbps))},
                        {"data.connections[0].terminals[0].uri": src_site.sense_uri},
                        {"data.connections[0].terminals[0].ipv6_prefix_list": src_ip_range},
                        {"data.connections[0].terminals[1].uri": dst_site.sense_uri},
                        {"data.connections[0].terminals[1].ipv6_prefix_list": dst_ip_range},
                        {"data.connections[0].terminals[0].vlan_tag": vlan_range},
                        {"data.connections[0].terminals[1].vlan_tag": vlan_range}
                    ]
                }
            ],
            "alias": rule_id
        }
        response = workflow_api.instance_create(json.dumps(intent))
        if not _good_response(response):
            raise ValueError(f"Failed to create instance for request {rule_id}, response: {response}")
        workflow_api.instance_operate("provision", sync="true")
        return response
    except Exception as e:
        logging.error(f"Failed to provision request {rule_id}: {e}")
        raise

def modify_link(sense_uuid, profile_uuid, bandwidth_mbps, src_site, dst_site,
                src_ip_range, dst_ip_range, vlan_range, rule_id):
    """
    Modify an existing SENSE link's bandwidth.
    
    Args:
        sense_uuid: SENSE instance UUID
        profile_uuid: SENSE service profile UUID
        bandwidth_mbps: New bandwidth in Mbps
        src_site: Source site object with sense_uri
        dst_site: Destination site object with sense_uri
        src_ip_range: Source endpoint IPv6 range
        dst_ip_range: Destination endpoint IPv6 range
        vlan_range: VLAN range for the connection
        rule_id: Rule ID for logging
        
    Returns:
        API response
        
    Raises:
        Exception: If modification fails
    """
    try:
        workflow_api = WorkflowCombinedApi()
        workflow_api.si_uuid = sense_uuid
        intent = {
            "service_profile_uuid": profile_uuid,
            "queries": [
                {
                    "ask": "edit",
                    "options": [
                        {"data.connections[0].bandwidth.capacity": str(int(bandwidth_mbps))},
                        {"data.connections[0].terminals[0].uri": src_site.sense_uri},
                        {"data.connections[0].terminals[0].ipv6_prefix_list": src_ip_range},
                        {"data.connections[0].terminals[1].uri": dst_site.sense_uri},
                        {"data.connections[0].terminals[1].ipv6_prefix_list": dst_ip_range},
                        {"data.connections[0].terminals[0].vlan_tag": vlan_range},
                        {"data.connections[0].terminals[1].vlan_tag": vlan_range}
                    ]
                }
            ],
            "alias": rule_id
        }
        response = workflow_api.instance_modify(json.dumps(intent), sync="true")
        return response
    except Exception as e:
        logging.error(f"Failed to modify request {rule_id}: {e}")
        raise

def cancel_link(sense_uuid, status=None):
    """
    Cancel a SENSE link.
    
    Args:
        sense_uuid: SENSE instance UUID
        status: Current circuit status (optional, for determining force cancel)
        
    Returns:
        API response
    """
    workflow_api = WorkflowCombinedApi()
    force_cancel = "READY" not in (status or "")
    response = workflow_api.instance_operate("cancel", si_uuid=sense_uuid, sync="true", force=str(force_cancel).lower())
    return response


def delete_instance(sense_uuid):
    """
    Delete a SENSE instance.
    
    Args:
        sense_uuid: SENSE instance UUID
        
    Returns:
        API response
    """
    workflow_api = WorkflowCombinedApi()
    response = workflow_api.instance_delete(si_uuid=sense_uuid)
    return response

from dmm.models.request import SenseCircuitStatus

def is_cancel_ready(status):
    """Check if instance is in CANCEL-READY state."""
    return status == SenseCircuitStatus.CANCEL_READY.value

def is_create_compiled(status):
    """Check if instance is in CREATE-COMPILED state."""
    return status == SenseCircuitStatus.CREATE_COMPILED.value

def is_ready_for_cancel(status):
    """Check if instance is in a state that can be cancelled."""
    return status in {
        SenseCircuitStatus.CREATE_READY.value,
        SenseCircuitStatus.MODIFY_READY.value,
        SenseCircuitStatus.REINSTATE_READY.value
    }

def is_create_ready(status):
    """Check if instance is in CREATE-READY state."""
    return status == SenseCircuitStatus.CREATE_READY.value

def is_create_failed(status):
    """Check if instance creation has failed."""
    return status == SenseCircuitStatus.CREATE_FAILED.value

def is_ready_for_modify(status):
    """Check if instance is in a state that can be modified."""
    return status in {
        SenseCircuitStatus.CREATE_READY.value,
        SenseCircuitStatus.MODIFY_READY.value,
        SenseCircuitStatus.REINSTATE_READY.value
    }

def is_being_modified(status):
    """Check if instance is currently being modified."""
    return status in {
        SenseCircuitStatus.MODIFY_COMMITTING.value,
        SenseCircuitStatus.MODIFY_COMMITTED.value
    }

def is_being_provisioned(status):
    """Check if instance is currently being provisioned."""
    return status in {
        SenseCircuitStatus.CREATE_COMMITTING.value,
        SenseCircuitStatus.CREATE_COMMITTED.value,
        SenseCircuitStatus.MODIFY_COMMITTING.value,
        SenseCircuitStatus.MODIFY_COMMITTED.value
    }

def is_affiliated_state(status):
    """Check if instance is in a state where endpoints should be affiliated."""
    return status in {
        SenseCircuitStatus.CREATE_COMPILED.value,
        SenseCircuitStatus.CREATE_COMMITTED.value,
        SenseCircuitStatus.CREATE_COMMITTING.value,
        SenseCircuitStatus.CREATE_READY.value
    }

def affiliate_endpoints(sense_uuid, src_site_name, dst_site_name, src_ip_range, dst_ip_range, 
                        sense_src_uri, sense_dst_uri):
    """
    Affiliate endpoints with a SENSE instance.
    
    Args:
        sense_uuid: SENSE instance UUID
        src_site_name: Source site name
        dst_site_name: Destination site name
        src_ip_range: Source endpoint IPv6 range
        dst_ip_range: Destination endpoint IPv6 range
        sense_src_uri: SENSE source URI
        sense_dst_uri: SENSE destination URI
        
    Raises:
        Exception: If affiliation fails
    """
    address_api = AddressApi()
    
    try:
        src_pool_name = f'RUCIO_Site_BGP_Subnet_Pool-{src_site_name}'
        exploded_source_ip = format_ipv6(ipaddress.IPv6Network(src_ip_range))
        logging.debug(f"Affiliating source endpoint {exploded_source_ip} with SENSE instance {sense_uuid} in address pool {src_pool_name}")
        address_api.affiliate_address(pool=src_pool_name, uri=sense_src_uri, address=exploded_source_ip)

        dst_pool_name = f'RUCIO_Site_BGP_Subnet_Pool-{dst_site_name}'
        exploded_dest_ip = format_ipv6(ipaddress.IPv6Network(dst_ip_range))
        logging.debug(f"Affiliating destination endpoint {exploded_dest_ip} with SENSE instance {sense_uuid} in address pool {dst_pool_name}")
        address_api.affiliate_address(pool=dst_pool_name, uri=sense_dst_uri, address=exploded_dest_ip)
        
        logging.info(f"Successfully affiliated endpoints for SENSE instance {sense_uuid}")
    except Exception as e:
        logging.error(f"Failed to affiliate endpoints for SENSE instance {sense_uuid}: {e}", exc_info=True)
        raise
