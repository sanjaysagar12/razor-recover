"""
Gunicorn entrypoint.

`python webhook_receiver.py` runs `_startup_checks()` (validates
RAZORPAY_WEBHOOK_SECRET, warms the confidence-gate probability band, starts
the PTP deadline-sweep background thread) only under `if __name__ ==
"__main__"`. Gunicorn imports the app module instead of executing it as
__main__, so that block never runs -- this module calls it explicitly so
gunicorn workers get the same startup behavior as the dev-server path.

Run: gunicorn -c gunicorn.conf.py wsgi:app
"""

import webhook_receiver

webhook_receiver._startup_checks()

app = webhook_receiver.app
