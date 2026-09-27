# TLDR

Documenting steps of moving Postgres DB from Render.com

## PHASE 1

- 1st created a project called `cmdlabs` in the default Org
- Copied the credentials for the DB provisioned in Supabase into this project
- Run all migrations
  - `alembic upgrade head` WORKED √

## PHASE 2

- Backed up DB
  - ansible-playbook --inventory inventory.prod --key-file "<PATH_TO_PEM_FILE>" backup_db.yml

## PHASE 3

- Run another Cloud Run service dedicated to Command Labs
  - create new project called `command-labs`

## Phase 4

- Run the FRONTEND Next.js web app in the `command-labs` project
- Run the BACKEND FastAPI application in the `command-labs` project
