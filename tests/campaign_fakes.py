"""An in-process fake DigitalOcean API for campaign tests; no request ever leaves the machine."""

from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

from proxy_index.campaign.do_api import DigitalOcean

TOKEN = "fake-do-token-for-tests-only"
PROD_PROJECT = "prj-production"
CAMPAIGN_PROJECT = "prj-proxy-index"
CID = "20270101t220000z-0123456789ab"
SID = "01234567"
OTHER_SID = "89abcdef"
SPEC_TIME = "2027-01-01T21:00:00Z"
CREATE_TIME = "2027-01-01T21:58:30Z"
EARLY_TIME = "2026-12-31T08:00:00Z"
MANAGER_ID = "500001"


@dataclass
class Fault:
    method: str
    path: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    times: int = 1


class FakeDigitalOcean:
    """Holds droplets, projects, keys, firewalls and snapshots and logs every request."""

    def __init__(self):
        self.droplets: dict[str, dict] = {}
        self.projects = {PROD_PROJECT: set(), CAMPAIGN_PROJECT: set()}
        self.project_names = {PROD_PROJECT: "LeftClick Production", CAMPAIGN_PROJECT: "Proxy Index"}
        self.resources = {"ssh_key": {}, "firewall": {}, "snapshot": {}}
        self.requests: list[tuple[str, str, dict, bool]] = []
        self.faults: list[Fault] = []
        self.deleted: list[tuple[str, str]] = []
        self.assign_delay = 0
        self.pending: set[str] = set()
        self.sizes = [{"slug": "c-2", "available": True, "regions": ["lon1", "fra1", "nyc9"]},
                      {"slug": "s-1vcpu-1gb", "available": False, "regions": ["lon1"]}]
        self._ids = itertools.count(700001)

    def add(self, name, tags=(), project=PROD_PROJECT, created=CREATE_TIME, droplet_id=None,
            ip="203.0.113.10"):
        droplet_id = str(droplet_id or next(self._ids))
        self.droplets[droplet_id] = {
            "id": int(droplet_id), "name": name, "tags": list(tags), "created_at": created,
            "status": "active", "size_slug": "c-2", "region": {"slug": "lon1"},
            "networks": {"v4": [{"type": "private", "ip_address": "192.0.2.2"},
                                {"type": "public", "ip_address": ip}]},
        }
        if project:
            self.projects[project].add(droplet_id)
        return droplet_id

    def add_resource(self, kind, resource_id, **values):
        self.resources[kind][str(resource_id)] = {"id": resource_id, **values}
        return str(resource_id)

    def seed_production(self, count=1125):
        """Production-like droplets in other projects, as the real account holds."""
        tag_sets = ([], ["prod"], ["k8s", "prod"], ["db"], ["lb", "web"])
        for index in range(count):
            self.add(f"prod-node-{index:04d}", tag_sets[index % len(tag_sets)],
                     created=EARLY_TIME if index % 2 else CREATE_TIME)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        route = app.router
        route.add_get("/v2/account", self._account)
        route.add_get("/v2/droplets", self._list)
        route.add_post("/v2/droplets", self._create)
        route.add_delete("/v2/droplets", self._bulk_delete)
        route.add_get("/v2/projects", self._projects)
        route.add_get("/v2/projects/{pid}/resources", self._project_resources)
        route.add_post("/v2/projects/{pid}/resources", self._assign)
        route.add_get("/v2/sizes", self._sizes)
        for kind, prefix in (("droplet", "/v2/droplets"), ("ssh_key", "/v2/account/keys"),
                             ("firewall", "/v2/firewalls"), ("snapshot", "/v2/snapshots")):
            route.add_get(prefix + "/{id}", self._getter(kind))
            route.add_delete(prefix + "/{id}", self._deleter(kind))
        return app

    @web.middleware
    async def _middleware(self, request, handler):
        authorized = request.headers.get("Authorization") == f"Bearer {TOKEN}"
        self.requests.append((request.method, request.path, dict(request.query), authorized))
        if not authorized:
            return web.json_response({"id": "unauthorized"}, status=401)
        for fault in self.faults:
            if fault.times and fault.method == request.method and request.path == fault.path:
                fault.times -= 1
                return web.json_response({"id": "fault"}, status=fault.status,
                                         headers=fault.headers)
        response = await handler(request)
        response.headers["ratelimit-remaining"] = "4321"
        return response

    def _page(self, request, key, items):
        page, size = int(request.query.get("page", 1)), int(request.query.get("per_page", 20))
        chunk = items[(page - 1) * size:page * size]
        return web.json_response({key: chunk, "meta": {"total": len(items)}})

    async def _account(self, request):
        return web.json_response({"account": {"droplet_limit": 10000, "status": "active",
                                              "email": "owner-at-example", "uuid": "secret"}})

    async def _list(self, request):
        tag = request.query.get("tag_name")
        found = [d for d in self.droplets.values() if tag is None or tag in d["tags"]]
        return self._page(request, "droplets", found)

    async def _create(self, request):
        body = await request.json()
        created = [self.droplets[self.add(name, body["tags"], project=None)]
                   for name in body["names"]]
        return web.json_response({"droplets": created}, status=202)

    async def _bulk_delete(self, request):
        self.deleted.append(("bulk", request.query.get("tag_name", "")))
        return web.Response(status=204)

    async def _projects(self, request):
        items = [{"id": pid, "name": name} for pid, name in self.project_names.items()]
        return self._page(request, "projects", items)

    async def _project_resources(self, request):
        pid = request.match_info["pid"]
        if self.pending:
            if self.assign_delay <= 0:
                self.projects[CAMPAIGN_PROJECT] |= self.pending
                self.pending.clear()
            self.assign_delay -= 1
        items = [{"urn": f"do:droplet:{i}"} for i in sorted(self.projects.get(pid, ()))]
        return self._page(request, "resources", items + [{"urn": "do:volume:abc"}])

    async def _assign(self, request):
        pid = request.match_info["pid"]
        ids = {urn.removeprefix("do:droplet:") for urn in (await request.json())["resources"]}
        for members in self.projects.values():
            members -= ids
        if pid == CAMPAIGN_PROJECT and self.assign_delay > 0:
            self.pending |= ids
        else:
            self.projects[pid] |= ids
        return web.json_response({"resources": []})

    async def _sizes(self, request):
        return self._page(request, "sizes", self.sizes)

    def _getter(self, kind):
        async def get(request):
            store = self.droplets if kind == "droplet" else self.resources[kind]
            found = store.get(request.match_info["id"])
            if found is None:
                return web.json_response({"id": "not_found"}, status=404)
            return web.json_response({kind: found})

        return get

    def _deleter(self, kind):
        async def delete(request):
            store = self.droplets if kind == "droplet" else self.resources[kind]
            store.pop(request.match_info["id"], None)
            self.deleted.append((kind, request.match_info["id"]))
            return web.Response(status=204)

        return delete


async def no_sleep(_seconds):
    return None


@asynccontextmanager
async def running(fake: FakeDigitalOcean, **options):
    server = TestServer(fake.app())
    await server.start_server()
    options.setdefault("sleep", no_sleep)
    client = DigitalOcean(lambda: TOKEN, base_url=str(server.make_url("")), **options)
    try:
        yield client
    finally:
        await client.close()
        await server.close()
