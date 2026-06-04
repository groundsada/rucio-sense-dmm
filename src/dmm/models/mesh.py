from sqlmodel import Field, Relationship, or_, select
from typing import Optional
import logging

from dmm.models.base import *

class Mesh(ModelBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    site_1: Optional[str] = Field(default=None, foreign_key='site.name')
    site_2: Optional[str] = Field(default=None, foreign_key='site.name')
    vlan_range: Optional[str] = Field(default=None)
    link_capacity_mbps: Optional[int] = Field(default=None)

    site1: Optional["Site"] = Relationship(sa_relationship_kwargs={"foreign_keys": "[Mesh.site_1]"})
    site2: Optional["Site"] = Relationship(sa_relationship_kwargs={"foreign_keys": "[Mesh.site_2]"})

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @classmethod
    def get_vlan_range(cls, site_1, site_2, session=None, use_lock: bool = True):
        site_1_name = site_1.name if hasattr(site_1, 'name') else site_1
        site_2_name = site_2.name if hasattr(site_2, 'name') else site_2

        logging.debug(f"MESH QUERY: vlan_range between {site_1_name} and {site_2_name}, locked={use_lock}")
        statement = (
            select(cls)
            .where(or_(cls.site_1 == site_1_name, cls.site_1 == site_2_name))
            .where(or_(cls.site_2 == site_1_name, cls.site_2 == site_2_name))
        )
        if use_lock:
            statement = statement.with_for_update()
        mesh = session.exec(statement).first()
        return mesh.vlan_range if mesh else None

    @classmethod
    def get_link_capacity(cls, site, session=None, use_lock: bool = True):
        site_name = site.name if hasattr(site, 'name') else site

        logging.debug(f"MESH QUERY: link_capacity for {site_name}, locked={use_lock}")
        statement = select(cls).where(or_(cls.site_1 == site_name, cls.site_2 == site_name))
        if use_lock:
            statement = statement.with_for_update()
        meshes = list(session.exec(statement).all())
        if not meshes:
            return None
        # Use the minimum (bottleneck) capacity across all links incident to this site.
        # Returning an arbitrary first entry would give wrong constraints when a site
        # has connections to multiple peers at different capacities.
        capacities = [m.link_capacity_mbps for m in meshes if m.link_capacity_mbps is not None]
        return min(capacities) if capacities else None
