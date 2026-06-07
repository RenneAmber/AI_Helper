CREATE TABLE IF NOT EXISTS conversations (
  id SERIAL PRIMARY KEY,
  session_id VARCHAR(128) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  role VARCHAR(32) NOT NULL,
  content TEXT NOT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id SERIAL PRIMARY KEY,
  event_type VARCHAR(64) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  route VARCHAR(255) NOT NULL,
  client_ip VARCHAR(64) NOT NULL DEFAULT '',
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_session_id ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_event_type ON audit_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
