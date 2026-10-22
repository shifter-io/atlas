"""The census/pulse orchestrator: scheduler, batch planning and barrier, and the node API.

`app.build_app()` assembles one aiohttp application (node API, scheduler, room for admin
routes); `proxy-index orchestrator` serves it. Nodes (`proxy_index.node`) hold the
provider credentials and post finished run directories back for `storage.ingest`.
"""
