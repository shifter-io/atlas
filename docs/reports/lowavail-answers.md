# Countries where a network has almost no exits: what the networks answer there

LOWAVAIL-1 (issue #1181), step 1 of 5. A read-only look at the data, before anything changes.

## In short

- Every network has between 11 and 40 countries where the October census found fewer than
  10 IPs at its best hour: **401 network-country pairs** in all.
- In the first day of daily tests (3,232 tests), **509 tests found fewer than 10 IPs**.
  What the network answered there falls into three kinds:
  1. **268 tests: the network's own "no exit" answer.** Most networks reply with one or two
     status codes of their own when they have no exit in a country (Rayobyte 551, Massive 470 and
     471, Webshare 502 and 422, ...). These codes are almost never seen in the big countries, so
     they are a safe sign. They are now written down per network (the list below).
  2. **77 tests: almost no failures at all.** The network answers every request, but always
     from the same handful of exits. A broken network never looks like this.
  3. **158 tests: mostly timeouts.** Some networks (FleetProxy, Geonode, Infatica, IPRoyal,
     ProxyEmpire, Proxy-Seller) often give no answer in a small country and let the request wait
     until it times out. The approved rule says timeouts never count as "no exit", because a
     network that is down also times out. **Question 1 below asks what to do with these.**
- Shifter's 15:00 daily test of 15 November is the clearest real case: 68 countries, 45 of them
  tiny places (Wallis and Futuna, the Marshall Islands, Tonga, Kiribati, Vanuatu, Bouvet Island),
  where its gateway answers 502 when it has no exit. Its section is below.
- Four networks had no daily test of a small country yet. Nodemaven's and Soax's came at 17:00
  (their lists are below); Byteful's small countries are tested at 18:00 and 19:00, and
  Proxy-Cheap tests a few countries a day.
- The safeguards matter: on 15 November Infatica answered 407 (a login error) on every request in
  Mexico, Puerto Rico, Panama, Bolivia and the Bahamas, and four networks timed out in Iran, where
  the census found over 1,000 IPs. Those are real problems and must stay visible: 407 is never a
  "no exit" answer, and a normally big country becomes low availability only after two days.

## The data

- **Daily tests:** every daily test stored in production from 14 November 18:00 to 15 November
  16:07 UTC (3,232 tests, the first day of the daily test), read in a read-only transaction. For
  each test: its network, country, unique IPs, and how its requests ended (status codes and
  errors).
- **October census:** the per-country hourly unique IPs in the formulas (`formulas/<network>.json`);
  a country's peak is its best hour. A country the census found no exit in is not in the formulas.
- The previous database's daily tests (8 and 9 November) are a simulation (every request a 200 or
  a timeout) and were not used.
- **Small country:** census peak under 10, or a daily test under 10. **Big country:** census peak
  and daily test both 50 or more. A failed answer is a status of 400 or more, or an error without
  a status (timeout, refused or dropped connection, TLS, protocol).

Refresh the tables with (read only):

```
python -m atlas.availability --database-file <database.toml> --formulas formulas [--since <ISO time>]
```

### Countries per network

"Census under 10": countries whose October census peak is under 10. "Tested": countries with a
daily test. "Daily under 10": countries with a daily test under 10 IPs. "0 IPs": a daily test found
none. "Big, daily under 10": the census found 50 or more, a daily test under 10 (the countries the
two-days-in-a-row rule protects).

| Network | Census under 10 | Tested | Daily under 10 | 0 IPs | Big, daily under 10 |
|---|---:|---:|---:|---:|---:|
| byteful | 13 | 76 | 0 | 0 | 0 |
| dataimpulse | 26 | 204 | 52 | 25 | 6 |
| decodo | 24 | 221 | 34 | 11 | 0 |
| evomi | 30 | 223 | 72 | 28 | 24 |
| fleetproxy | 19 | 208 | 38 | 12 | 5 |
| geonode | 11 | 225 | 50 | 19 | 10 |
| infatica | 25 | 214 | 43 | 15 | 7 |
| iproyal | 18 | 213 | 26 | 9 | 3 |
| massive | 13 | 128 | 17 | 4 | 0 |
| nodemaven | 16 | 157 | 0 | 0 | 0 |
| oxylabs | 24 | 221 | 31 | 11 | 0 |
| proxy-cheap | 40 | 13 | 2 | 2 | 2 |
| proxy-seller | 24 | 216 | 37 | 12 | 1 |
| proxyempire | 17 | 211 | 36 | 16 | 4 |
| rayobyte | 23 | 214 | 33 | 9 | 0 |
| shifter | 19 | 148 | 0 | 0 | 0 |
| soax | 35 | 132 | 4 | 0 | 4 |
| webshare | 24 | 208 | 34 | 16 | 3 |

Evomi stands out: 24 countries that the census found 50 or more IPs in (Luxembourg 400, Syria 804)
found under 10 on 14-15 November, mostly while answering normally. Either Evomi has fewer exits
there now, or the test meets the same few exits again and again. The two-days rule keeps them
normal countries until it is seen twice.

