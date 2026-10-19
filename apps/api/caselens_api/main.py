from fastapi import FastAPI

from caselens_api import __version__

app = FastAPI(title="CaseLens API", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
