from atlas.onboarding.export import render_report

MANIFEST = {
    "provider": "shifter",
    "product": "<rotating>",
    "campaign_id": "c1",
    "starts_at": "2026-01-01T00:00:00Z",
    "ends_at": "2026-01-02T00:00:00Z",
}


def round_info(number, duration, delay, completed):
    return {
        "round": number,
        "scheduled_at": f"2026-01-01T{number:02d}:00:00Z",
        "duration_seconds": duration,
        "delay_seconds": delay,
        "completed": completed,
    }


def result(rounds, issues=()):
    region = {
        "region": "nyc3",
        "rounds": rounds,
        "requests": 12345,
        "errors": 67,
        "peak_memory_mb": 80.5,
        "settings": {"concurrency": 50, "timeout_seconds": 10},
    }
    return {"regions": [region], "issues": list(issues), "rows": 1000, "complete": not issues}


def test_report_rows_show_each_round_status():
    page = render_report(
        MANIFEST,
        result(
            [
                round_info(0, 3000, 1.25, True),
                round_info(1, 1800, 0.5, True),
                round_info(2, 600, 2.0, False),
                round_info(3, None, None, False),
            ]
        ),
    ).decode()
    rows = page.split("<tbody>")[1]
    assert "<td>nyc3</td><td>0</td><td>2026-01-01T00:00:00Z</td><td>50.0</td>" in rows
    assert "<td>1.2</td><td>Over 40 min</td></tr>" in rows
    assert "<td>30.0</td><td>0.5</td><td>Complete</td></tr>" in rows
    assert "<td>10.0</td><td>2.0</td><td>Incomplete</td></tr>" in rows
    assert "<td>Incomplete</td><td>Not started</td><td>Incomplete</td></tr>" in rows
    assert rows.count("<tr>") == 4


def test_report_escapes_and_summarises_the_campaign():
    page = render_report(MANIFEST, result([], issues=["nyc3: <late>"])).decode()
    assert "shifter / &lt;rotating&gt;" in page and "<li>nyc3: &lt;late&gt;</li>" in page
    assert "12,345 requests; 67 errors; peak memory 80.5 MiB; concurrency 50" in page
    assert "1,000 verified observations" in page and "Coverage: Incomplete" in page
    assert "No missing work" not in page
    assert "No missing work" in render_report(MANIFEST, result([])).decode()
