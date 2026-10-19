-- Local dev only: a separate database for the migration/RLS contract tests,
-- so `make test` never touches the dev database.
CREATE DATABASE caselens_test;
