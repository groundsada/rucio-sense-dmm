"""
Core allocation and site management operations.
This module contains all allocation-related logic and SENSE discovery API calls,
extracted from the allocator and sites daemons for better separation of concerns.
"""
import logging
import json
import ipaddress

from sense.client.address_api import AddressApi
from sense.client.discover_api import DiscoverApi
from sense.client.workflow_combined_api import WorkflowCombinedApi

from dmm.models.site import Site
from dmm.models.mesh import Mesh
from dmm.models.endpoint import Endpoint
from dmm.core.rucio import list_rses, get_rse_protocol
from dmm.core.config import config_get

def _good_response(response):
    if not response:
        return False
    if isinstance(response, dict):
        if response.get("error"):
            return False
    return not any("error" in str(r).lower() for r in response)

def allocate_address(sitename, alloc_name):
    """
    Allocate an IPv6 address from SENSE-O address pool.
    
    Args:
        sitename: Name of the site (used for the address pool)
        alloc_name: Alias for the allocation used in SENSE-O (typically rule_id)
        
    Returns:
        Allocated IPv6 address string
        
    Raises:
        Exception: If allocation fails
    """
    address_api = AddressApi()
    pool_name = f"RUCIO_Site_BGP_Subnet_Pool-{sitename}"
    alloc_type = "IPv6"
    try:
        logging.debug(f"Getting IPv6 allocation for {sitename}")
        response = address_api.allocate_address(pool_name, alloc_type, alloc_name, netmask="/64", batch="subnet")
        logging.debug(f"Got allocation: {response} for {sitename}")
        return response
    except Exception as e:
        logging.error(f"allocate_address: {str(e)}")
        try:
            address_api.free_address(pool_name, name=alloc_name)
        except Exception as cleanup_err:
            logging.error(
                f"allocate_address cleanup failed for pool={pool_name}, alloc_name={alloc_name}: {cleanup_err}",
                exc_info=True,
            )
        raise

def free_address(sitename, alloc_name):
    """
    Free an IPv6 address allocation from SENSE-O.
    
    Args:
        sitename: Name of the site (used for the address pool)
        alloc_name: Alias for the allocation to free
        
    Returns:
        True if successful
        
    Raises:
        ValueError: If freeing fails
    """
    try:
        logging.debug(f"Freeing IPv6 allocation {alloc_name}")
        address_api = AddressApi()
        pool_name = f'RUCIO_Site_BGP_Subnet_Pool-{sitename}'
        address_api.free_address(pool_name, name=alloc_name)
        logging.debug(f"Allocation {alloc_name} freed for {sitename}")
        return True
    except Exception as e:
        logging.error(f"free_address: {str(e)}")
        raise ValueError(f"Freeing allocation failed for {sitename} and {alloc_name}")

def affiliate_endpoints(sense_uuid, src_site_name, dst_site_name, rule_id,
                        sense_src_uri, sense_dst_uri):
    """
    Affiliate endpoints with a SENSE instance.
    
    Args:
        sense_uuid: SENSE instance UUID
        src_site_name: Source site name
        dst_site_name: Destination site name
        rule_id: Rule ID used as alias for the affiliation in SENSE-O
        sense_src_uri: SENSE source URI
        sense_dst_uri: SENSE destination URI
        
    Raises:
        Exception: If affiliation fails
    """
    address_api = AddressApi()
    
    try:
        src_pool_name = f'RUCIO_Site_BGP_Subnet_Pool-{src_site_name}'
        logging.debug(f"Affiliating allocation {rule_id} with SENSE instance {sense_uuid} in address pool {src_pool_name}")
        address_api.affiliate_address(pool=src_pool_name, uri=sense_src_uri, name=rule_id)
        address_api.expire_address(pool=src_pool_name, expire=-1, name=rule_id)

        dst_pool_name = f'RUCIO_Site_BGP_Subnet_Pool-{dst_site_name}'
        logging.debug(f"Affiliating allocation {rule_id} with SENSE instance {sense_uuid} in address pool {dst_pool_name}")
        address_api.affiliate_address(pool=dst_pool_name, uri=sense_dst_uri, name=rule_id)
        address_api.expire_address(pool=dst_pool_name, expire=-1, name=rule_id)
        
        logging.info(f"Successfully affiliated endpoints for SENSE instance {sense_uuid}")
    except Exception as e:
        logging.error(f"Failed to affiliate endpoints for SENSE instance {sense_uuid}: {e}", exc_info=True)
        raise

def format_ipv6_compressed(ip_range):
    """
    Format an IPv6 range to compressed notation.
    
    Args:
        ip_range: IPv6 range string
        
    Returns:
        Compressed IPv6 network string
    """
    return ipaddress.IPv6Network(ip_range).compressed

