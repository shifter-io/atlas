"""The census/pulse orchestrator: scheduler, batch planning and barrier, and the node API.

`app.build_app()` assembles one aiohttp application (node API, scheduler, room for admin
routes); `milf-framework orchestrator` serves it. Nodes (`milf_framework.node`) hold the
provider credentials and post finished run directories back for `storage.ingest`.
"""
