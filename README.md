# Trip-Planner

Standalone Japan & Bali trip itinerary app, deployed on Railway.

- Build: Railpack (Python 3.13) via `railway.toml` / `railpack.json`
- Start: `python app/trip/server.py` (listens on `$PORT`)
- Healthcheck: `/health`
- Trip data/PDFs live on the Railway volume; use `scripts/copy_trip_data.py` to seed it.

## Deploy from hosted DEV to PROD

On the hosted DEV trip site, the itinerary page shows **Deploy to production**. It asks first, then uploads the app files DEV is running (`railway up`, via `app/railway_deploy.py`) to the `production` environment of this Railway project. Trip data (itinerary, booking PDFs, photos, password and Gmail files) lives on each environment's volume at `DLM_DATA_DIR` (usually `/data`) and is never uploaded or replaced; `.railwayignore` excludes `data/`. Lost Miniatures is a separate project and is not touched. On PROD the button is hidden. On a PC (not Railway) the icon button still copies this PC's itinerary and PDFs to production.

One-time setup:

1. In Railway project **Trip-Planner**, make sure a `production` environment exists with the trip service and its volume (`DLM_DATA_DIR=/data`).
2. Project Settings → Tokens → create a **project token** with Environment = `production`.
3. On the trip service in the `development` environment set `RAILWAY_PROD_TOKEN` to that token and `DLM_ENV=DEV`. On the `production` environment set `DLM_ENV=PROD`.
4. Redeploy DEV so it sees the new variables.

The service name defaults to `RAILWAY_SERVICE_NAME` (set by Railway), then `TRIP_SERVICE`, then `Trip-Planner`. An account token (`RAILWAY_API_TOKEN`) cannot upload to production.
