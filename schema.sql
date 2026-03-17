-- =============================================================================
-- AI RESERVATION AGENT — DATABASE SCHEMA
-- PostgreSQL | Version 1.0
-- =============================================================================
-- Tables: customers, tables, reservations, conversation_sessions
-- Features: Row-level locking to prevent double-booking race conditions
-- =============================================================================

-- Enable UUID generation extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- For fuzzy customer search

-- -----------------------------------------------------------------------------
-- ENUM TYPES
-- -----------------------------------------------------------------------------

CREATE TYPE seating_preference AS ENUM (
    'indoor',
    'outdoor',
    'bar',
    'quiet_corner',
    'window',
    'private_room',
    'no_preference'
);

CREATE TYPE table_status AS ENUM (
    'available',
    'reserved',
    'occupied',
    'maintenance'
);

CREATE TYPE reservation_status AS ENUM (
    'pending',       -- AI collected info but not yet confirmed
    'confirmed',     -- Successfully booked
    'modified',      -- Changed after initial booking
    'cancelled',     -- Customer or staff cancelled
    'completed',     -- Dining finished
    'no_show'        -- Customer never arrived
);

CREATE TYPE notification_status AS ENUM (
    'pending',
    'sent',
    'failed'
);

-- -----------------------------------------------------------------------------
-- CUSTOMERS TABLE
-- Stores guest profiles; linked to multiple reservations over time.
-- -----------------------------------------------------------------------------
CREATE TABLE customers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name       VARCHAR(150) NOT NULL,
    email           VARCHAR(255) UNIQUE,
    phone           VARCHAR(30),
    dietary_notes   TEXT,                          -- Vegetarian, vegan, gluten-free, etc.
    allergy_notes   TEXT,                          -- Critical: nuts, shellfish, dairy, etc.
    vip_status      BOOLEAN DEFAULT FALSE,
    visit_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast customer lookup by phone or email (used by AI agent tools)
CREATE INDEX idx_customers_email ON customers(email);
CREATE INDEX idx_customers_phone ON customers(phone);
CREATE INDEX idx_customers_name  ON customers USING gin(full_name gin_trgm_ops);

-- -----------------------------------------------------------------------------
-- TABLES TABLE
-- Physical tables in the restaurant with their attributes.
-- -----------------------------------------------------------------------------
CREATE TABLE tables (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    table_number        VARCHAR(10) UNIQUE NOT NULL,   -- e.g., "T1", "T12", "B3"
    capacity            INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 20),
    location            seating_preference NOT NULL DEFAULT 'indoor',
    status              table_status NOT NULL DEFAULT 'available',
    has_high_chair      BOOLEAN DEFAULT FALSE,
    is_accessible       BOOLEAN DEFAULT FALSE,          -- Wheelchair accessible
    description         TEXT,                           -- "Near the fireplace, romantic setting"
    floor_number        INTEGER DEFAULT 1,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Index to quickly find tables by location and capacity
CREATE INDEX idx_tables_location ON tables(location);
CREATE INDEX idx_tables_capacity ON tables(capacity);
CREATE INDEX idx_tables_status   ON tables(status);

-- Sample seed data for tables
INSERT INTO tables (table_number, capacity, location, description, is_accessible) VALUES
    ('T01', 2,  'window',       'Cozy window seat with street view', FALSE),
    ('T02', 2,  'window',       'Sunset-facing window table', FALSE),
    ('T03', 4,  'indoor',       'Central dining room, standard', TRUE),
    ('T04', 4,  'indoor',       'Near the fireplace, warm ambiance', FALSE),
    ('T05', 4,  'quiet_corner', 'Secluded corner, ideal for business meals', FALSE),
    ('T06', 6,  'indoor',       'Large round table, great for groups', TRUE),
    ('T07', 2,  'outdoor',      'Garden patio, shaded umbrella', FALSE),
    ('T08', 4,  'outdoor',      'Open terrace with garden view', FALSE),
    ('T09', 6,  'outdoor',      'Large outdoor table, pet-friendly area', FALSE),
    ('T10', 2,  'bar',          'Bar seating, lively atmosphere', FALSE),
    ('T11', 8,  'private_room', 'Private dining room, AV available', TRUE),
    ('T12', 10, 'private_room', 'Grand private suite, ideal for events', TRUE);

