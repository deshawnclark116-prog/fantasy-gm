"""FastAPI application factory.

v0.1 exposes health/version and read-only ledger access. There is intentionally no endpoint
that executes transactions.
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request

from fantasy_gm import __version__
from fantasy_gm.config import Settings
from fantasy_gm.decisions.ledger import DecisionLedger, DecisionNotFoundError, TamperDetectedError
from fantasy_gm.domain.ids import DecisionId


# Module-level dependency (and no ``from __future__ import annotations``): FastAPI must be able
# to resolve these annotations at runtime.
def get_ledger(request: Request) -> DecisionLedger:
    led: DecisionLedger = request.app.state.ledger
    return led


def create_app(ledger: DecisionLedger, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="fantasy-gm", version=__version__)
    app.state.ledger = ledger
    app.state.settings = settings or Settings()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "execution_enabled": app.state.settings.execution_enabled,
        }

    @app.get("/v1/decisions/{decision_id}")
    def get_decision(
        decision_id: str, led: Annotated[DecisionLedger, Depends(get_ledger)]
    ) -> dict[str, Any]:
        try:
            entry = led.get(DecisionId(decision_id))
        except DecisionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="decision not found") from exc
        except TamperDetectedError as exc:  # pragma: no cover - integrity failure
            raise HTTPException(status_code=500, detail="ledger integrity failure") from exc
        body: dict[str, Any] = entry.model_dump(mode="json")
        body["current_status"] = entry.current_status.value
        return body

    return app
