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
