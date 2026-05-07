# Migrations

This repo ships without a `migrations/` directory. You generate the initial migration the first time you set up a database; from then on, every model change gets its own migration.

## First time

```bash
flask --app wsgi db init
flask --app wsgi db migrate -m "initial"
flask --app wsgi db upgrade
```

`db init` creates the `migrations/` directory. `db migrate` autogenerates a migration from the current models. `db upgrade` applies it. Commit `migrations/` to the repo so the same migration runs in production.

## After model changes

```bash
flask --app wsgi db migrate -m "describe what changed"
flask --app wsgi db upgrade
```

Inspect the generated file in `migrations/versions/` before committing — autogeneration handles most cases but doesn't always pick up renames or constraint tweaks correctly.

## In production

The `release: flask db upgrade` line in the `Procfile` runs `db upgrade` automatically on every Railway deploy. You don't need to do anything; just ensure `migrations/` is committed.

## Resetting

If you've made a mess locally and want to start over:

```bash
rm -rf migrations/
sudo -u postgres dropdb f1predictions && sudo -u postgres createdb -O f1user f1predictions
flask --app wsgi db init
flask --app wsgi db migrate -m "initial"
flask --app wsgi db upgrade
```

Don't do this in production unless you're prepared to lose data.
