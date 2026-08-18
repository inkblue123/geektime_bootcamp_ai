from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List
import httpx
from datetime import datetime
import time
import csv
import io
import json

from app.database import get_db, engine
from app.models import Base, DatabaseConnection, QueryHistory
from app.config import get_settings
from pydantic import BaseModel

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Database Query Tool API")
settings = get_settings()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic models for API
class DatabaseConnectionCreate(BaseModel):
    name: str
    connection_string: str
    description: str = ""


class DatabaseConnectionResponse(BaseModel):
    id: int
    name: str
    connection_string: str
    description: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class QueryRequest(BaseModel):
    database_id: int
    natural_language_query: str = None
    sql_query: str = None


class QueryResponse(BaseModel):
    id: int
    database_id: int
    natural_language_query: str
    sql_query: str
    execution_time_ms: int
    row_count: int
    status: str
    created_at: datetime
    results: List[dict] = []

    class Config:
        from_attributes = True


class TableInfo(BaseModel):
    name: str
    type: str = "table"
    columns: List[dict]


class ExportRequest(BaseModel):
    query_history_id: int
    format: str = "csv"  # csv or json


class ExecuteAndExportRequest(BaseModel):
    database_id: int
    natural_language_query: str = None
    sql_query: str = None
    export_format: str = "csv"  # csv or json


# Health check endpoint
@app.get("/health")
def health_check():
    return {"status": "healthy", "message": "Database Query Tool API is running"}


# Database connection endpoints
@app.post("/api/v1/database", response_model=DatabaseConnectionResponse)
def create_database_connection(
    connection: DatabaseConnectionCreate, db: Session = Depends(get_db)
):
    """Create a new database connection."""
    try:
        db_connection = DatabaseConnection(
            name=connection.name,
            connection_string=connection.connection_string,
            description=connection.description,
        )
        db.add(db_connection)
        db.commit()
        db.refresh(db_connection)
        return db_connection
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/database", response_model=List[DatabaseConnectionResponse])
def list_database_connections(db: Session = Depends(get_db)):
    """List all database connections."""
    return db.query(DatabaseConnection).all()


@app.get("/api/v1/database/{connection_id}", response_model=DatabaseConnectionResponse)
def get_database_connection(connection_id: int, db: Session = Depends(get_db)):
    """Get a specific database connection."""
    connection = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.id == connection_id)
        .first()
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Database connection not found")
    return connection


@app.delete("/api/v1/database/{connection_id}")
def delete_database_connection(connection_id: int, db: Session = Depends(get_db)):
    """Delete a database connection."""
    connection = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.id == connection_id)
        .first()
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Database connection not found")
    db.delete(connection)
    db.commit()
    return {"message": "Database connection deleted successfully"}


# Query execution endpoints
@app.post("/api/v1/query", response_model=QueryResponse)
def execute_query(
    request: QueryRequest, db: Session = Depends(get_db)
):
    """Execute a SQL query (with optional natural language to SQL conversion)."""
    # Verify database connection exists
    connection = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.id == request.database_id)
        .first()
    )
    if not connection:
        raise HTTPException(
            status_code=404, detail="Database connection not found"
        )

    # Convert natural language to SQL if provided
    sql_query = request.sql_query
    natural_query = request.natural_language_query or ""

    if not sql_query and natural_query:
        sql_query = _convert_natural_language_to_sql(natural_query, connection)

    if not sql_query:
        raise HTTPException(
            status_code=400, detail="Either SQL query or natural language query is required"
        )

    # Execute the query
    start_time = time.time()
    try:
        results = _execute_sql(sql_query, connection.connection_string)
        execution_time = int((time.time() - start_time) * 1000)
        row_count = len(results) if results else 0
        status = "success"
        error_message = None
    except Exception as e:
        execution_time = int((time.time() - start_time) * 1000)
        results = []
        row_count = 0
        status = "error"
        error_message = str(e)

    # Save to query history
    history = QueryHistory(
        database_id=request.database_id,
        natural_language_query=natural_query,
        sql_query=sql_query,
        execution_time_ms=execution_time,
        row_count=row_count,
        status=status,
        error_message=error_message,
    )
    db.add(history)
    db.commit()
    db.refresh(history)

    return QueryResponse(
        id=history.id,
        database_id=history.database_id,
        natural_language_query=history.natural_language_query,
        sql_query=history.sql_query,
        execution_time_ms=history.execution_time_ms,
        row_count=history.row_count,
        status=history.status,
        created_at=history.created_at,
        results=results,
    )


