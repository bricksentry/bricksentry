-- Grants for the bricksentry app's service principal.
-- Find its application ID on the app's page (Authorization tab, "Service principal"),
-- then replace `<app-sp>` below. Adjust catalogs and schemas to the tables agents will check.

-- Tables bricksentry may read (the base tables and the candidate tables your CI builds)
GRANT USE CATALOG ON CATALOG main TO `<app-sp>`;
GRANT USE SCHEMA ON SCHEMA main.sales TO `<app-sp>`;
GRANT SELECT ON SCHEMA main.sales TO `<app-sp>`;

-- Blast radius: Unity Catalog lineage. Without it, checks still run and add a
-- lineage-unavailable warning.
GRANT USE CATALOG ON CATALOG system TO `<app-sp>`;
GRANT USE SCHEMA ON SCHEMA system.access TO `<app-sp>`;
GRANT SELECT ON TABLE system.access.table_lineage TO `<app-sp>`;

-- Only if you remove --read-only from app.yaml: previews run on shallow clones here.
-- CREATE SCHEMA IF NOT EXISTS main.bricksentry_sandbox;
-- GRANT USE SCHEMA, CREATE TABLE ON SCHEMA main.bricksentry_sandbox TO `<app-sp>`;
