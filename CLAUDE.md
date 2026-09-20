# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Vageesh is an Agentic AI Orchestration Platform. This repo is the FastAPI backend (`vageesh-back-end`).

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt   # or: pip install -e ".[dev]"

# Run dev server
uvicorn app.main:app --reload

# Run tests
pytest

# Run a single test file
pytest tests/path/to/test_file.py

# Run a single test
pytest tests/path/to/test_file.py::test_function_name
```

> Note: No `requirements.txt` or `pyproject.toml` exists yet — create one as dependencies are added.

## Architecture

The entry point is `app/main.py`, which creates the FastAPI `app` instance. As the project grows, follow this structure:

```
app/
  main.py          # FastAPI app instantiation, router registration
  routers/         # Route handlers grouped by domain
  services/        # Business logic layer
  models/          # Pydantic request/response schemas
  db/              # Database session, models, migrations
  core/            # Config, settings, auth, dependencies
tests/
  unit/
  integration/
```

## Stack

- **Framework**: FastAPI (async)
- **Python**: 3.11+
- **Testing**: pytest + pytest-asyncio for async route testing
