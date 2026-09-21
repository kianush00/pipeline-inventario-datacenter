# Development and Testing

This document describes how to contribute to the project, run tests, and the code quality tools used.

---

## Run Tests

The repository includes a comprehensive unit test suite that validates logic, edge cases, and API interactions (using mocks). To run the tests:

```bash
pytest tests/ -v
```

Before committing new code or deploying changes, make sure all tests pass successfully.

---

## Code Quality Tools

### Static Typing (Pyright)

```bash
.venv/bin/pyright <file>
```

Checks type annotations, missing arguments, and unjustified use of `Any`.

### Linting (Ruff)

```bash
# Automatically detect and fix errors
.venv/bin/ruff check <file> --fix

# Format according to PEP 8
.venv/bin/ruff format <file>
```

### Syntax Validation

```bash
.venv/bin/python3 -m py_compile <file>
```

---

## Code Conventions

- **Strong typing:** All Python code uses annotations from the `typing` module. Unjustified `Any` is avoided.
- **Source code in English:** Variables, functions, and classes in English. Comments, logs, and user messages in Spanish.
- **Single Responsibility Principle (SRP):** Functions should do one thing and do it well.
- **Early Returns / Fail-Fast:** Nesting is reduced by returning early in error cases.
- **Idempotency:** Scripts that modify external systems (NetBox) or master files must be strictly idempotent.

For complete engineering guidelines, refer to `GEMINI.md` and `CLAUDE.md` at the root of the project.

---

## Data Quality and Security

The pipeline validates required headers, rejects duplicate columns and malformed fields, detects invalid or duplicate merge keys, and validates the required mapping columns before contacting NetBox. Temporary output files are atomically replaced where supported by the individual script.

Given that the inventory contains infrastructure, network, hardware, and operating system information, access must be restricted to:

- Rundeck jobs
- NetBox credentials
- Source spreadsheets
- Generated files and logs

Avoid committing execution artifacts or sensitive inventory data.