def get_site_uris(site_name):
    """
    Get the full URI and root URI for a given site from SENSE.
    
    Args:
        site_name: Name of the site
        
    Returns:
        Tuple of (full_uri, root_uri)
        
    Raises:
        ValueError: If discovery fails
    """
    try:
        discover_api = DiscoverApi()
        response = discover_api.discover_lookup_name_get(site_name, search="metadata", type="/sitename")
        if not _good_response(response) or not response["results"]:
            raise ValueError(f"Discover query failed for {site_name}")
        matched_results = [result for result in response["results"] if site_name in result["name/tag/value"]]
        if not matched_results:
            raise ValueError(f"No results matched for {site_name}")
        full_uri = matched_results[0]["resource"]
        root_uri = discover_api.discover_lookup_rooturi_get(full_uri)
        if not _good_response(root_uri):
            raise ValueError(f"Discover query failed for {full_uri}")
        logging.debug(f"Got URI: {root_uri} for {site_name}")
        return full_uri, root_uri
    except Exception as e:
        logging.error(f"get_site_uris: {str(e)}")
        raise ValueError(f"Getting URI failed for {site_name}")

def get_site_info(root_uri):
    """
    Get site info for a given root URI from SENSE.
    
    Args:
        root_uri: Root URI of the site
        
    Returns:
        Site info dict containing domain_uri, domain_url, peer_points, etc.
        
    Raises:
        ValueError: If query fails
    """
    try:
        discover_api = DiscoverApi()
        site_info = discover_api.discover_domain_id_get(root_uri)
        if not _good_response(site_info):
            raise ValueError(f"Site Info Query Failed for {root_uri}")
        return site_info
    except Exception as e:
        logging.error(f"Error occurred while getting site info for {root_uri}: {str(e)}")
        raise

def get_link_capacity(site_info, vlan_range):
    """
    Get the link capacity for a given site and VLAN range.
    
    Args:
        site_info: Site info dict from get_site_info
        vlan_range: VLAN range string (e.g., "100-200", "100,101", or "any")
        
    Returns:
        Link capacity in Mbps
    """
    vlan_range_start = None
    vlan_range_end = None
    
    if "-" in vlan_range:
        vlan_range_start, vlan_range_end = map(int, vlan_range.split("-"))
        logging.debug(f"Using vlan range {vlan_range_start}-{vlan_range_end} for link capacity")
    elif "," in vlan_range:
        vlan_range_start = min(map(int, vlan_range.split(",")))
        vlan_range_end = max(map(int, vlan_range.split(",")))
        logging.debug(f"Using vlan range {vlan_range_start}-{vlan_range_end} for link capacity")
    
    if vlan_range == "any":
        port_capacity = int(site_info["peer_points"][0]["port_capacity"])
        logging.debug(f"Using port capacity {port_capacity} for vlan range 'any'")
        return port_capacity
    else:
        for peer_point in site_info["peer_points"]:
            if str(vlan_range_start) in peer_point["peer_vlan_pool"] and str(vlan_range_end) in peer_point["peer_vlan_pool"]:
                port_capacity = int(peer_point["port_capacity"])
                logging.debug(f"Using port capacity {port_capacity} for vlan range {vlan_range_start}-{vlan_range_end}")
                return port_capacity
        port_capacity = int(site_info["peer_points"][0]["port_capacity"])
        logging.debug(f"Using default port capacity {port_capacity} for vlan range {vlan_range}")
        return port_capacity

def get_endpoints_for_site(sense_uri, site_name):
    """
    Get the endpoints (IP ranges and hostnames) for a given site from SENSE.
    
    Args:
        sense_uri: SENSE URI of the site
        site_name: Name of the site (used for metadata tag determination)
        
    Returns:
        Dict mapping IP ranges to hostnames
        
    Raises:
        ValueError: If query fails or no endpoints found
    """
    if not sense_uri:
        logging.warning(f"Site {site_name} has no SENSE URI, skipping endpoint discovery")
        return {}
    
    try:
        logging.info(f"Getting list of endpoints for {sense_uri}")
        workflow_api = WorkflowCombinedApi()
        
        # Determine the correct metadata tag based on site name
        # TODO: Make this configurable instead of hardcoded
        metadata_tag = "/xrootd6" if "FNAL" in site_name else "/xrootd"
        
        manifest_json = {
            "Metadata": "?metadata?",
            "sparql-ext": f"SELECT ?metadata WHERE {{ ?site nml:hasService ?md_svc. ?md_svc mrs:hasNetworkAttribute ?dir_xrootd. ?dir_xrootd mrs:type 'metadata:directory'. ?dir_xrootd mrs:tag '{metadata_tag}'. ?dir_xrootd mrs:value ?metadata.  FILTER regex(str(?site), '{sense_uri}') }} LIMIT 1",
            "required": "true"
        }
        
        response = workflow_api.manifest_create(json.dumps(manifest_json))
        if not response or "jsonTemplate" not in response:
            raise ValueError(f"Invalid response from SENSE manifest creation: {response}")
            
        metadata = json.loads(response["jsonTemplate"])
        logging.debug(f"Got metadata response for {sense_uri}")
        
        if "Metadata" not in metadata:
            logging.warning(f"No Metadata field in response for {sense_uri}")
            return {}
            
        endpoint_list = json.loads(metadata["Metadata"].replace("'", "\""))
        if not endpoint_list:
            logging.warning(f"Empty endpoint list for {sense_uri}")
            return {}
        
        return endpoint_list
        
    except Exception as e:
        error_msg = f"Getting list of endpoints failed for {sense_uri}: {e}"
        logging.error(error_msg, exc_info=True)
        raise ValueError(error_msg)

