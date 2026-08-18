.PHONY: help install dev backend frontend test lint format clean

# Variables
BACKEND_DIR = backend
FRONTEND_DIR = frontend
PYTHON = python3.12
NPM = npm

# Colors for output
BLUE = \033[0;34m
GREEN = \033[0;32m
YELLOW = \033[0;33m
NC = \033[0m # No Color

help: ## Show this help message
	@echo "$(BLUE)Database Query Tool - Makefile Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf " $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

# Installation
install: install-backend install-frontend ## Install all dependencies

install-backend: ## Install backend dependencies
	@echo "$(BLUE)Installing backend dependencies...$(NC)"
	cd $(BACKEND_DIR) && pip install -r requirements.txt

install-frontend: ## Install frontend dependencies
	@echo "$(BLUE)Installing frontend dependencies...$(NC)"
	cd $(FRONTEND_DIR) && $(NPM) install

# Development servers
dev: dev-backend dev-frontend ## Start both backend and frontend

dev-backend: ## Start backend development server
	@echo "$(BLUE)Starting backend server on http://localhost:8000$(NC)"
	cd $(BACKEND_DIR) && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend: ## Start frontend development server
	@echo "$(BLUE)Starting frontend server on http://localhost:5173$(NC)"
	cd $(FRONTEND_DIR) && $(NPM) run dev

# Backend commands
backend: dev-backend ## Alias for dev-backend

# Frontend commands
frontend: dev-frontend ## Alias for dev-frontend

frontend-build: ## Build frontend for production
	@echo "$(BLUE)Building frontend...$(NC)"
	cd $(FRONTEND_DIR) && $(NPM) run build

# Testing
test: test-backend test-frontend ## Run all tests

test-backend: ## Run backend tests
	@echo "$(BLUE)Running backend tests...$(NC)"
	cd $(BACKEND_DIR) && pytest -v

test-frontend: ## Run frontend tests
	@echo "$(BLUE)Running frontend tests...$(NC)"
	cd $(FRONTEND_DIR) && $(NPM) test

# Code quality
lint: lint-backend lint-frontend ## Run all linters

lint-backend: ## Lint backend code
	@echo "$(BLUE)Linting backend...$(NC)"
	cd $(BACKEND_DIR) && ruff check app tests

lint-frontend: ## Lint frontend code
	@echo "$(BLUE)Linting frontend...$(NC)"
	cd $(FRONTEND_DIR) && $(NPM) run lint

format: format-backend format-frontend ## Format all code

format-backend: ## Format backend code
	@echo "$(BLUE)Formatting backend...$(NC)"
	cd $(BACKEND_DIR) && ruff format app tests

format-frontend: ## Format frontend code
	@echo "$(BLUE)Formatting frontend...$(NC)"
	cd $(FRONTEND_DIR) && $(NPM) run lint -- --fix || true

# Cleanup
clean: clean-backend clean-frontend ## Clean all build artifacts

clean-backend: ## Clean backend artifacts
	@echo "$(BLUE)Cleaning backend...$(NC)"
	cd $(BACKEND_DIR) && \
	find . -type d -name "__pycache__" -exec rm -r {} + 2>/dev/null || true && \
	find . -type f -name "*.pyc" -delete && \
	rm -rf .pytest_cache htmlcov .coverage dist build

clean-frontend: ## Clean frontend artifacts
	@echo "$(BLUE)Cleaning frontend...$(NC)"
	cd $(FRONTEND_DIR) && \
	rm -rf node_modules dist .vite

# Setup
setup: install ## Initial setup: install dependencies
	@echo "$(GREEN)Setup complete!$(NC)"
	@echo ""
	@echo "Next steps:"
	@echo " 1. Create backend/.env file with your OPENAI_API_KEY"
	@echo " 2. Run 'make dev' to start both servers"
	@echo " 3. Open http://localhost:5173 in your browser"

# Health checks
health: ## Check if backend is running
	@curl -s http://localhost:8000/health | python3 -m json.tool || echo "$(YELLOW)Backend is not running$(NC)"

# API documentation
docs: ## Open API documentation in browser
	@echo "$(BLUE)Opening API docs...$(NC)"
	@echo "Please open http://localhost:8000/docs manually"

# Development workflow shortcuts
check: lint test ## Run lint and tests