@app.get("/api/v1/query/history", response_model=List[QueryResponse])
def get_query_history(
    database_id: int = None, limit: int = 50, db: Session = Depends(get_db)
):
    """Get query execution history."""
    query = db.query(QueryHistory)
    if database_id:
        query = query.filter(QueryHistory.database_id == database_id)
    query = query.order_by(QueryHistory.created_at.desc()).limit(limit)
    return query.all()


@app.get("/api/v1/database/{connection_id}/metadata", response_model=List[TableInfo])
def get_database_metadata(
    connection_id: int, db: Session = Depends(get_db)
):
    """Get database metadata (tables and columns)."""
    connection = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.id == connection_id)
        .first()
    )
    if not connection:
        raise HTTPException(status_code=404, detail="Database connection not found")

    try:
        metadata = _get_metadata(connection.connection_string)
        return metadata
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get metadata: {str(e)}")


# Export endpoints
@app.post("/api/v1/export")
def export_query_results(request: ExportRequest, db: Session = Depends(get_db)):
    """Export query results to CSV or JSON format."""
    # Get query history
    query_history = (
        db.query(QueryHistory)
        .filter(QueryHistory.id == request.query_history_id)
        .first()
    )

    if not query_history:
        raise HTTPException(status_code=404, detail="Query history not found")

    if query_history.status != "success" or not query_history.results:
        raise HTTPException(
            status_code=400, detail="Cannot export failed or empty query results"
        )

    # Execute the query again to get fresh results
    try:
        connection = (
            db.query(DatabaseConnection)
            .filter(DatabaseConnection.id == query_history.database_id)
            .first()
        )
        if not connection:
            raise HTTPException(status_code=404, detail="Database connection not found")

        results = _execute_sql(query_history.sql_query, connection.connection_string)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute query for export: {str(e)}")

    # Export based on format
    if request.format.lower() == "json":
        return _export_as_json(results, query_history.sql_query)
    elif request.format.lower() == "csv":
        return _export_as_csv(results, query_history.sql_query)
    else:
        raise HTTPException(status_code=400, detail="Unsupported export format. Use 'csv' or 'json'")


@app.post("/api/v1/query/execute-and-export", response_model=QueryResponse)
def execute_and_export_query(
    request: ExecuteAndExportRequest, db: Session = Depends(get_db)
):
    """Execute a query and immediately export the results."""
    # Verify database connection exists
    connection = (
        db.query(DatabaseConnection)
        .filter(DatabaseConnection.id == request.database_id)
        .first()
    )
    if not connection:
        raise HTTPException(
            status_code=404, detail="Database connection not found"
        )

    # Convert natural language to SQL if provided
    sql_query = request.sql_query
    natural_query = request.natural_language_query or ""

    if not sql_query and natural_query:
        sql_query = _convert_natural_language_to_sql(natural_query, connection)

    if not sql_query:
        raise HTTPException(
            status_code=400, detail="Either SQL query or natural language query is required"
        )

    # Execute the query
    start_time = time.time()
    try:
        results = _execute_sql(sql_query, connection.connection_string)
        execution_time = int((time.time() - start_time) * 1000)
        row_count = len(results) if results else 0
        status = "success"
        error_message = None
    except Exception as e:
        execution_time = int((time.time() - start_time) * 1000)
        results = []
        row_count = 0
        status = "error"
        error_message = str(e)

    # Save to query history
    history = QueryHistory(
        database_id=request.database_id,
        natural_language_query=natural_query,
        sql_query=sql_query,
        execution_time_ms=execution_time,
        row_count=row_count,
        status=status,
        error_message=error_message,
    )
    db.add(history)
    db.commit()
    db.refresh(history)

    # If successful, export the results
    if status == "success" and results:
        export_filename = f"query_results_{history.id}_{int(time.time())}"
        if request.export_format.lower() == "json":
            export_response = _export_as_json(results, sql_query, export_filename)
        elif request.export_format.lower() == "csv":
            export_response = _export_as_csv(results, sql_query, export_filename)
        else:
            raise HTTPException(status_code=400, detail="Unsupported export format. Use 'csv' or 'json'")

        # Return query response with export info
        response = QueryResponse(
            id=history.id,
            database_id=history.database_id,
            natural_language_query=history.natural_language_query,
            sql_query=history.sql_query,
            execution_time_ms=history.execution_time_ms,
            row_count=history.row_count,
            status=history.status,
            created_at=history.created_at,
            results=results,
        )

        # Add export information to the response
        response_dict = response.model_dump()
        response_dict["export_info"] = {
            "format": request.export_format,
            "exported_at": datetime.now().isoformat(),
            "message": f"Results exported as {request.export_format.upper()}"
        }

        return response_dict
    else:
        # Return error response
        return QueryResponse(
            id=history.id,
            database_id=history.database_id,
            natural_language_query=history.natural_language_query,
            sql_query=history.sql_query,
            execution_time_ms=history.execution_time_ms,
            row_count=history.row_count,
            status=history.status,
            created_at=history.created_at,
            results=[],
        )