def refresh_all_sites(rucio_client, session):
    """
    Refresh all sites from Rucio and SENSE.
    This is a high-level function that can be called from the frontend API.
    
    Args:
        rucio_client: Rucio client instance
        session: Database session
        
    Returns:
        List of site names that were processed
    """
    
    logging.debug("Getting list of sites registered in Rucio")
    sites = [i['rse'] for i in list_rses(rucio_client)]
    logging.debug(f"Got list of sites: {sites}, adding to database")
    
    site_objs = []
    for site_name in sites:
        try:
            site_ = _get_or_create_site(site_name, site_objs, session, config_get)
            site_objs.append(site_)
            _add_endpoints_for_site(site_, rucio_client, session)
        except Exception as e:
            logging.error(f"Error occurred in refresh_sites for site {site_name}: {str(e)}")
    
    return sites

def _get_or_create_site(site_name, site_objs, session, config_get_func):
    """
    Get existing site or create new one with SENSE URIs.
    
    Args:
        site_name: Name of the site
        site_objs: List of already processed site objects
        session: Database session
        config_get_func: Config get function for VLAN ranges
        
    Returns:
        Site object
    """
    
    site_exists = Site.get_by_name(name=site_name, session=session, use_lock=False)
    if site_exists:
        logging.debug(f"Site {site_name} already exists in database")
        return site_exists
    
    logging.debug(f"Site {site_name} not found in database, adding...")
    try:
        full_uri, root_uri = get_site_uris(site_name)
        site_info = get_site_info(root_uri)
        sense_uri = site_info["domain_uri"]
        query_url = site_info["domain_url"]
        site_ = Site(name=site_name, sense_uri=sense_uri, query_url=query_url)
        site_.save(session=session)

        # Create mesh links between this site and existing sites
        for site_obj in site_objs:
            if site_obj == site_:
                continue
            vlan_range = _get_vlan_range_for_pair(site_obj, site_, config_get_func)
            link_capacity = get_link_capacity(site_info, vlan_range)
            mesh = Mesh(site1=site_obj, site2=site_, vlan_range=vlan_range, link_capacity_mbps=link_capacity)
            mesh.save(session=session)

        logging.debug(f"Site {site_name} added to database")
        return site_
    except Exception as e:
        logging.error(f"Error occurred while adding site {site_name}: {str(e)}")
        raise

def _get_vlan_range_for_pair(site_obj, site_, config_get_func):
    """Get VLAN range for a site pair."""
    vlan_range = config_get_func("vlan-ranges", f"{site_obj.name}-{site_.name}", default="any")
    if vlan_range == "any":
        vlan_range = config_get_func("vlan-ranges", f"{site_.name}-{site_obj.name}", default="any")
    logging.debug(f"Using vlan range {vlan_range} for {site_obj.name} and {site_.name}")
    return vlan_range

def _add_endpoints_for_site(site_, rucio_client, session):
    """
    Add endpoints for a site from SENSE metadata.
    
    Args:
        site_: Site object
        rucio_client: Rucio client instance
        session: Database session
    """
    try:
        if not site_.sense_uri:
            logging.warning(f"Site {site_.name} has no SENSE URI, skipping endpoint creation")
            return
        
        endpoint_list = get_endpoints_for_site(site_.sense_uri, site_.name)
        if not endpoint_list:
            logging.warning(f"No endpoints found for {site_.name}")
            return

        logging.info(f"Getting protocol for the registered endpoints for {site_.name}")
        protocol = get_rse_protocol(rucio_client, site_.name)
        if not protocol:
            raise ValueError(f"No protocol found for RSE {site_.name}")

        endpoints_added = 0
        for iprange, hostname in endpoint_list.items():
            try:
                iprange_compressed = format_ipv6_compressed(iprange)
                
                if Endpoint.get_by_ip_range(ip_range=iprange_compressed, session=session, use_lock=False) is None:
                    new_endpoint = Endpoint(
                        site=site_,
                        protocol=protocol,
                        ip_range=iprange_compressed,
                        hostname=hostname,
                        is_allocated=False
                    )
                    new_endpoint.save(session=session)
                    endpoints_added += 1
                else:
                    logging.debug(f"Endpoint {iprange_compressed} already exists, skipping")
            except Exception as endpoint_err:
                logging.error(f"Failed to add endpoint {iprange} for site {site_.name}: {endpoint_err}")
                continue
                
        logging.info(f"Added {endpoints_added} new endpoints for {site_.name}")
        
    except Exception as e:
        error_msg = f"Getting list of endpoints failed for {site_.sense_uri}: {e}"
        logging.error(error_msg, exc_info=True)
        raise ValueError(error_msg)
