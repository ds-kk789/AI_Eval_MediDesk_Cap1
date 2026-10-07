"""Start the MediDesk API and the web app together.

    python run_web.py           replay mode (default): no model calls, no key needed
    python run_web.py --live    /ask and /clinical/ask call the model (Groq budget)

API:     http://127.0.0.1:8003   (backend.main, X-API-Key -> role)
Web app: http://127.0.0.1:8080   (static files in web/, talks only to the API)

Replay mode answers the ask routes from the responses recorded by the e2e run
(reports/_cache, see backend/replay.py). Patient lookup, audit and release
health never call a model, so they behave the same in both modes.
"""

import argparse
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_PORT = 8080


def serve_web(host):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    server = ThreadingHTTPServer((host, WEB_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live", action="store_true", help="call the model instead of replaying"
    )
    args = parser.parse_args()

    # Must be set before backend.config is imported: it reads the env once.
    os.environ["MEDIDESK_REPLAY"] = "0" if args.live else "1"

    if not args.live:
        # Replay never needs a model. Blank the keys (load_dotenv won't
        # override a set variable) so nothing in this process can spend budget.
        for name in ("GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GOOGLE_API_KEY"):
            os.environ[name] = ""

    import uvicorn

    from backend import config

    serve_web(config.HOST)
    mode = "LIVE (model calls use the Groq budget)" if args.live else "REPLAY (no model calls)"
    print(f"MediDesk web app: http://{config.HOST}:{WEB_PORT}  ·  mode: {mode}")
    print(f"MediDesk API:     {config.BASE_URL}")

    uvicorn.run("backend.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
