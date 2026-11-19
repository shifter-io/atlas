"""Why a droplet create left names without a droplet (PULSE-FLEET-FIX-1, DROPLET-CREATE-ERR-1),
shared by the pulse fleet and the hour launch: DigitalOcean's error id and message, the HTTP
statuses of the latest creates, never a header, an address or the token."""

from __future__ import annotations

from . import scope
from .do_api import CreateError


def create_failure(cloud, error: Exception | None) -> tuple[str, dict]:
    """Why a create left names without a droplet: the class (DigitalOcean's error id, else
    `http_<status>`; `uncertain` for a lost answer whose droplet was not found by name;
    `not_returned` for a name the answer left out; the exception's type for a create refused
    before any request, as `OldImageError`) and scope.error_detail with DigitalOcean's message,
    without secrets, and the class as `code`."""
    kind, detail = _classify(cloud, error)
    return kind, detail | {"code": kind}


def _classify(cloud, error: Exception | None) -> tuple[str, dict]:
    if error is None:
        return "not_returned", scope.error_detail(cloud, [], failed=["not in the answer"])
    detail = scope.error_detail(cloud, [], error)
    if not isinstance(error, CreateError):
        return type(error).__name__, detail
    if error.detail:
        message = f"{error}: {error.detail}"
        detail["message"] = scope.SECRETS.sub("[removed]", message)[: scope.MESSAGE_LIMIT]
    if error.uncertain:
        return "uncertain", detail
    return error.code or (f"http_{error.status}" if error.status else "no_answer"), detail
