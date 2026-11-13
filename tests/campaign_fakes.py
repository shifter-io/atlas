"""An in-process fake DigitalOcean API for campaign tests; no request ever leaves the machine."""

from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

from atlas.campaign.do_api import DigitalOcean

TOKEN = "fake-do-token-for-tests-only"
PROD_PROJECT = "11111111-1111-4111-8111-111111111111"  # the team's default project
CAMPAIGN_PROJECT = "22222222-2222-4222-8222-222222222222"
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
        self.tags: set[str] = set()  # as on DigitalOcean: made by POST /v2/tags or a create
        self.requests: list[tuple[str, str, dict, bool]] = []
        self.faults: list[Fault] = []
        self.deleted: list[tuple[str, str]] = []
        self.assign_delay = 0
        self.create_then_fail = 0
        self.pending: set[str] = set()
        self.unlisted: list[tuple[str, str]] = []
        self.sizes = [
            {"slug": "c-2", "available": True, "regions": ["lon1", "fra1", "nyc9"]},
            {"slug": "s-1vcpu-1gb", "available": False, "regions": ["lon1"]},
        ]
        self._ids = itertools.count(700001)
        self.images: dict[str, dict] = {}
        self.actions: dict[str, dict] = {}
        self.action_polls = 0  # each action answers "in-progress" this many times first
        self.failing_actions: set[str] = set()  # action types that end "errored"
        self.bodies: list[tuple[str, str, dict]] = []
        # Eventual consistency just after a create: droplet id -> overrides, one per GET.
        self.stale: dict[str, list[dict]] = {}

    def add(
        self,
        name,
        tags=(),
        project=PROD_PROJECT,
        created=CREATE_TIME,
        droplet_id=None,
        ip="203.0.113.10",
    ):
        droplet_id = str(droplet_id or next(self._ids))
        self.droplets[droplet_id] = {
            "id": int(droplet_id),
            "name": name,
            "tags": list(tags),
            "created_at": created,
            "status": "active",
            "size_slug": "c-2",
            "region": {"slug": "lon1"},
            "networks": {
                "v4": [
                    {"type": "private", "ip_address": "192.0.2.2"},
                    {"type": "public", "ip_address": ip},
                ]
            },
        }
        self.tags.update(tags)
        if project:
            self.projects[project].add(droplet_id)
        return droplet_id

    def move(self, droplet_id, project=CAMPAIGN_PROJECT):
        """Put a droplet into one project, as a project assignment would."""
        for members in self.projects.values():
            members.discard(str(droplet_id))
        self.projects[project].add(str(droplet_id))

    def add_resource(self, kind, resource_id, **values):
        self.resources[kind][str(resource_id)] = {"id": resource_id, **values}
        return str(resource_id)

    def seed_production(self, count=1125):
        """Production-like droplets in other projects, as the real account holds."""
        tag_sets = ([], ["prod"], ["k8s", "prod"], ["db"], ["lb", "web"])
        for index in range(count):
            self.add(
                f"prod-node-{index:04d}",
                tag_sets[index % len(tag_sets)],
                created=EARLY_TIME if index % 2 else CREATE_TIME,
            )

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        route = app.router
        route.add_get("/v2/account", self._account)
        route.add_get("/v2/droplets", self._list)
        route.add_post("/v2/droplets", self._create)
        route.add_delete("/v2/droplets", self._bulk_delete)
        route.add_get("/v2/projects", self._projects)
        route.add_get("/v2/projects/{pid}", self._project)
        route.add_get("/v2/projects/{pid}/resources", self._project_resources)
        route.add_post("/v2/projects/{pid}/resources", self._assign)
        route.add_get("/v2/sizes", self._sizes)
        route.add_get("/v2/account/keys", self._lister("ssh_key", "ssh_keys"))
        route.add_post("/v2/account/keys", self._add_key)
        route.add_get("/v2/actions/{id}", self._action)
        route.add_post("/v2/droplets/{id}/actions", self._droplet_action)
        route.add_get("/v2/droplets/{id}/snapshots", self._droplet_snapshots)
        route.add_get("/v2/images/{id}", self._getter("image"))
        route.add_post("/v2/images/{id}/actions", self._image_action)
        route.add_post("/v2/firewalls", self._firewall)
        route.add_put("/v2/firewalls/{id}", self._firewall)
        route.add_get("/v2/tags/{name}", self._tag)
        route.add_post("/v2/tags", self._add_tag)
        for kind, prefix in (
            ("droplet", "/v2/droplets"),
            ("ssh_key", "/v2/account/keys"),
            ("firewall", "/v2/firewalls"),
            ("snapshot", "/v2/snapshots"),
        ):
            route.add_get(prefix + "/{id}", self._getter(kind))
            route.add_delete(prefix + "/{id}", self._deleter(kind))
        # Anything else (actions, tag resources, PUT, PATCH, ...) exists, so a leak is visible.
        route.add_route("*", "/{tail:.*}", self._unlisted)
        return app

    async def _unlisted(self, request):
        self.unlisted.append((request.method, request.path))
        return web.json_response({})

    async def _project(self, request):
        pid = request.match_info["pid"]
        if pid not in self.project_names:
            return web.json_response({"id": "not_found"}, status=404)
        return web.json_response({"project": {"id": pid, "name": self.project_names[pid]}})

    @web.middleware
    async def _middleware(self, request, handler):
        authorized = request.headers.get("Authorization") == f"Bearer {TOKEN}"
        self.requests.append((request.method, request.path, dict(request.query), authorized))
        if not authorized:
            return web.json_response({"id": "unauthorized"}, status=401)
        for fault in self.faults:
            if fault.times and fault.method == request.method and request.path == fault.path:
                fault.times -= 1
                return web.json_response(
                    {"id": "fault"}, status=fault.status, headers=fault.headers
                )
        response = await handler(request)
        response.headers["ratelimit-remaining"] = "4321"
        return response

    def _page(self, request, key, items):
        page, size = int(request.query.get("page", 1)), int(request.query.get("per_page", 20))
        chunk = items[(page - 1) * size : page * size]
        return web.json_response({key: chunk, "meta": {"total": len(items)}})

    async def _account(self, request):
        return web.json_response(
            {
                "account": {
                    "droplet_limit": 10000,
                    "status": "active",
                    "email": "owner-at-example",
                    "uuid": "secret",
                }
            }
        )

    async def _list(self, request):
        tag = request.query.get("tag_name")
        found = [d for d in self.droplets.values() if tag is None or tag in d["tags"]]
        return self._page(request, "droplets", found)

    async def _create(self, request):
        body = await request.json()
        created = [self.droplets[self.add(name, body["tags"])] for name in body["names"]]
        if self.create_then_fail:  # created, but the answer is lost
            self.create_then_fail -= 1
            return web.json_response({"id": "server_error"}, status=500)
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

    def _lister(self, kind, key):
        async def listing(request):
            return self._page(request, key, list(self.resources[kind].values()))

        return listing

    async def _body(self, request):
        body = await request.json()
        self.bodies.append((request.method, request.path, body))
        return body

    async def _add_key(self, request):
        body = await self._body(request)
        key = {"id": next(self._ids), **body}
        self.resources["ssh_key"][str(key["id"])] = key
        return web.json_response({"ssh_key": key}, status=201)

    def _new_action(self, kind, resource_id):
        action_id = str(next(self._ids))
        self.actions[action_id] = {
            "id": int(action_id),
            "type": kind,
            "polls": self.action_polls,
            "resource_id": resource_id,
        }
        return web.json_response({"action": self.actions[action_id]}, status=201)

    async def _action(self, request):
        action = self.actions[request.match_info["id"]]
        status = "errored" if action["type"] in self.failing_actions else "completed"
        if action["polls"] > 0:
            action["polls"] -= 1
            status = "in-progress"
        return web.json_response({"action": {**action, "status": status}})

    async def _droplet_action(self, request):
        body, droplet_id = await self._body(request), request.match_info["id"]
        droplet = self.droplets[droplet_id]
        if body["type"] in ("shutdown", "power_off"):
            droplet["status"] = "off"
        else:
            image_id = str(next(self._ids))
            self.images[image_id] = {
                "id": int(image_id),
                "name": body["name"],
                "type": "snapshot",
                "regions": [droplet["region"]["slug"]],
                "droplet": droplet_id,
            }
        return self._new_action(body["type"], droplet_id)

    async def _droplet_snapshots(self, request):
        found = [i for i in self.images.values() if i["droplet"] == request.match_info["id"]]
        return self._page(request, "snapshots", found)

    async def _image_action(self, request):
        body, image_id = await self._body(request), request.match_info["id"]
        if body["type"] not in self.failing_actions:
            self.images[image_id]["regions"].append(body["region"])
        return self._new_action(body["type"], image_id)

    async def _tag(self, request):
        name = request.match_info["name"]
        if name not in self.tags:
            return web.json_response({"id": "not_found"}, status=404)
        return web.json_response({"tag": {"name": name}})

    async def _add_tag(self, request):
        body = await self._body(request)
        self.tags.add(body["name"])
        return web.json_response({"tag": {"name": body["name"]}}, status=201)

    async def _firewall(self, request):
        body = await self._body(request)
        sources = {t for r in body.get("inbound_rules") or [] for t in r["sources"].get("tags", [])}
        if (set(body.get("tags") or []) | sources) - self.tags:  # "tag ... does not exist"
            return web.json_response({"id": "unprocessable_entity"}, status=422)
        firewall_id = request.match_info.get("id") or (
            f"33333333-3333-4333-8333-{next(self._ids):012d}"
        )
        self.resources["firewall"][firewall_id] = {"id": firewall_id, **body}
        return web.json_response(
            {"firewall": self.resources["firewall"][firewall_id]},
            status=200 if request.method == "PUT" else 202,
        )

    def _getter(self, kind):
        async def get(request):
            stores = {"droplet": self.droplets, "image": self.images, **self.resources}
            store = stores[kind]
            found = store.get(request.match_info["id"])
            if found is None:
                return web.json_response({"id": "not_found"}, status=404)
            if kind == "droplet" and self.stale.get(request.match_info["id"]):
                found = found | self.stale[request.match_info["id"]].pop(0)
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
