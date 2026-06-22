"""
Single-process entrypoint for the Mykare Voice AI backend.

Runs both pieces together:
  - FastAPI (token + read APIs) via Uvicorn in a daemon background thread
  - the LiveKit agent worker on the main thread via cli.run_app()

The agent worker (cli.run_app) is blocking and owns the main thread and its own
asyncio loop; Uvicorn runs its own loop in a daemon thread that dies with the
process. So one command brings up both.

Run:  python main.py dev      # hot-reload for local dev
      python main.py start    # production
(cli.run_app reads sys.argv, so you must pass a LiveKit subcommand.)
"""

import threading

import uvicorn
from livekit.agents import WorkerOptions, cli

from agent import entrypoint, prewarm
from server import app


def run_uvicorn() -> None:
    """Run the FastAPI server (own event loop, separate thread)."""
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    # Start FastAPI in a daemon thread so it exits when the worker exits.
    threading.Thread(target=run_uvicorn, daemon=True).start()

    # Run the LiveKit agent worker on the main thread (blocking).
    # agent_name => explicit dispatch: this worker only joins rooms that request
    # "mykare-frontdesk" (the frontend token sets this). Avoids grabbing
    # unrelated rooms in a shared LiveKit project.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="mykare-frontdesk",
            # The turn-detector model can take >10s to load on a cold/slow start;
            # give the inference + job subprocesses room so they aren't killed.
            initialize_process_timeout=60,
        )
    )
