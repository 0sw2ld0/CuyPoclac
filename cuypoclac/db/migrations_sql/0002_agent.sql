CREATE TABLE agent_settings (
  workspace_id      INTEGER PRIMARY KEY REFERENCES workspace(id),
  token             TEXT NOT NULL,
  pseudonym_secret  TEXT NOT NULL,
  port              INTEGER NOT NULL,
  project_path      TEXT
);