### What the networks answer in small countries

Each answer: its share of all requests in the small countries / its share in the big countries.
An answer that is common in small countries and almost absent in big ones is the network's
"no exit" answer. The last column is how much of the small countries' failures the list explains.

| Network | Small tests | Failed answers, small / big (share of requests) | No-exit list | Its share of small failures |
|---|---:|---|---|---:|
| byteful | 0 | - | - | - |
| dataimpulse | 52 | 503 29.5% / 0.0%, 502 23.8% / 0.6%, connection 7.9% / 0.4%, timeout 3.7% / 0.3% | 502, 503 | 82.1% |
| decodo | 34 | 522 30.5% / 0.1%, 502 23.8% / 0.2%, timeout 0.5% / 0.1%, 403 0.2% / 0.2% | 502, 522 | 98.6% |
| evomi | 72 | 409 39.0% / 0.0%, 500 17.1% / 0.0%, timeout 0.8% / 0.5%, 502 0.1% / 0.0% | 409, 500 | 98.5% |
| fleetproxy | 38 | timeout 43.5% / 6.0%, 502 23.3% / 0.0%, connection 1.4% / 0.3%, tls 0.4% / 0.1% | 502 | 33.9% |
| geonode | 50 | timeout 21.7% / 0.3%, 500 2.4% / 0.1%, protocol 0.3% / 0.1%, connection 0.1% / 0.1% | 465 | 0.0% |
| infatica | 43 | timeout 36.7% / 3.1%, 503 16.2% / 0.0%, 407 11.6% / 2.7%, connection 0.8% / 0.2% | 503 | 24.7% |
| iproyal | 27 | 504 25.1% / 0.4%, timeout 21.6% / 0.6%, connection 18.0% / 0.4%, 503 7.5% / 0.0% | 503 | 10.3% |
| massive | 17 | 470 61.6% / 0.0%, 471 24.7% / 0.0% | 470, 471 | 100.0% |
| nodemaven | 0 | - | - | - |
| oxylabs | 31 | 522 35.3% / 0.1%, 502 29.5% / 0.2%, timeout 0.3% / 0.2% | 502, 522 | 99.5% |
| proxy-cheap | 2 | timeout 100.0% / 36.9% | - | 0.0% |
| proxy-seller | 37 | 503 28.3% / 2.4%, timeout 25.0% / 1.6% | 503 | 53.0% |
| proxyempire | 36 | timeout 42.1% / 2.6%, 502 35.6% / 0.0%, connection 0.4% / 0.3%, protocol 0.0% / 0.0% | 502 | 45.5% |
| rayobyte | 34 | 551 89.3% / 3.4%, timeout 4.9% / 3.7%, 504 0.1% / 2.1%, connection 0.1% / 0.0% | 551 | 94.6% |
| shifter | 0 | - | - | - |
| soax | 4 | timeout 5.9% / 0.2%, connection 4.6% / 0.1%, protocol 0.7% / 0.0%, 502 0.2% / 0.0% | - | 0.0% |
| webshare | 35 | 502 70.2% / 0.8%, 422 20.1% / 0.0%, timeout 2.2% / 0.2%, protocol 0.0% / 0.0% | 422, 502 | 97.6% |

### The "no exit" answers per network

