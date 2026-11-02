CREATE TABLE workspace (
  id            INTEGER PRIMARY KEY,
  folder_path   TEXT NOT NULL UNIQUE,
  created_at    TEXT NOT NULL
);

CREATE TABLE schema_version (
  id            INTEGER PRIMARY KEY,
  workspace_id  INTEGER NOT NULL REFERENCES workspace(id),
  version       INTEGER NOT NULL,
  fingerprint   TEXT NOT NULL,
  is_current    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  UNIQUE (workspace_id, version),
  UNIQUE (workspace_id, fingerprint)
);

CREATE TABLE field_group (
  id                TEXT NOT NULL,
  schema_version_id INTEGER NOT NULL REFERENCES schema_version(id),
  label             TEXT NOT NULL,
  ord               INTEGER NOT NULL,
  repeatable        INTEGER NOT NULL DEFAULT 0,
  repeat_mode       TEXT,
  status            TEXT NOT NULL DEFAULT 'active',
  PRIMARY KEY (id, schema_version_id)
);

CREATE TABLE field (
  id                TEXT NOT NULL,
  schema_version_id INTEGER NOT NULL REFERENCES schema_version(id),
  group_id          TEXT,
  label             TEXT NOT NULL,
  normalized_label  TEXT NOT NULL,
  type              TEXT NOT NULL,
  type_source       TEXT NOT NULL DEFAULT 'inferred',
  declared_type     TEXT NOT NULL DEFAULT 'libre',
  options_json      TEXT,
  ord               INTEGER NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active',
  PRIMARY KEY (id, schema_version_id)
);

CREATE TABLE field_alias (
  workspace_id      INTEGER NOT NULL REFERENCES workspace(id),
  normalized_label  TEXT NOT NULL,
  field_id          TEXT NOT NULL,
  PRIMARY KEY (workspace_id, normalized_label)
);

CREATE TABLE field_role (
  schema_version_id INTEGER NOT NULL REFERENCES schema_version(id),
  role              TEXT NOT NULL,
  field_id          TEXT NOT NULL,
  PRIMARY KEY (schema_version_id, role, field_id)
);

CREATE TABLE migration (
  id            INTEGER PRIMARY KEY,
  workspace_id  INTEGER NOT NULL REFERENCES workspace(id),
  from_version  INTEGER NOT NULL,
  to_version    INTEGER NOT NULL,
  ops_json      TEXT NOT NULL,
  applied_at    TEXT,
  UNIQUE (workspace_id, from_version, to_version)
);

CREATE TABLE source_file (
  id                INTEGER PRIMARY KEY,
  workspace_id      INTEGER NOT NULL REFERENCES workspace(id),
  rel_path          TEXT NOT NULL,
  file_hash         TEXT NOT NULL,
  schema_version_id INTEGER REFERENCES schema_version(id),
  last_imported_at  TEXT,
  status            TEXT NOT NULL,
  repeated_rows_hint TEXT,
  UNIQUE (workspace_id, rel_path)
);

CREATE TABLE record (
  id                INTEGER PRIMARY KEY,
  workspace_id      INTEGER NOT NULL REFERENCES workspace(id),
  origin            TEXT NOT NULL DEFAULT 'import',
  status            TEXT NOT NULL DEFAULT 'active',
  source_file_id    INTEGER REFERENCES source_file(id),
  source_row        INTEGER,
  origin_version    INTEGER NOT NULL,
  record_key        TEXT,
  data              TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  imported_at       TEXT,
  CHECK (origin = 'manual' OR (source_file_id IS NOT NULL AND source_row IS NOT NULL))
);
CREATE INDEX idx_record_key ON record(workspace_id, record_key);
CREATE INDEX idx_record_source ON record(source_file_id);

CREATE TABLE record_edit (
  record_id         INTEGER NOT NULL REFERENCES record(id) ON DELETE CASCADE,
  field_id          TEXT NOT NULL,
  value             TEXT,
  base_value        TEXT,
  incoming_value    TEXT,
  status            TEXT NOT NULL DEFAULT 'active',
  edited_at         TEXT NOT NULL,
  PRIMARY KEY (record_id, field_id)
);

CREATE TABLE import_issue (
  id              INTEGER PRIMARY KEY,
  source_file_id  INTEGER NOT NULL REFERENCES source_file(id),
  row             INTEGER,
  field_id        TEXT,
  severity        TEXT NOT NULL,
  code            TEXT,
  message         TEXT NOT NULL
);
CREATE INDEX idx_issue_file ON import_issue(source_file_id);
