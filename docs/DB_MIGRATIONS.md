# Database migration and adoption guide

## Fresh PostgreSQL database

Use the reviewed application commit and production environment file. The production settings must contain strong, unique credentials and `DB_AUTO_CREATE_SCHEMA=false`.

```bash
docker compose -f docker-compose.prod.yml run --rm app python -m alembic upgrade head
docker compose -f docker-compose.prod.yml run --rm app python -m alembic current
docker compose -f docker-compose.prod.yml up -d
```

The initial revision creates the tables and indexes represented by the current SQLAlchemy models. CI verifies an empty PostgreSQL database can be upgraded, checked against the ORM metadata, downgraded, and upgraded again.

Do not enable application auto-create as a substitute for migrations in production.

## Existing database created by the legacy application

The earlier application could create tables automatically through `Base.metadata.create_all()`. **Do not run the initial migration against a database that already has those tables**; Alembic would try to create them again.

1. Stop application writes or schedule a maintenance window.
2. Take and verify a PostgreSQL backup.
3. Run the ORM drift check against the existing database:

   ```bash
   docker compose -f docker-compose.prod.yml run --rm app python -m alembic check
   ```

4. If the check reports any pending schema operations, stop here. Review the differences and prepare a dedicated reconciliation migration. Do not stamp the database yet.
5. Only when the check reports no pending operations, record that the existing schema already matches the baseline:

   ```bash
   docker compose -f docker-compose.prod.yml run --rm app python -m alembic stamp head
   docker compose -f docker-compose.prod.yml run --rm app python -m alembic current
   ```

6. Deploy the reviewed application, confirm readiness and verify normal reads/writes before ending the maintenance window.

Stamping records migration history; it does not create, repair, or validate missing data. Retain the backup until the deployment has been verified.

## Routine schema changes

1. Change SQLAlchemy models intentionally.
2. Generate a candidate revision in a development database with `python -m alembic revision --autogenerate -m "describe schema change"`.
3. Review the migration file manually. Autogenerate is a draft, not a proof of correctness.
4. Test upgrade and downgrade against disposable PostgreSQL, and test upgrade from a copy of the previous schema.
5. Deploy the reviewed migration before starting the new application version.
6. Keep the previous application image available for rollback. A database rollback is a separate, reviewed action; do not blindly downgrade after writes have used a new schema.

## Connection URL note

`alembic/env.py` escapes percent signs for ConfigParser interpolation. This preserves URL-encoded reserved characters in generated PostgreSQL connection URLs. Keep credentials in the environment/secret store; never commit a production `.env` file.