-- -----------------------------------------------------------------------------
-- RESERVATIONS TABLE
-- Core booking record. Uses SELECT FOR UPDATE to prevent race conditions.
-- -----------------------------------------------------------------------------
CREATE TABLE reservations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id         UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    table_id            UUID NOT NULL REFERENCES tables(id) ON DELETE RESTRICT,
    reservation_date    DATE NOT NULL,
    start_time          TIME NOT NULL,
    end_time            TIME NOT NULL,                -- Calculated: start + avg dining duration
    party_size          INTEGER NOT NULL CHECK (party_size >= 1),
    status              reservation_status NOT NULL DEFAULT 'pending',
    special_requests    TEXT,                          -- Free-text from AI conversation
    internal_notes      TEXT,                          -- Staff-only notes
    confirmation_code   VARCHAR(12) UNIQUE NOT NULL,  -- Human-readable code, e.g., "RES-A3X9"
    source              VARCHAR(50) DEFAULT 'ai_agent', -- 'ai_agent', 'phone', 'walk_in'
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    -- Ensure no overlapping reservations on the same table (database-level guard)
    -- This works alongside application-level locking for double protection
    CONSTRAINT no_time_overlap EXCLUDE USING gist (
        table_id WITH =,
        tsrange(
            (reservation_date + start_time)::timestamp,
            (reservation_date + end_time)::timestamp,
            '[)'  -- Inclusive start, exclusive end
        ) WITH &&
    ) WHERE (status NOT IN ('cancelled', 'no_show'))
);

-- Add btree_gist for the EXCLUDE constraint
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Indexes for availability checks (the most frequent query)
CREATE INDEX idx_reservations_date_table  ON reservations(reservation_date, table_id);
CREATE INDEX idx_reservations_customer    ON reservations(customer_id);
CREATE INDEX idx_reservations_status      ON reservations(status);
CREATE INDEX idx_reservations_code        ON reservations(confirmation_code);

-- -----------------------------------------------------------------------------
-- CONVERSATION SESSIONS TABLE
-- Persists AI conversation state across HTTP requests.
-- -----------------------------------------------------------------------------
CREATE TABLE conversation_sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_token   VARCHAR(64) UNIQUE NOT NULL,
    customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
    reservation_id  UUID REFERENCES reservations(id) ON DELETE SET NULL,
    messages        JSONB NOT NULL DEFAULT '[]',       -- Full conversation history
    context         JSONB NOT NULL DEFAULT '{}',       -- Extracted booking details in progress
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_active_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '2 hours')
);

CREATE INDEX idx_sessions_token      ON conversation_sessions(session_token);
CREATE INDEX idx_sessions_active     ON conversation_sessions(is_active, expires_at);

-- -----------------------------------------------------------------------------
-- NOTIFICATION LOG TABLE
-- Tracks all outbound emails/SMS for auditing and retry logic.
-- -----------------------------------------------------------------------------
CREATE TABLE notification_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    reservation_id  UUID NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    channel         VARCHAR(10) NOT NULL CHECK (channel IN ('email', 'sms')),
    recipient       VARCHAR(255) NOT NULL,
    template_name   VARCHAR(100),
    status          notification_status DEFAULT 'pending',
    provider_id     VARCHAR(255),                      -- SendGrid/Twilio message ID
    error_message   TEXT,
    sent_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- AUTO-UPDATE TIMESTAMPS TRIGGER
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_tables_updated_at
    BEFORE UPDATE ON tables
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_reservations_updated_at
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- =============================================================================
-- END OF SCHEMA
-- =============================================================================