They live in each network's grammar (`src/atlas/providers/grammars/<network>.toml`, `[testing]
no_inventory_statuses`), where Geonode's 465 already was. The daily test already counts these
answers separately (`no_inventory` in a test's summary), so step 2 can read them from every
new test. Adding them changes no score, no stored result and nothing on the website.

| Network | "No exit" answers | Notes |
|---|---|---|
| Dataimpulse | 502, 503 | |
| Decodo | 502, 522 | |
| Evomi | 409, 500 | |
| FleetProxy | 502 | also timeouts (Question 1) |
| Geonode | 465 | 465 is its answer with strict country matching, which the daily test does not ask for; otherwise timeouts (Question 1) |
| Infatica | 503 | also timeouts (Question 1); its 407 is a login error, never "no exit" |
| IPRoyal | 503 | also 504 ("the exit did not answer in time"), timeouts and dropped connections (Question 1) |
| Massive | 470, 471 | |
| Nodemaven | 406, 503 | from its 17:00 daily test: 406 29.6% and 503 27.4% of requests in small countries, 0.05% and 0.15% in big ones; also timeouts (Question 1) |
| Oxylabs | 502, 522 | |
| Proxy-Seller | 503 | also timeouts (Question 1) |
| ProxyEmpire | 502 | also timeouts (Question 1) |
| Rayobyte | 551 | |
| Shifter | 502 | from its 15:00 daily test of 15 November (section below) |
| Soax | 502 | from its 17:00 daily test: 30.4% of requests in small countries, 0.02% in big ones |
| Webshare | 422, 502 | |
| Byteful, Proxy-Cheap | not yet | no daily test of a small country stored yet |

### Shifter's 15:00 daily test of 15 November

At 15:00 the daily test measured Shifter in 68 countries at once, 45 of them tiny places. The
October census found 0 to 11 Shifter IPs in each (Wallis and Futuna 1, Bouvet Island 2, the
Marshall Islands 3, Kiribati 5, Vanuatu 8, Tonga 11). The operator's report on that hour, by
worker region:

| Worker region | Countries | Requests | Successful | HTTP 502 | Exit IPs found |
|---|---:|---:|---:|---:|---:|
| Toronto | 1 (Greenland) | 2,999 | 15.0% | 49.0% | 1 |
| Sydney | 14 Pacific islands | 41,861 | 40.4% | 45.5% | 57 |
| Singapore | 4 | 11,990 | 75.8% | 13.3% | 51 |
| Frankfurt | 10 small European and African countries | 29,864 | 68.4% | 10.4% | 233 |
| Bangalore | 13 small African and Indian Ocean countries | 38,952 | 75.0% | 10.2% | 584 |
| London | 12 small European and West African countries | 35,687 | 84.2% | 3.7% | 272 |
| New York | 14 Caribbean countries | 46,798 | 97.6% | 0.4% | 4,917 |

About 15% of the hour's requests were 502, against 0.013% in Shifter's big countries on 14-15
November. A direct test from five regions got 502 on every request for Wallis and Futuna, the
Marshall Islands and Tonga (no exit), and one single exit for Kiribati and Vanuatu (502 while it
is busy): the answer depends on the place asked for, not the region. So Shifter's "no exit"
answer is 502. The hour's results were taken out of the database at 16:02 and are being put back
from the archive: the data stays as measured, and the low-availability rule is what treats it
(the same for every network). Their per-country rows join the tables and the tests once restored.

### How a small test's failures split

With these lists, the 509 tests under 10 IPs:

| Kind | Tests | What step 2 will do |
|---|---:|---|
| Mostly "no exit" answers | 268 | low availability (no exits at 0 IPs) |
| Almost no failures (under 10% of requests) | 77 | low availability: the network answers, from few exits |
| Mostly timeouts or dropped connections | 158 | Question 1 |
| Other (Infatica's 407 in Latin America) | 6 | stays visible: a real problem |

The 158 timeout tests: 71 in countries the census found under 10 IPs, 64 in countries of 10 to 49,
and 23 in normally big countries (50 or more; Iran, Niger, Guinea and Fiji among them).

---

**Question 1: Should a small country count as "low availability" when the network lets the request wait until it times out?**

**What it's about:** the approved rule says a country is "low availability" when its daily test
found fewer than 10 IPs and the network's failures are its own "no exit" answer, for example
Rayobyte's 551. Timeouts (no answer at all in time) were left out on purpose, because a network
that is down also times out. The data shows that six networks often give no "no exit" answer
in a small country: they just let the request wait. Example: Ana looks at FleetProxy in Tonga,
where the census found 3 IPs. Nine of ten failed requests there time out, while FleetProxy
answers normally in Germany in the same hour. Under the approved rule Tonga is not "low
availability", so it shows up as a failure in the hour report every day and keeps getting the
hourly checks (pulses). On the first day this was 158 of the 509 small-country tests.

**Your options:**
1. **Timeouts count as "no exit" in countries that are not normally big, while the network
   works elsewhere** (recommended)
   - "Not normally big": the census and the recent daily tests found fewer than 50 IPs there
     (135 of the 158 tests). "Works elsewhere": the network guard sees no network problem in that
     hour, that is, most of the network's big countries are not failing at the same time.
   - Users: nothing changes on the website (these countries already do not count in scores).
   - Operators: about 135 fewer false failures a day in the hour report, and no pulses there.
   - Cost: fewer requests, a little cheaper.
   - Risk: a real outage limited to one small country is not shown as a failure. It already does
     not count in the scores. A normally big country that starts timing out stays visible.
2. **Keep the approved rule: timeouts never count as "no exit"**
   - Nothing can be hidden by a timeout.
   - About 158 small-country tests a day stay as failures in the hour report, and those countries
     keep their pulses (more requests, more noise that hides the real failures).
3. **Timeouts count as "no exit" everywhere, with the two-days rule and the network guard**
   - The fewest false failures.
   - Risk: a normally big country whose exits stop answering for two days becomes "low
     availability" and is no longer shown. On 15 November four networks timed out in Iran, where
     the census found over 1,000 IPs: with this option that would be hidden after two days.

**Why we recommend 1:** it removes the daily noise exactly where the data says these are tiny
places, and every normally big country stays visible, as the two-days rule intended. Option 2
keeps that noise and the pulses' cost every day; option 3 can hide a real outage in a big
country.

**Hard to undo:** nothing: it is one rule in the code and can be changed later. Past hour reports
keep what they said.

**If you don't decide:** step 2 builds the approved rule (option 2); option 1 can be added later.

---

**Your answer:** one line, for example `Q1: 1`.
