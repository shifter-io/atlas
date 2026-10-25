# Campaign 20270101t220000z-0123456789ab: per-hour completeness

Cell = that hour's round of one network in one region. ✓ complete and verified, `partial` some countries interrupted, failed or unverified (each followed by the real-error rate % of answered requests: errors that are neither rate limits nor no-IP answers / requests that were not rate-limited), ✗ ours = our side failed (create, assign, boot, start, or the round failed or ended without closing), ⚠ data = the round ran but its data is not in the campaign root, `running` not finished, · not launched, ‖ paused = the network was paused for that hour (final, no data).

| Network | complete | partial | failed_ours | uncollected | running | pending | paused | cells |
|---|---|---|---|---|---|---|---|---|
| geonode | 4 | 0 | 43 | 0 | 1 | 0 | 0 | 48 |
| shifter | 5 | 1 | 42 | 0 | 0 | 0 | 0 | 48 |
| total | 9 | 1 | 85 | 0 | 1 | 0 | 0 | 96 |

## geonode

| Region | h00 | h01 | h02 | h03 | h04 | h05 | h06 | h07 | h08 | h09 | h10 | h11 | h12 | h13 | h14 | h15 | h16 | h17 | h18 | h19 | h20 | h21 | h22 | h23 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ams3 | ✓ 25 | ✓ 25 | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours |
| tor1 | running | ✓ 25 | ✓ 25 | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours |

## shifter

| Region | h00 | h01 | h02 | h03 | h04 | h05 | h06 | h07 | h08 | h09 | h10 | h11 | h12 | h13 | h14 | h15 | h16 | h17 | h18 | h19 | h20 | h21 | h22 | h23 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ams3 | ✓ 33 | ✓ 33 | ✓ 33 | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours |
| tor1 | ✓ 33 | partial 33 | ✓ 33 | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours | ✗ ours |

## Answer classes per network and region

Sums over the measured slots. Rate-limited requests are not failures and do not count toward a country's sample; duplicates, other-country exits and no-IP answers count toward the 50% stop rule; real errors are the rest.

| Network | Region | Requests | Rate-limited | Answered | Real errors | Duplicates | Other-country | No-IP |
|---|---|---|---|---|---|---|---|---|
| geonode | ams3 | 96 | 0 | 96 | 24 | 0 | 0 | 0 |
| geonode | tor1 | 24 | 0 | 24 | 6 | 0 | 0 | 0 |
| shifter | ams3 | 108 | 0 | 108 | 36 | 0 | 0 | 0 |
| shifter | tor1 | 21 | 0 | 21 | 7 | 0 | 0 | 0 |
