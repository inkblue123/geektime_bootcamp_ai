from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class DatabaseConnection(Base):
    __tablename__ = "database_connections"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    connection_string = Column(Text, nullable=False)
    description = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QueryHistory(Base):
    __tablename__ = "query_history"

    id = Column(Integer, primary_key=True, index=True)
    database_id = Column(Integer, nullable=False)
    natural_language_query = Column(Text)
    sql_query = Column(Text, nullable=False)
    execution_time_ms = Column(Integer)
    row_count = Column(Integer)
    status = Column(String(20), nullable=False)  # success, error
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)