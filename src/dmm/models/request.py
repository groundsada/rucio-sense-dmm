from sqlmodel import Field, Relationship, select
from typing import Optional, List

import logging

from dmm.models.base import *

class Request(ModelBase, table=True):
    rule_id: str = Field(primary_key=True)
    transfer_status: Optional[str] = Field(default=None, index=True)
    priority: Optional[int] = Field(default=None)
    rule_size: Optional[float] = Field(default=None)
    modified_priority: Optional[int] = Field(default=None)
    available_bandwidth_mbps: Optional[float] = Field(default=None)  # Renamed: clarified units
    allocated_bandwidth_mbps: Optional[float] = Field(default=None)  # Renamed from bandwidth
    previous_bandwidth_mbps: Optional[float] = Field(default=None)  # Renamed: clarified units
    sense_uuid: Optional[str] = Field(default=None)
    sense_src_uri: Optional[str] = Field(default=None)  # Renamed from source_affiliation_uri
    sense_dst_uri: Optional[str] = Field(default=None)  # Renamed from destination_affiliation_uri
    sense_circuit_status: Optional[str] = Field(default=None, index=True)
    sense_affiliated: Optional[bool] = Field(default=False)
    fts_streams_current: Optional[int] = Field(default=0)  # Renamed from fts_streams_current
    fts_streams_desired: Optional[int] = Field(default=None)  # Renamed from fts_streams_desired
    sense_provisioned_at: Optional[datetime] = Field(default=None)
    rucio_finished_at: Optional[datetime] = Field(default=None)
    prometheus_throughput: Optional[float] = Field(default=None)
    prometheus_bytes: Optional[float] = Field(default=None)
    health: Optional[str] = Field(default=None)
    
    src_site_: Optional[str] = Field(default=None, foreign_key='site.name')
    dst_site_: Optional[str] = Field(default=None, foreign_key='site.name')
    src_endpoint_: Optional[int] = Field(default=None, foreign_key='endpoint.id')
    dst_endpoint_: Optional[int] = Field(default=None, foreign_key='endpoint.id')

    src_site: Optional["Site"] = Relationship(
        back_populates='requests_as_source', 
        sa_relationship_kwargs={"foreign_keys": "[Request.src_site_]"}
    )
    dst_site: Optional["Site"] = Relationship(
        back_populates='requests_as_destination', 
        sa_relationship_kwargs={"foreign_keys": "[Request.dst_site_]"}
    )
    src_endpoint: Optional["Endpoint"] = Relationship(
        back_populates='requests_as_source', 
        sa_relationship_kwargs={"foreign_keys": "[Request.src_endpoint_]"}
    )
    dst_endpoint: Optional["Endpoint"] = Relationship(
        back_populates='requests_as_destination', 
        sa_relationship_kwargs={"foreign_keys": "[Request.dst_endpoint_]"}
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def __eq__(self, other):
        return self.rule_id == other.rule_id

    @classmethod
    def get_by_status(cls, statuses: List[str], session=None, use_lock: bool = True):
        logging.debug(f"REQUEST QUERY: statuses={statuses}, locked={use_lock}")
        statement = select(cls).where(cls.transfer_status.in_(statuses))
        if use_lock:
            statement = statement.with_for_update()
        return list(session.exec(statement).all())

    @classmethod
    def get_by_id(cls, rule_id: str, session=None, use_lock: bool = True):
        logging.debug(f"REQUEST QUERY: rule_id={rule_id}, locked={use_lock}")
        statement = select(cls).where(cls.rule_id == rule_id)
        if use_lock:
            statement = statement.with_for_update()
        return session.exec(statement).first()
    
    def set_status(self, status: str, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> status={status}")
        self.transfer_status = status 
        self.save(session)

    def set_available_bandwidth(self, bandwidth_mbps: float, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> available_bandwidth={bandwidth_mbps} Mbps")
        self.available_bandwidth_mbps = bandwidth_mbps
        self.save(session)

    def set_sense_uuid(self, sense_uuid: str, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> sense_uuid={sense_uuid}")
        self.sense_uuid = sense_uuid
        self.save(session)

    def set_sense_uris(self, src_uri: str, dst_uri: str, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> src_uri={src_uri}, dst_uri={dst_uri}")
        self.sense_src_uri = src_uri
        self.sense_dst_uri = dst_uri
        self.save(session)
    
    def set_allocated_bandwidth(self, bandwidth_mbps: float, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> allocated_bandwidth={bandwidth_mbps} Mbps")
        self.allocated_bandwidth_mbps = bandwidth_mbps
        self.save(session)

    def set_previous_bandwidth(self, bandwidth_mbps: float, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> previous_bandwidth={bandwidth_mbps} Mbps")
        self.previous_bandwidth_mbps = bandwidth_mbps
        self.save(session)

    def set_priority(self, priority: int, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> priority={priority}")
        self.priority = priority
        self.modified_priority = priority
        self.save(session)

    def set_sense_circuit_status(self, status: str, session=None):
        logging.debug(f"REQUEST UPDATE: {self.rule_id} -> circuit_status={status}")
        self.sense_circuit_status = status
        self.save(session)
    
    def set_fts_streams(self, current: int = None, desired: int = None, session=None):
        if current is not None:
            logging.debug(f"REQUEST UPDATE: {self.rule_id} -> fts_streams_current={current}")
            self.fts_streams_current = current
        if desired is not None:
            logging.debug(f"REQUEST UPDATE: {self.rule_id} -> fts_streams_desired={desired}")
            self.fts_streams_desired = desired
        self.save(session)

    def set_prometheus_metrics(self, throughput: float = None, bytes_transferred: float = None, session=None):
        if throughput is not None:
            self.prometheus_throughput = throughput
        if bytes_transferred is not None:
            self.prometheus_bytes = bytes_transferred
        self.save(session)

    def set_health(self, health: str, session=None):
        self.health = health
        self.save(session)
