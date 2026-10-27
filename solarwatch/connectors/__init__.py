from .base import AuthError, Connector, ConnectorError, TransientError
from .demo import DemoConnector
from .deye import DeyeConnector
from .goodwe_plus import GoodWePlusConnector
from .luxpower import LuxPowerConnector
from .sma import SMAConnector
from .sunsynk import SunsynkConnector
from .victron import VictronConnector

REGISTRY: dict[str, type[Connector]] = {
    "victron": VictronConnector,
    "sunsynk": SunsynkConnector,
    "goodwe_plus": GoodWePlusConnector,
    "sma": SMAConnector,
    "deye": DeyeConnector,
    "luxpower": LuxPowerConnector,
    "demo": DemoConnector,
}

# Platforms that were removed, with what to use instead.
RETIRED = {
    "goodwe": "platform 'goodwe' (legacy GoodWe SEMS) has been removed because GoodWe retired SEMS "
              "in favour of SEMS+. Change it to 'platform: goodwe_plus' and use your SEMS+ login.",
}


def make_connector(account: dict) -> Connector:
    platform = account.get("platform")
    if platform in RETIRED:
        raise ValueError(f"account {account.get('id')!r}: {RETIRED[platform]}")
    if platform not in REGISTRY:
        raise ValueError(f"account {account.get('id')!r}: unknown platform {platform!r} "
                         f"(choose from {', '.join(REGISTRY)})")
    return REGISTRY[platform](account)


__all__ = ["REGISTRY", "RETIRED", "make_connector", "Connector", "ConnectorError", "AuthError", "TransientError"]
