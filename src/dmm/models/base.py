from datetime import datetime, timezone

from sqlmodel import SQLModel, Field

class ModelBase(SQLModel):
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        attrs = {k: getattr(self, k) for k in vars(self) if not k.startswith('_')}
        return f"<{self.__class__.__name__}({attrs})>"

    def __setitem__(self, key, value):
        setattr(self, key, value)
        if key != 'updated_at':
            self.updated_at = datetime.now(timezone.utc)

    def __getitem__(self, key):
        return getattr(self, key)
    
    def save(self, session=None):
        if session is None:
            raise ValueError("Session is required to save model instances")
        self.updated_at = datetime.now(timezone.utc)
        session.add(self)

    def delete(self, session=None):
        if session is None:
            raise ValueError("Session is required to delete model instances")
        session.delete(self)

    def update(self, values, session=None):
        if session is None:
            raise ValueError("Session is required to update model instances")
        for k, v in values.items():
            self[k] = v
        session.add(self)

    @classmethod
    def get_all(cls, session=None):
        return [obj for obj in session.query(cls).all()]