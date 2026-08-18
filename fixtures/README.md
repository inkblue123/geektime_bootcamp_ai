## API Testing with REST Client

This directory contains REST Client test files for testing the Database Query Tool API.

## Prerequisites

1. Install the REST Client extension in VSCode
2. Ensure the backend server is running (`make dev-backend` or `make dev`)

## Usage

1. Open `test.rest` in VSCode
2. Click the "Send Request" link above each HTTP request
3. View the response in the VSCode panel

## Variables

The test file uses variables that you can customize:

- `@baseUrl`: API base URL (default: `http://localhost:8000/api/v1`)
- `@dbName`: Database connection name for testing (default: `testdb`)
- `@testDbUrl`: PostgreSQL connection URL (default: `sqlite:///./test.db`)

## Test Scenarios

The file includes tests for:

1. __Health Check__ - Verify backend is running
2. __Database Management__ - CRUD operations for database connections
3. __Metadata Operations__ - Get and refresh database metadata
4. __SQL Query Execution__ - Various SELECT queries
5. __Natural Language to SQL__ - LLM-powered SQL generation
6. __Query History__ - Retrieve execution history
7. __Error Cases__ - Validation and error handling
8. __Edge Cases__ - Special characters, NULL handling, complex queries

## Running Tests

### Individual Tests

Click "Send Request" above any request to execute it individually.

### Complete Workflow

Follow the numbered requests in order to test a complete workflow:

1. Add database connection
2. Get metadata
3. Execute queries
4. Check history

## Notes

- Make sure your SQLite database is running and accessible
- Update `@testDbUrl` if your database credentials differ
- Some tests require a database with specific tables (users, orders, etc.)
- Error tests are expected to fail - they verify error handling