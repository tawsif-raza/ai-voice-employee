# Database Architecture and Operations

_Status: Production-Ready (Phases 12 & 13)_

---

## 1. Overview

The AI Voice Agent persistence layer provides durable, ACID-compliant relational storage for conversational sessions, long-term user memories, security audit events, and tool execution idempotency records.

### Core Technologies
* **ORM & Query Engine**: [SQLAlchemy](https://www.sqlalchemy.org/) (Core and ORM declarative models)
* **Migrations**: [Alembic](https://alembic.sqlalchemy.org/) for schema versioning and deterministic upgrades/downgrades
* **Production Engine**: PostgreSQL 15+ (network-attached, pooled connections)
* **Development & Test Engine**: SQLite (in-memory or file-backed for fast, offline testing)

### Persistence Modes
Persistence mode is governed by the `PERSISTENCE_MODE` environment variable:

* **Dev Mode (`PERSISTENCE_MODE=dev` or unset)**:
  * Default mode.
  * In-memory repositories are utilized by default (`SessionRepository`, `MemoryRepository`, `InMemoryIdempotencyRepository`).
  * Enables fully offline development and unit tests without external database dependencies.
  * `GET /ready` checks model loading only.

* **Production Mode (`PERSISTENCE_MODE=production` or `postgres`/`postgresql`)**:
  * Requires a valid, reachable `DATABASE_URL`.
  * If `DATABASE_URL` is missing or invalid, startup fails immediately (`DatabaseConfigurationError`).
  * If PostgreSQL is unreachable at startup or runtime, the application fails closed (`DatabaseUnavailableError`).
  * `GET /ready` actively checks database liveness via `Database.health_check()`. If the database is unreachable or degraded, `/ready` responds with HTTP 503 `{"ready": false}`.
  * Silent fallback from production to in-memory mode is strictly forbidden.

### Security and Credential Hygiene
* Connection strings embedding credentials are masked via `DatabaseConfig.safe_url` (e.g., `postgresql://voice_app:***@localhost:5432/ai_voice_agent`).
* Raw passwords and connection strings are never included in exception messages, server logs, or API responses.

---

## 2. Current State

The persistence layer separates business logic from storage through clean repository interfaces:

```text
       LLM / Audio / API Layer
                  ↓
         ConversationManager
                  ↓
   Services (Session, Memory, Audit, Tools)
                  ↓
         Repository Interfaces
                  ↓
   PostgreSQL / SQLite Implementation
       (via SQLAlchemy Core / ORM)
```

Repositories implemented:
1. **`PostgresSessionRepository`** (`src/agent/session_repository_postgres.py`):
   Manages session state, conversation context, workflow status, and atomic confirmation consumption.
2. **`PostgresMemoryRepository`** (`src/agent/memory_repository_postgres.py`):
   Persists scoped durable user memory records filtered by `PolicyEngine` and `PrivacyService`.
3. **`PostgresAuditRepository`** (`src/agent/audit_repository_postgres.py`):
   Append-only storage for structured audit events and security incident logs.
4. **`PostgresIdempotencyRepository`** (`src/agent/idempotency_repository_postgres.py`):
   Composite-key idempotency reservation and execution status tracking to prevent duplicate tool execution.

---

## 3. Knowledge Base Storage

RAG (Retrieval-Augmented Generation) knowledge base storage is deliberately decoupled from relational persistence:
* Knowledge documents reside as markdown/text files in `data/knowledge/` or `configs/rag/`.
* Vector embeddings are indexed using FAISS (`Retriever` in `src/rag/retriever.py`) and stored on disk (`outputs/rag_index/`).
* Operational relational tables store only transactional and conversational state, not vector chunks.

---

## 4. Schema Specifications

The schema comprises five primary tables defined in `src/agent/db_models.py` and versioned via Alembic migrations.

### Table: `sessions`
Stores transient and active conversational session state.
* `session_id` (`VARCHAR(64)`, Primary Key)
* `user_id` (`VARCHAR(128)`, Nullable, Indexed)
* `status` (`VARCHAR(32)`, Not Null, Check Constraint: `ACTIVE`, `WAITING_FOR_INPUT`, `WAITING_FOR_CONFIRMATION`, `COMPLETED`, `EXPIRED`, `FAILED`)
* `created_at` (`TIMESTAMPTZ`, Not Null)
* `updated_at` (`TIMESTAMPTZ`, Not Null)
* `expires_at` (`TIMESTAMPTZ`, Not Null, Indexed)
* `current_intent` (`VARCHAR(128)`, Nullable)
* `workflow_state` (`VARCHAR(64)`, Nullable)
* `pending_action` (`VARCHAR(64)`, Nullable)
* `pending_parameters` (`JSON`, Not Null, Default `{}`)
* `confirmation_state` (`JSON`, Not Null, Default `{}`)
* `metadata` (`JSON`, Not Null, Default `{}`)
* `version` (`INTEGER`, Not Null, Default `1`): Optimistic concurrency control version counter.

### Table: `memory_records`
Stores durable user preferences and workflow context.
* `id` (`VARCHAR(64)`, Primary Key)
* `user_id` (`VARCHAR(128)`, Not Null, Indexed)
* `category` (`VARCHAR(32)`, Not Null, Check Constraint: `PREFERENCE`, `WORKFLOW_CONTEXT`, `COMMUNICATION_PREFERENCE`)
* `key` (`VARCHAR(128)`, Not Null)
* `value` (`TEXT`, Not Null)
* `source` (`VARCHAR(64)`, Not Null)
* `created_at` (`TIMESTAMPTZ`, Not Null)
* `updated_at` (`TIMESTAMPTZ`, Not Null)
* `expires_at` (`TIMESTAMPTZ`, Nullable)
* `metadata` (`JSON`, Not Null, Default `{}`)
* `version` (`INTEGER`, Not Null, Default `1`): Optimistic concurrency control version counter.

### Table: `audit_events`
Append-only log of operational and business events.
* `event_id` (`VARCHAR(64)`, Primary Key)
* `timestamp` (`TIMESTAMPTZ`, Not Null)
* `event_type` (`VARCHAR(64)`, Not Null, Indexed)
* `request_id` (`VARCHAR(64)`, Nullable, Indexed)
* `conversation_id` (`VARCHAR(64)`, Nullable)
* `session_id` (`VARCHAR(64)`, Nullable)
* `actor` (`VARCHAR(128)`, Nullable)
* `action` (`VARCHAR(64)`, Nullable)
* `resource` (`VARCHAR(128)`, Nullable)
* `outcome` (`VARCHAR(32)`, Not Null)
* `policy` (`VARCHAR(64)`, Nullable)
* `reason` (`TEXT`, Nullable)
* `metadata` (`JSON`, Not Null, Default `{}`)

### Table: `security_events`
Append-only log of security violations, cross-user access attempts, and authentication anomalies.
* `event_id` (`VARCHAR(64)`, Primary Key)
* `timestamp` (`TIMESTAMPTZ`, Not Null)
* `type` (`VARCHAR(64)`, Not Null, Indexed)
* `severity` (`VARCHAR(16)`, Not Null, Check Constraint: `INFO`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`)
* `request_id` (`VARCHAR(64)`, Nullable)
* `actor` (`VARCHAR(128)`, Nullable)
* `resource` (`VARCHAR(128)`, Nullable)
* `outcome` (`VARCHAR(32)`, Not Null)
* `reason` (`TEXT`, Not Null)

### Table: `idempotency_records`
Ensures non-idempotent tool actions execute at most once across process restarts and retries.
* `user_id` (`VARCHAR(128)`, Composite Primary Key)
* `request_id` (`VARCHAR(64)`, Composite Primary Key)
* `action` (`VARCHAR(64)`, Not Null)
* `result_status` (`VARCHAR(32)`, Not Null)
* `executed_at` (`TIMESTAMPTZ`, Not Null)
* `expires_at` (`TIMESTAMPTZ`, Not Null)

---

## 5. Concurrency and Optimistic Locking

### Multi-Process Optimistic Concurrency Control (Phase 13)
To prevent lost updates, race conditions, and dirty writes in multi-process deployments, `sessions` and `memory_records` utilize optimistic concurrency control:
1. Every record includes an integer `version` initialized to `1`.
2. When updating an existing row, the write is guarded by a compare-and-swap (CAS) predicate:
   ```sql
   UPDATE sessions
   SET ..., version = version + 1
   WHERE session_id = :session_id AND version = :expected_version
   ```
3. If zero rows match:
   * The repository checks if the row exists with a newer version.
   * If a version mismatch is detected, the operation raises a typed `ConcurrentModificationError`.
   * Stale updates are rejected deterministically and never overwrite newer data.

### Atomic Confirmation Consumption
Two-phase confirmation workflows (e.g. booking an appointment) must never execute twice on duplicate or concurrent "yes" responses:
* `PostgresSessionRepository.try_consume_pending_confirmation()` executes a single atomic SQL statement guarded by:
  `workflow_state = 'AWAITING_CONFIRMATION'`, `pending_action IS NOT NULL`, and `expires_at > NOW()`.
* Upon matching, it clears `workflow_state` and increments `version = version + 1`, returning the consumed parameters.
* Any concurrent or replay request observes `0` matching rows and returns `None`, guaranteeing at-most-once execution.

---

## 6. Data Retention and Lifecycle

* **Session Expiry**: Sessions default to a 30-minute time-to-live (`DEFAULT_SESSION_TTL = 30 min`). Expiration is evaluated lazily upon read/write. Expired sessions are transitioned to `EXPIRED` status, clearing any pending actions.
* **Memory Expiry**: Individual memory records support an optional `expires_at` timestamp. Expired records are omitted from context assembly.
* **Idempotency Expiry**: Idempotency reservations default to a 24-hour TTL (`DEFAULT_TTL = 24h`). Expired entries can be safely re-reserved.
* **Storage Hygiene**: Because expiration is evaluated on access, no scheduled background cleanup daemon is mandatory for correct functioning, though periodic archiving may be configured.

---

## 7. Backup and Disaster Recovery

### Recovery Guarantees
* **Restart Recovery (Verified in Phase 12.12)**: The database layer was explicitly tested against simulated hard process crashes and restarts. All active sessions, user preferences, security events, and idempotency locks persist intact.
* **Crash Consistency**: Repository operations execute within atomic transactions (`session_scope()`), guaranteeing that failures trigger automatic rollback without partial writes.

### Operational Out-of-Scope Items
* Automated off-site backups, point-in-time recovery (PITR), and read replicas are infrastructure-level concerns managed by cloud providers (e.g. AWS RDS Aurora, GCP Cloud SQL) and are not orchestrated directly by application code.

---

## 8. Open Technical Debt and Future Work

1. **Multi-Process Write Contention Backoff**: Under heavy concurrent write contention for the exact same session ID, `ConcurrentModificationError` is raised immediately. Adding application-level retry loops with exponential jitter would improve client experience.
2. **Real PostgreSQL Performance Profile**: In this development environment, performance baselines were benchmarked against SQLite as a disclosed stand-in. Production deployments should execute `scripts/validate_real_postgres.py` against live PostgreSQL hardware to establish network and disk I/O baselines.
