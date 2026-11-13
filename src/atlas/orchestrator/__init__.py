"""The census/pulse orchestrator: scheduler, batch planning and barrier, and the node API.

`app.build_app()` assembles one aiohttp application (node API, scheduler, room for admin
routes); `atlas orchestrator` serves it. Nodes (`atlas.node`) hold the
provider credentials and post finished run directories back for `storage.ingest`.
"""
