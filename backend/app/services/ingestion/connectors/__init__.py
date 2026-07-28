from app.services.ingestion.connectors.base import BaseConnector, RawAct
from app.services.ingestion.connectors.gov_opendata import GovOpenDataConnector
from app.services.ingestion.connectors.lexuz import SEED_ACT_IDS, LexUzConnector
from app.services.ingestion.connectors.norma import NormaConnector

CONNECTORS = {
    "lexuz": LexUzConnector,
    "norma": NormaConnector,
    "gov_opendata": GovOpenDataConnector,
}


def get_connector(name: str, **kwargs) -> BaseConnector:
    if name not in CONNECTORS:
        raise ValueError(f"unknown connector: {name} (available: {', '.join(CONNECTORS)})")
    return CONNECTORS[name](**kwargs)


__all__ = [
    "BaseConnector",
    "RawAct",
    "LexUzConnector",
    "NormaConnector",
    "GovOpenDataConnector",
    "SEED_ACT_IDS",
    "CONNECTORS",
    "get_connector",
]
