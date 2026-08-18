# Database Query Tool

A web-based tool for managing database connections and executing SQL queries with natural language support using OpenAI's GPT.

## 🚀 Features

- **Database Connection Management**: Create, list, and manage multiple database connections
- **SQL Query Execution**: Run SQL queries against connected databases
- **Natural Language to SQL**: Convert plain English questions into SQL queries using AI
- **Query History**: Track and review past query executions
- **Database Metadata**: Explore database structure with table and column information
- **Real-time Results**: View query results with execution time and row count
- **RESTful API**: Complete API for integration and testing

## 📋 Prerequisites

- Python 3.12+
- Node.js 18+
- OpenAI API Key (for natural language to SQL conversion)
- SQLite (included) or PostgreSQL for database connections

## 🏗️ Project Structure

```
db_query/
├── backend/                 # FastAPI backend (Python)
│   ├── app/
│   │   ├── main.py         # FastAPI application and routes
│   │   ├── models.py       # Database models
│   │   ├── database.py     # Database connection configuration
│   │   └── config.py       # Application configuration
│   ├── tests/              # Backend tests
│   ├── requirements.txt    # Python dependencies
│   ├── .env.example        # Environment variables template
│   └── .gitignore
├── frontend/               # React frontend (TypeScript + Vite)
│   ├── src/
│   │   ├── App.tsx         # Main application component
│   │   ├── DatabasePanel.tsx  # Database management UI
│   │   ├── QueryPanel.tsx  # Query execution UI
│   │   ├── api.ts          # API client functions
│   │   └── main.tsx        # React entry point
│   ├── package.json        # Node dependencies
│   ├── vite.config.ts      # Vite configuration
│   └── tailwind.config.js  # Tailwind CSS configuration
├── fixtures/               # API test files
│   ├── test.rest          # REST Client test file
│   └── README.md          # Testing guide
└── Makefile               # Development commands
```

## 🛠️ Installation & Setup

### 1. Install Dependencies

```bash
# Install all dependencies (backend + frontend)
make install

# Or install separately:
make install-backend    # Install Python dependencies
make install-frontend   # Install Node dependencies
```

### 2. Configure Environment

```bash
# Copy the example environment file
cp backend/.env.example backend/.env

# Edit backend/.env and add your OpenAI API Key
# Required: OPENAI_API_KEY=your_openai_api_key_here
```

### 3. Run Database Migrations

```bash
# The application uses SQLite by default, which doesn't require migration
# For PostgreSQL, you would run: make db-upgrade
```

## 🚀 Running the Application

### Start Both Servers

```bash
make dev
```

This starts:
- **Backend**: http://localhost:8000
- **Frontend**: http://localhost:5173

### Start Individual Servers

```bash
# Backend only
make dev-backend

# Frontend only
make dev-frontend
```

## 📖 Usage

### 1. Add Database Connection

1. Navigate to "Database Connections" tab
2. Click "Add Connection"
3. Fill in:
   - **Name**: A friendly name for your connection
   - **Connection String**: Database URL (e.g., `sqlite:///path/to/database.db`)
   - **Description**: Optional description

### 2. Execute SQL Queries

1. Navigate to "Query Execution" tab
2. Select a database connection
3. Enter either:
   - **Natural Language Query**: Ask questions in plain English
   - **SQL Query**: Write SQL directly
4. Click "Execute Query"
5. View results in the table below

### 3. View Query History

- Scroll down on the Query Execution tab to see past queries
- Shows execution time, row count, and status

## 🔧 Development Commands

```bash
# View all available commands
make help

# Code quality
make lint              # Run all linters
make format            # Format all code

# Testing
make test              # Run all tests
make test-backend      # Run backend tests
make test-frontend     # Run frontend tests

# Cleanup
make clean             # Clean all build artifacts
```

## 🧪 API Testing

### Using REST Client (VSCode)

1. Install the REST Client extension in VSCode
2. Open `fixtures/test.rest`
3. Click "Send Request" above any HTTP request
4. View responses in the VSCode panel

See [fixtures/README.md](fixtures/README.md) for detailed testing guide.

### API Endpoints

- `GET /health` - Health check
- `POST /api/v1/database` - Create database connection
- `GET /api/v1/database` - List all connections
- `GET /api/v1/database/{id}` - Get specific connection
- `DELETE /api/v1/database/{id}` - Delete connection
- `GET /api/v1/database/{id}/metadata` - Get database metadata
- `POST /api/v1/query` - Execute query
- `GET /api/v1/query/history` - Get query history

## 🔐 Configuration

### Backend Environment Variables

Located in `backend/.env`:

```env
# Database
DATABASE_URL=sqlite:///./db_query.db

# OpenAI
OPENAI_API_KEY=your_openai_api_key_here

# API
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=True

# CORS
CORS_ORIGINS=["http://localhost:5173", "http://localhost:3000"]
```

## 🎨 Tech Stack

### Backend
- **FastAPI** - Modern, fast web framework
- **SQLAlchemy** - SQL toolkit and ORM
- **SQLite** - Default database (PostgreSQL support available)
- **OpenAI API** - Natural language to SQL conversion
- **Pydantic** - Data validation and settings

### Frontend
- **React 18** - UI library
- **TypeScript** - Type safety
- **Vite** - Build tool
- **Tailwind CSS** - Styling
- **Axios** - HTTP client
- **Lucide React** - Icons

## 📝 Project Status

✅ **Phase 1 Complete**: Project structure, configuration, and infrastructure

- Backend project structure initialized
- Frontend project structure initialized  
- Core infrastructure (FastAPI, database, models) ready
- Data models defined with API convention
- Makefile with common development tasks
- REST Client test file for API testing

## 🚀 Next Steps

1. Add your OpenAI API key to `backend/.env`
2. Run `make dev` to start the application
3. Open http://localhost:5173 in your browser
4. Add your first database connection
5. Start querying with natural language!

## 📄 License

This project is part of the Geektime AI Bootcamp.

## 🤝 Support

For issues and questions, please refer to the implementation guide or contact the course instructor.