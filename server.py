"""Gunicorn entrypoint. `server:app` matches the Render Docker command."""

import os

from osp.web.app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
