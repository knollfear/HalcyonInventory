#!/usr/bin/env sh
# Container entrypoint for Railway.
#
# Lives in the repo rather than inline in railway.json so that:
#   * a shell definitely interprets it (inline commands are not always run
#     through one, which would leave ${PORT} unexpanded and && literal);
#   * the log markers below show exactly how far startup got;
#   * collectstatic failing degrades to "no static files" instead of "no
#     server at all" — an unstyled site beats an outage.
#
# The two things that must be right for Railway to route traffic here:
#   bind 0.0.0.0 (not gunicorn's default 127.0.0.1, unreachable from outside)
#   port $PORT   (whatever the platform assigns)
#
# WHAT HAS ALREADY RUN BY THE TIME THIS STARTS
# Railway's preDeploy command (set in the dashboard, which overrides
# railway.json) runs first:
#
#     python manage.py migrate && python manage.py collectstatic --noinput
#
# So migrations are already applied here, and deliberately are not repeated
# below. preDeploy *gates* the deploy: a migration that fails there means the
# new version never goes live and the old one keeps serving, which is a
# rollback. Migrating here instead would buy an outage — either gunicorn
# boots against a schema that does not match the code and every page 500s, or
# the chain breaks and there is no server at all while ON_FAILURE retries it
# ten times. This also runs once per replica and per restart, where preDeploy
# runs once, so two replicas would race to migrate the same database.
#
# collectstatic is the exception that has to happen in both places: preDeploy
# runs in a throwaway container, so files written to its filesystem never
# reach the one serving traffic. Migrations land in the database, which does
# persist — that asymmetry is the whole reason the two are treated
# differently, and it is not an oversight to tidy up.

echo "[start] collectstatic..."
if python manage.py collectstatic --noinput; then
    echo "[start] collectstatic ok"
else
    echo "[start] collectstatic FAILED — continuing so the app still serves"
fi

echo "[start] launching gunicorn on 0.0.0.0:${PORT:-8000}"
# exec so gunicorn becomes PID 1 and receives shutdown signals directly.
exec gunicorn mysite.wsgi \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
