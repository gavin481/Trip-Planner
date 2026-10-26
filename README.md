# Trip-Planner

Standalone Japan & Bali trip itinerary app, deployed on Railway.

- Build: Railpack (Python 3.13) via `railway.toml` / `railpack.json`
- Start: `python app/trip/server.py` (listens on `$PORT`)
- Healthcheck: `/health`
- Trip data/PDFs live on the Railway volume; use `scripts/copy_trip_data.py` to seed it.
