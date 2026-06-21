---
name: yolo-api-data-layer
description: Guide refactors and feature work for the YOLO service's persistence layer. Use this skill when the user asks to move the API to SQLAlchemy, add prediction-session queries, introduce feedback tables, change schema fields, delete sessions by UID, or make the database backend configurable. Trigger on prompts such as "refactor the api to use sqlalchemy", "add an endpoint GET /predictions/recent", "add a UserFeedback table", "the database layer doesn't follow our architectural design", "delete a prediction session and all its detection objects by uid", "add a column processing_time_ms", or "make the database backend configurable so we can use postgres in production".
---

# YOLO API Data Layer

Use this skill when the request is about how the service stores, retrieves, updates, or deletes prediction data.

## Core goal

Keep the HTTP layer thin and move persistence details into a clear, testable data layer.
The API should handle request validation and serialization, while the database layer should own schema, relationships, queries, and transaction behavior.

## Architectural guardrails

- Prefer SQLAlchemy models and a session dependency over raw `sqlite3` calls.
- Keep endpoint handlers focused on HTTP concerns, not database plumbing.
- Use explicit repository/service helpers for queries such as recent sessions, label lookups, score lookups, and deletions.
- Do not hide the important flow behind a framework wrapper if the lesson is about understanding how data moves through the system.
- Keep the model inference code separate from persistence logic.

## Database design expectations

When changing the data layer, favor this shape:

- `PredictionSession`
  - `uid` (primary key)
  - `timestamp`
  - `original_image`
  - `predicted_image`
  - `processing_time_ms` (if requested)
- `DetectionObject`
  - belongs to one prediction session
  - stores `label`, `score`, and `box`
- `UserFeedback` (if requested)
  - belongs to one prediction session
  - stores rating and optional comment

Use relationships and foreign keys so the schema communicates intent clearly.
If a session is deleted, related detection rows should also be removed (via cascade rules or explicit delete logic).

## SQLAlchemy guidance

- Use a declarative base and a session factory.
- Build the schema once with `Base.metadata.create_all(...)` for development, or use migrations if the project already expects them.
- Use `session.execute(...)` or ORM query patterns consistently instead of mixing styles.
- Use transactions explicitly:
  - `session.add(...)`
  - `session.commit()`
  - `session.rollback()` on error
- Avoid leaking open sessions between requests.

## Endpoint-specific expectations

- `GET /predictions/recent`
  - return the 10 most recent prediction sessions
  - sort by timestamp descending
  - keep the response shape consistent with the rest of the API

- `DELETE /prediction/{uid}` (or equivalent delete route)
  - remove the session and all related detection objects
  - return a clear success or not-found response

- `processing_time_ms`
  - add the column to the session model if the prompt asks for it
  - include it in JSON responses where relevant

- `UserFeedback`
  - add a feedback table for prediction ratings
  - make the relation explicit and easy to query

## Configurable backend guidance

If the request is to support multiple databases:

- use a single configuration source such as `DATABASE_URL`
- allow the app to switch between SQLite (dev) and PostgreSQL (production)
- keep the default local setup simple and reproducible
- document the expected env vars clearly

For example:

- local/dev: SQLite path or URL
- production: Postgres connection string

## Testing expectations

When adding or changing the data layer:

- write tests that exercise the real API contract
- use a temporary database for each test run
- avoid testing only mock behavior
- verify both the HTTP status code and the response body
- if the route reads/writes DB state, assert the stored data as well as the response

## Workflow

1. Identify the exact schema change or endpoint behavior requested.
2. Separate the work into small steps: models, session setup, repository helpers, endpoint updates, and tests.
3. Update the database layer before or alongside the endpoint logic so the API contract is grounded in the real schema.
4. Verify behavior with focused tests or manual requests.
5. If the request mentions production database switching, confirm the config is env-driven and not hardcoded.

## Tooling note

If you run Python locally for verification, use the workspace interpreter under `/home/shahd/PolyAiFursa/.venv` rather than a random system Python.

## Checklist before finishing

- [ ] The schema and relationships match the intended architecture.
- [ ] Endpoint logic is not mixing raw persistence details with response formatting.
- [ ] New queries are explicit and easy to reason about.
- [ ] Deletion and cascading behavior are correct.
- [ ] Configurable database settings are documented and env-driven.
- [ ] Tests cover happy paths and obvious failure cases.