# Helper functions
def _convert_natural_language_to_sql(natural_query: str, connection: DatabaseConnection) -> str:
    """Convert natural language query to SQL using OpenAI."""
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=500, detail="OpenAI API key not configured"
        )

    try:
        metadata = _get_metadata(connection.connection_string)
        schema_info = "\n".join([
            f"Table: {table.name}\nColumns: {', '.join([col['name'] + ':' + col['type'] for col in table.columns])}"
            for table in metadata
        ])

        prompt = f"""You are a SQL expert. Convert the following natural language question to SQL query.

Database Schema:
{schema_info}

Question: {natural_query}

Return only the SQL query, no explanations or additional text."""

        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
        sql_query = result["choices"][0]["message"]["content"].strip()

        # Clean up the response
        if sql_query.startswith("```sql"):
            sql_query = sql_query[6:]
        if sql_query.startswith("```"):
            sql_query = sql_query[3:]
        if sql_query.endswith("```"):
            sql_query = sql_query[:-3]

        return sql_query.strip()
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to convert natural language to SQL: {str(e)}"
        )


def _execute_sql(sql_query: str, connection_string: str) -> List[dict]:
    """Execute SQL query and return results."""
    # This is a simplified implementation - in production, use proper connection pooling
    # and security measures like parameterized queries
    import sqlite3

    # For SQLite connections
    if "sqlite" in connection_string.lower():
        db_path = connection_string.split("///")[-1]
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(sql_query)
        columns = [description[0] for description in cursor.description] if cursor.description else []
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]

        conn.close()
        return results
    else:
        # For other databases, use appropriate drivers
        raise NotImplementedError("Only SQLite is currently supported")


def _get_metadata(connection_string: str) -> List[TableInfo]:
    """Get database metadata."""
    import sqlite3

    if "sqlite" in connection_string.lower():
        db_path = connection_string.split("///")[-1]
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()

        metadata = []
        for table in tables:
            table_name = table[0]
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [
                {
                    "name": col[1],
                    "type": col[2],
                    "not_null": col[3],
                    "primary_key": col[5],
                }
                for col in cursor.fetchall()
            ]
            metadata.append(
                TableInfo(name=table_name, type="table", columns=columns)
            )

        conn.close()
        return metadata
    else:
        raise NotImplementedError("Only SQLite is currently supported")


def _export_as_csv(results: List[dict], sql_query: str, filename: str = None) -> StreamingResponse:
    """Export results as CSV file."""
    if not results:
        raise HTTPException(status_code=400, detail="No results to export")

    # Create CSV content in memory
    output = io.StringIO()
    writer = csv.writer(output)

    # Write header
    headers = list(results[0].keys())
    writer.writerow(headers)

    # Write data rows
    for row in results:
        writer.writerow([str(row.get(header, "")) for header in headers])

    # Reset pointer to beginning
    output.seek(0)

    # Generate filename
    if not filename:
        filename = f"query_results_{int(time.time())}"

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8')),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}.csv"
        }
    )


def _export_as_json(results: List[dict], sql_query: str, filename: str = None) -> StreamingResponse:
    """Export results as JSON file."""
    if not results:
        raise HTTPException(status_code=400, detail="No results to export")

    # Create JSON content
    json_data = {
        "query": sql_query,
        "exported_at": datetime.now().isoformat(),
        "row_count": len(results),
        "data": results
    }

    # Generate filename
    if not filename:
        filename = f"query_results_{int(time.time())}"

    # Convert to string and encode
    json_string = json.dumps(json_data, indent=2, ensure_ascii=False)

    return StreamingResponse(
        io.BytesIO(json_string.encode('utf-8')),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename={filename}.json"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)