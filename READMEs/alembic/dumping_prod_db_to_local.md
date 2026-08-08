# TLDR

Dumping and restoring a DB to sync production with local for testing 

## DUMP

pg_dump 'postgresql://postgres.suakmcavrlfzllvrckea:<PASSWORD_HERE>@aws-0-us-east-1.pooler.supabase.com:5432/postgres' \
  --no-owner --no-privileges --schema=public \
  | grep -v '^SET transaction_timeout' > db_backups/prod.sql


## RESTORE (IMPORT?)

psql "$(grep -m1 '^POSTGRES_URL=' .env | cut -d= -f2- | sed 's/cmdlabs-test-pg/127.0.0.1/')" \
  -v ON_ERROR_STOP=1 \
  -c 'DROP SCHEMA public CASCADE' \
  -f db_backups/prod.sql