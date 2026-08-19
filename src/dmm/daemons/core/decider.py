import logging
import numpy as np
from scipy.optimize import linprog
import networkx as nx
from math import floor

from dmm.daemons.base import DaemonBase

from dmm.models.request import Request, RequestStatus
from dmm.models.mesh import Mesh
from dmm.db.session import databased
from dmm.core.sense import is_circuit_active, is_being_provisioned
from dmm.core.metrics import (
    DECIDER_INFEASIBLE,
    DECIDER_ROUNDING_LOSS,
    DECIDER_SOLVES_PER_CYCLE,
    DECIDER_SOLVE_DURATION,
    LINK_ALLOCATED,
    LINK_CAPACITY,
    MODIFY_CAPPED,
    REQUESTED_BANDWIDTH,
)

def _max_capacity(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)

def _edge_upper_bound(data):
    bound = data.get('available_bandwidth', 1000)
    capacity = data.get('link_capacity_mbps')
    if capacity is not None:
        bound = min(bound, capacity)
    return bound

class DeciderDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        
    def process(self, **kwargs):
        self.run_once(**kwargs)
    
    @databased
    def run_once(self, session=None):
        # The graph is rebuilt from scratch every cycle, so drop the label sets
        # from the last one rather than leaving departed links at a stale value.
        LINK_CAPACITY.clear()
        LINK_ALLOCATED.clear()
        REQUESTED_BANDWIDTH.clear()

        multi_graph = self._build_multi_graph(session)

        if not multi_graph.nodes:
            DECIDER_ROUNDING_LOSS.set(0)
            return

        simple_graph, nodes, edges = self._simplify_graph(multi_graph)
        A, c, b, edge_index = self._prepare_optimization_matrices(simple_graph, nodes, edges)
        optim_result = self._optimize_bandwidth(A, b, c, edges)

        self._allocate_bandwidth(multi_graph, simple_graph, edges, edge_index, optim_result)
        self._publish_graph(multi_graph, simple_graph)

        self._allocate_new_bandwidth(multi_graph, session)
        self._modify_existing_bandwidth(multi_graph, session)

    def _publish_graph(self, multi_graph, simple_graph) -> None:
        """
        Export the graph the decision was made on, after allocation so the
        allocated figure is the rounded one that actually reaches SENSE.
        """
        allocated = {}
        for u, v, data in multi_graph.edges(data=True):
            link = tuple(sorted((u, v)))
            allocated[link] = allocated.get(link, 0) + (data.get('bandwidth') or 0)

        for u, v, data in simple_graph.edges(data=True):
            link = tuple(sorted((u, v)))
            capacity = data.get('link_capacity_mbps')
            if capacity is not None:
                LINK_CAPACITY.labels(*link).set(capacity)
            LINK_ALLOCATED.labels(*link).set(allocated.get(link, 0))

    def _build_multi_graph(self, session) -> nx.MultiGraph:
        """
        Build a network graph from the requests in the database.
        The max available bandwidth is gotten from the Mesh table.
        """
        multi_graph = nx.MultiGraph()
        reqs = Request.get_by_status(statuses=[RequestStatus.MODIFIED, RequestStatus.DECIDED, RequestStatus.STALE, RequestStatus.STAGED, RequestStatus.PROVISIONED, RequestStatus.FINISHED], session=session) # get all requests which would affect the decision (i.e. don't consider requests that are in CANCELLED or FAILED state)
        if reqs == []:
            return multi_graph
        for req in reqs:
            link_capacity_mbps = Mesh.get_link_capacity(req.src_site, req.dst_site, session=session)
            multi_graph.add_node(req.src_site.name)
            multi_graph.add_node(req.dst_site.name)
            # Capacity belongs to the site pair, so it goes on the edge. Writing it
            # to the nodes gave every node whichever pair was processed last.
            multi_graph.add_edge(
                req.src_site.name, req.dst_site.name,
                rule_id=req.rule_id,
                priority=req.priority or 0,  # guard against None priority
                bandwidth=req.allocated_bandwidth_mbps,
                available_bandwidth=req.available_bandwidth_mbps,
                link_capacity_mbps=link_capacity_mbps,
            )
        return multi_graph

    def _simplify_graph(self, multi_graph) -> tuple:
        """
        Simplify the network graph by merging edges with the same source and destination nodes.
        """
        simple_graph = nx.Graph()
        simple_graph.add_nodes_from(multi_graph.nodes(data=True))

        for u, v, data in multi_graph.edges(data=True):
            priority = data['priority']
            available_bandwidth = data.get('available_bandwidth', 1000)
            link_capacity_mbps = data.get('link_capacity_mbps')
            if simple_graph.has_edge(u, v):
                simple_graph[u][v]['priority'] += priority
                # The physical link capacity is fixed — take the max (not sum) so we
                # don't artificially inflate the upper-bound constraint in the LP.
                simple_graph[u][v]['available_bandwidth'] = max(
                    simple_graph[u][v]['available_bandwidth'], available_bandwidth
                )
                simple_graph[u][v]['link_capacity_mbps'] = _max_capacity(
                    simple_graph[u][v]['link_capacity_mbps'], link_capacity_mbps
                )
            else:
                simple_graph.add_edge(u, v, priority=priority, available_bandwidth=available_bandwidth,
                                      link_capacity_mbps=link_capacity_mbps)

        # A node terminates every link incident to it, so the most it can carry is
        # the largest of those. Derived here rather than written per pair.
        for node in simple_graph.nodes:
            caps = [c for c in (simple_graph[node][nbr].get('link_capacity_mbps')
                                for nbr in simple_graph[node]) if c is not None]
            simple_graph.nodes[node]['link_capacity_mbps'] = max(caps) if caps else None

        return simple_graph, list(simple_graph.nodes), list(simple_graph.edges(data=True))

    def _prepare_optimization_matrices(self, simple_graph, nodes, edges) -> tuple:
        """
        Prepare the matrices for the linear programming optimization.
        """
        n_edges = len(edges)
        edge_index = {edge[:2]: i for i, edge in enumerate(edges)}

        c = np.zeros(n_edges)
        for i, (u, v, data) in enumerate(edges):
            priority = data['priority']
            c[i] = -priority
        
        incidence = nx.incidence_matrix(simple_graph, nodelist=nodes, edgelist=edges).toarray()
        node_capacities = [simple_graph.nodes[node].get('link_capacity_mbps') for node in nodes]
        # A site with no known capacity gets no row at all, rather than a guess.
        constrained = [i for i, capacity in enumerate(node_capacities) if capacity is not None]
        if len(constrained) < len(nodes):
            unknown = [nodes[i] for i in range(len(nodes)) if i not in constrained]
            logging.warning(f"No link capacity known for {unknown}, leaving them unconstrained in the LP")

        A_nodes = incidence[constrained]
        b_nodes = np.array([node_capacities[i] for i in constrained], dtype=float)

        edge_bounds = np.array([_edge_upper_bound(data) for _, _, data in edges], dtype=float)

        A = np.vstack([A_nodes, np.eye(n_edges)])
        b = np.concatenate([b_nodes, edge_bounds])

        return A, c, b, edge_index

    def _optimize_bandwidth(self, A, b, c, edges) -> object:
        """
        Optimize the bandwidth allocation using linear programming.
        Binary-searches over the minimum per-edge lower bound to find the highest
        feasible floor — O(log(capacity/precision)) LP solves instead of O(capacity/precision).
        """
        n_edges = len(edges)
        precision_mbps = 5
        solves = 0

        def solve(lower_bound):
            nonlocal solves
            solves += 1
            with DECIDER_SOLVE_DURATION.time():
                return linprog(c, A_ub=A, b_ub=b, bounds=[(lower_bound, None)] * n_edges,
                               method='highs')

        try:
            # Verify a solution exists with lower_bound=0 before searching.
            base_result = solve(0)
            if not base_result.success:
                DECIDER_INFEASIBLE.inc()
                raise ValueError("No feasible solution found for the optimization problem.")

            optim_result = base_result

            # The highest any single edge can be floored is the minimum capacity constraint.
            lo = 0
            hi = int(np.min(b))

            while hi - lo > precision_mbps:
                mid = (lo + hi) / 2
                result = solve(mid)
                if result.success:
                    lo = mid
                    optim_result = result
                else:
                    hi = mid

            return optim_result.x
        finally:
            DECIDER_SOLVES_PER_CYCLE.observe(solves)

    def _allocate_bandwidth(self, multi_graph, simple_graph, edges, edge_index, bandwidths) -> None:
        """
        Set the bandwidths in the graph based on the optimization result.
        @param multi_graph: the network multi_graph
        @param simple_graph: the simplified graph
        @param edges: the edges of the graph
        @param edge_index: the edge index mapping
        @param x: the optimization result
        """
        rounding_loss = 0.0
        for u, v, key, data in multi_graph.edges(keys=True, data=True):
            total_priority = simple_graph[u][v]['priority']
            if total_priority > 0:
                proportion = data['priority'] / total_priority
                bandwidth = bandwidths[edge_index[(u, v)]] * proportion
                # Round to lowest 1000 Mbps, but ensure minimum of 1000 if bandwidth > 0
                rounded_bandwidth = floor(bandwidth // 1000) * 1000
                if bandwidth > 0 and rounded_bandwidth == 0:
                    rounded_bandwidth = 1000  # Minimum bandwidth
                rounding_loss += max(0.0, bandwidth - rounded_bandwidth)
                if data.get('rule_id'):
                    # Sorted to match the link-level series, so the two can be joined.
                    REQUESTED_BANDWIDTH.labels(data['rule_id'], *sorted((u, v))).set(bandwidth)
                multi_graph[u][v][key]['bandwidth'] = rounded_bandwidth
            else:
                logging.warning(f"Total priority is 0 for edge {u}->{v}, setting bandwidth to 0")
                multi_graph[u][v][key]['bandwidth'] = 0
        DECIDER_ROUNDING_LOSS.set(rounding_loss)

    def _allocate_new_bandwidth(self, multi_graph, session) -> None:
        """
        Allocate bandwidth for new requests and mark them as decided
        """
        reqs_allocated = Request.get_by_status(statuses=[RequestStatus.STAGED], session=session)
        for req in reqs_allocated:
            allocated_bandwidth = None  # Initialize to prevent NameError
            for _, _, key, data in multi_graph.edges(keys=True, data=True):
                if "rule_id" in data and data["rule_id"] == req.rule_id:
                    allocated_bandwidth = int(data["bandwidth"])
                    break  # Found the matching edge, no need to continue
            
            if allocated_bandwidth is None:
                logging.error(f"Could not find bandwidth allocation for request {req.rule_id} in multi_graph")
                continue  # Skip this request, don't update it
                
            req.set_allocated_bandwidth(allocated_bandwidth, session=session)
            logging.info(f"Allocated bandwidth for request {req.rule_id}: {allocated_bandwidth}")
            req.set_status(status=RequestStatus.DECIDED, session=session)

    def _modify_existing_bandwidth(self, multi_graph, session) -> None:
        """
        Modify the bandwidth for existing requests and mark them as stale.

        """
        reqs_provisioned = Request.get_by_status(statuses=[RequestStatus.MODIFIED, RequestStatus.PROVISIONED, RequestStatus.DECIDED], session=session)
        for req in reqs_provisioned:
            allocated_bandwidth = None  # Initialize to prevent NameError
            link_capacity = None  # capacity of this request's own link (needed for cap logic)
            link = None
            for u, v, key, data in multi_graph.edges(keys=True, data=True):
                if "rule_id" in data and data["rule_id"] == req.rule_id:
                    allocated_bandwidth = int(data["bandwidth"])
                    link_capacity = data.get("link_capacity_mbps")
                    link = tuple(sorted((u, v)))
                    break  # Found the matching edge, no need to continue

            if allocated_bandwidth is None:
                logging.error(f"Could not find bandwidth allocation for request {req.rule_id} in multi_graph")
                continue  # Skip this request, don't update it

            if link_capacity is not None and allocated_bandwidth > (req.allocated_bandwidth_mbps or 0):
                cotenant_statuses = [
                    RequestStatus.PROVISIONED, RequestStatus.STALE, RequestStatus.MODIFIED,
                    RequestStatus.DECIDED, RequestStatus.FINISHED, RequestStatus.FINISHED_R,
                ]
                cotenant_reqs = Request.get_by_status(
                    statuses=cotenant_statuses, session=session, use_lock=False
                )
                reserved_by_others = sum(
                    (r.allocated_bandwidth_mbps or 0)
                    for r in cotenant_reqs
                    if r.rule_id != req.rule_id
                    and (
                        (r.src_site_ == req.src_site_ and r.dst_site_ == req.dst_site_)
                        or (r.src_site_ == req.dst_site_ and r.dst_site_ == req.src_site_)
                    )
                    and r.sense_uuid is not None
                )
                capped = int(min(allocated_bandwidth, link_capacity - reserved_by_others))
                if capped != allocated_bandwidth:
                    MODIFY_CAPPED.labels(*link).inc()
                    logging.info(
                        f"Capping upward MODIFY for {req.rule_id}: "
                        f"LP={allocated_bandwidth} → capped={capped} Mbps "
                        f"({reserved_by_others} Mbps reserved by co-tenant circuits with active SENSE UUIDs)"
                    )
                allocated_bandwidth = capped

            if allocated_bandwidth == req.allocated_bandwidth_mbps:
                continue

            circuit_committed = (
                is_circuit_active(req.sense_circuit_status)
                or is_being_provisioned(req.sense_circuit_status)
            )
            if req.transfer_status == RequestStatus.DECIDED and not circuit_committed:
                req.set_allocated_bandwidth(allocated_bandwidth, session=session)
                logging.info(f"Updated decided bandwidth for not-yet-provisioned request {req.rule_id}: {allocated_bandwidth}")
                continue

            req.set_previous_bandwidth(req.allocated_bandwidth_mbps, session=session)
            req.set_allocated_bandwidth(allocated_bandwidth, session=session)
            logging.info(f"Modified bandwidth for request {req.rule_id}: {allocated_bandwidth}")
            req.set_status(status=RequestStatus.STALE, session=session)

    @staticmethod
    def _good_response(response):
        return bool(response and not any("ERROR" in r for r in response))