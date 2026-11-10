# Phase 3 report: every Proxy Index page from the API, fast at production volume (staging)

**Status: not closed.** Three misses wait for their fixes:
- the coverage search's first render: #797;
- 46 map gaps on staging: #798 and #799;
- the browser scores missing from v4 builds: #800 (URGENT SCORES-V4-1).

They are in sections 2, 4, 6 and 9. Under the issue's rule ("no miss at close") this report is re-measured after those fixes
merge, and closes only when every row passes.

- **Code:** `main` at 0b0acf9 (2026-11-10), with #787, #788, #789 and #790 merged. Every other
  issue of the milestone is merged or closed, apart from the fix issues of section 9.
  SIM-MODEL-3 (#793) merged as PR #796 after this run's code was taken. It changes only the
  simulator (`scripts/scale/`), not the calculation or the API, and the stored week was
  generated before it.
- **Staging only:** every step ran on #718's staging host against the staging database
  (`proxy_reviews_staging`), with credentials files that name it. No step connected to the
  production database or changed a production service, setting, firewall or credential.
- **Steps, 2026-11-10 UTC:**
  1. `proxy-index db-migrate`: v14 to v15 (#719's `test_events` indexes) and calculated v13 to v14
     (#789's stored list counts and API head), 3.4 s; `siteapi migrate`: already at site
     migration 0003.
     - The migration waited from 01:23 to 03:16. A `pg_dump` session of the database
       superuser held a read lock on every staging table, and the migration needs one table
       for itself for a moment. The migration was retried with a 30 s lock timeout every 5
       minutes, so it never queued in front of other readers. The dump was not touched.
  2. `calculate --incremental --publish` at as-of 2026-11-09 03:00, the first build on the new
     code (build 4, "after a deploy").
  3. The staging siteapi from 0b0acf9, on a local port only, `MemoryMax` 5 GB, as `proxy_api`.
  4. `siteapi explain` as `proxy_api` on build 4.
  5. `siteapi bench` with `--gaps docs/api-web-map-gaps.json`, on build 4.
  6. `calculate --incremental --publish` at as-of 04:00 (the hourly calculation) and the
     siteapi's rebuild for it.
     - `simulate.py` cannot store another hour: its state holds the 7 days and one extra hour.
       So the hour is the as-of step, as #718 did.
  7. The staging siteapi is stopped after the measurements.
- **Not run:** the 200 MB download test. It waits for the owner's send-buffer change on the
  database server (section 1, decision 5).
- **The data's limit:**
  - **Total volume is production's:** 26,936 daily tests, about 192M observation rows, 91.4 GB.
    Every speed figure in this report holds for that total.
  - **The spread over the 18 networks is not:** per network, daily rows run from about −83% to
    +1,094% of the real reference (#718's checks, #784). A network's own numbers may differ
    from what that network would produce in production.
  - **Remaining model gaps (#784's re-check: 231 of 233 statistics and 494 of 542 size checks
    pass):**
    - **Daily tests:** 16% to 70% more observation rows per IP than the real reference, on
      every network; 40% to 70% on 16 of 18.
    - **IP enrichment:** about twice the rows from day 2 on. The generator used a new DB-IP
      release every day; production uses one a month.
    - **Buckets per run:** off by −17% to +25% on 8 networks.
    - **Browser bytes per page load:** up to 40% high, mostly the ChatGPT target.
    - **proxy-cheap's places:** its cities and ASNs differ a little from the reference.
  - **So the speed figures are on the cautious side:** the observation and enrichment tables,
    the largest, are bigger than production's would be for the same tests. SIM-MODEL-3 (#793,
    merged) fixes the model for a future load.

## 1. For the owner

This part is written for someone who has never seen the project. A few words first:

- **The API** is the program that hands the website its numbers. Each question the website asks it
  (for example "give me the network page of Oxylabs for the last 30 days") is an **endpoint**.
- **Staging** is our test copy of the system. It has its own database and its own copy of the
  API, and nothing in it touches the real (production) system that visitors use. Every number
  in this report comes from staging.
- **The test data:** we generated 7 days of data with the same total size as a real week:
  26,936 daily tests of 18 proxy networks, about 192 million rows, 91 GB.
- **A budget** is the time limit we set in advance for each answer. We never raised a limit to
  make an answer pass.
- **p99** means "99 of 100 requests were at least this fast". We sent every request 200 times.
- **ms** is a millisecond, a thousandth of a second. A blink takes about 100 ms.
- **EXPLAIN** is the database's own report of how it found the rows for one question and how
  long that took. We ran it for every question the API asks the database.
- **An index** is like the index at the back of a book: it lets the database find rows without
  reading everything.
- **A migration** is a change to how the database stores its data, applied once.
- **The calculation** is the hourly job that turns the raw test results into the numbers the
  website shows; **the rebuild** is the API reading that result into memory.

### What works and is fast

Almost everything is fast, measured on staging with a full-size week. When Ana opens the
Oxylabs page, each of its 40 answers takes the API under 1 ms to send,
and the 40 together add up to about 7 ms (the limit is 10). Every other page (home, targets, networks, compare,
overlap, true cost, live tape) gets all its answers in under 5 ms in total. Every question the API asks the database finishes within 36 ms, against a
limit of 100 ms. When new numbers arrive, the API is ready with them in 29 seconds (the limit is
60; it took 95 before this phase's fixes). Turning a week of raw results into website numbers
takes about 29 minutes after a code change, and about 11 minutes each hour after that (the
limit is 50 minutes).

### What is not fast or not finished

Three things are not finished, and this report stays open until they are:

1. **The coverage search.** The first time Ben searches a big network's list of cities and
   internet providers right after an hourly update, he waits about half a second to one second.
   The limit is 0.07 seconds. Every later search is instant. The fix is planned: the API will prepare these lists before it
   switches to new numbers.
2. **Empty fields.** For 46 of the 160 website needs that the API now answers, some answers
   leave a value empty without saying why, which the website needs in order to show "N/A"
   properly. 29 of them are empty only because the test copy had no price list, reviews or
   target descriptions loaded. Prices, trials, review labels and target categories are
   catalog text that you write; until you do, the API will say "not filled in yet" instead
   of leaving the field silently empty. The other 17 are missing data in the 24-hour view, such as
   uptime when a network had no test that day.
3. **The new browser scores do not reach the website.** The 1-to-10 scores are calculated, but
   the newest way the API reads results skips them, so every score would show "N/A". The fix is
   planned and urgent. Even after it, many scores for the last 24 hours will honestly show "not
   enough data": we test each network on each website once a day with 100 tries, and about one
   day in seven a network has no test of a given website within the last 24 hours. The 7-day and
   30-day scores have enough tries.

The test data also has a limit: its total size equals a real week, but the two largest tables
(what each test saw, and where each address is located) hold more rows than production's would
for the same tests, because the data generator made more rows per address than real tests do.
So the speed figures are on the cautious side: production should be as fast or faster.

### What the website must change

The website still shows numbers from files it carries itself ("mock data"). Before production it
must:

1. Read every number from the API instead of its own files, page by page (section 5 lists which
   answer each page reads).
2. Turn off the demo networks that the website adds for display.
3. Show "N/A" (not available) instead of an empty space, "0.0" or "Grade null" when a number is
   missing.
4. Show a short sentence instead of an empty box when a list has nothing in it.
5. Replace the word "Live" with the age of the data ("updated 12 minutes ago"), and check every
   few seconds whether newer numbers exist.
6. Send "Download the dataset" to the API's own download.
7. Look up the text of each test target by its fixed name, not by its English title, so
   translations keep working when a title changes.
8. On the compare and targets pages, only call a network "best" against networks measured with
   the same browser (Chrome or Camoufox, the two browsers we test with).
9. Build the list of network pages, the footer and the sitemap from the API, so a new network
   appears without a website change.
10. Show the new 1-to-10 browser scores next to the percentages it shows today.

### Decisions before production

Answer each question with its number and an option, all in one line (an example is at the end).

**1. Should a network's headline score follow the time range the visitor picks?**
(a) What it is about: Ana opens the Oxylabs page and switches the range from "30 days" to "24
hours". Today the charts change, but the big score at the top, its grade and its rank stay the
30-day ones, with no label saying so. Ben, comparing two networks, may think the score is for the
last 24 hours.
(b) What happens with each option:
- Keep the 30-day score and label it "30 days": visitors see a steady score with an honest label.
  No cost, no risk, no change to the API (it already gives every range).
- Let the score follow the range: the score jumps with the range. For a 24-hour range many browser
  scores are missing: in our test week, in about 1 of 7 cases a network had no browser test of a
  target in the last 24 hours (section 6), so its score would show "N/A".
(c) Options:
1. Keep the 30-day score, grade and rank, and label them "30 days" (recommended).
2. Let them follow the chosen range.
(d) Why 1: now, the headline is never empty and never misleading; later, if we test more often,
we can switch to option 2 without any API change. Option 2 shows the newest picture but leaves
gaps and jumps in the most visible number.
(e) Hard to undo: nothing; both are website choices that can change at any release.
(f) If you do not decide: the website keeps the 30-day score without a label, which is today's
confusing state.

**2. Should every network card show "cost per 1,000 successful requests"?**
(a) What it is about: the networks page's questions-and-answers section promises Carla that each
card shows what 1,000 requests that actually work cost, but the cards do not show it. The API
already has the number.
(b) What happens with each option:
- Add the figure to each card: the promise becomes true; the website changes one card template. No
  cost, no risk.
- Reword the two answers instead: no new figure; the page is consistent but less useful for
  comparing prices.
(c) Options:
1. Add the figure to every card (recommended).
2. Reword answers 3 and 4 of that section.
(d) Why 1: now, it is the page's own argument (price per working request, not per gigabyte); later,
the same figure feeds the true-cost page. Option 2 hides a number we already measure.
(e) Hard to undo: nothing.
(f) If you do not decide: the page keeps promising a figure it does not show.

**3. Who writes the missing catalog text, and when?**
(a) What it is about: two pieces of text are content only you can write. Some browser test targets
have no category or difficulty in our catalog (the API then uses the target's scoring weight as
its difficulty). And Shifter's free-trial sentence and order link are typed into the website
itself, so Dan, who manages the website, must change code to update them.
(b) What happens with each option:
- You (or someone you name) write the rows before production: the pages show proper categories
  and Shifter's trial from the catalog; about an hour of work, no cost, no risk.
- Launch without them: the pages work, with a default difficulty and no category for those
  targets, and Shifter's text stays in the website.
(c) Options:
1. Write the catalog rows before production (recommended).
2. Launch without them and add them later.
(d) Why 1: now, every target card is complete on day one; later, every catalog change is a data
change, not a website release. Option 2 launches sooner by about an hour, with thinner cards.
(e) Hard to undo: nothing; rows can be added or changed at any time.
(f) If you do not decide: option 2 happens by default.

**4. Do we keep the 91 GB of test data on the shared database server?**
(a) What it is about: the 7 test days live on the same database server as production, in a
separate database. Elena, who watches that server's disk, sees 91 GB used by tests.
(b) What happens with each option:
- Keep it until the production move is done: we can re-run any measurement and the planned
  re-load with the corrected data model (section 9) without generating everything again (the load took
  about 10 hours). It uses 91 GB of disk; no effect on visitors.
- Delete it now: frees 91 GB at once; any new measurement needs a new 7-day load first.
(c) Options:
1. Keep it until production runs on this code, then delete it (recommended).
2. Delete it now.
(d) Why 1: now, the corrected re-load and any re-check stay cheap; later, we delete it in one
step. Option 2 saves disk now but costs about 10 hours of loading for the next check.
(e) Hard to undo: deleting is final; the data can only be generated again.
(f) If you do not decide: we keep it (option 1) and ask again when production is moved.

**5. Should the database server's network buffers be raised?**
(a) What it is about: the database server sends data to each connection at most at about 15
megabytes a second, because of a small send buffer (the memory the server's operating system
uses to send data over the network). Our hourly calculation runs on a different machine and
downloads a lot of rows, so this limit slows it down. A planned test that downloads 200 MB over
one connection waits for this change, so it is not in this report.
(b) What happens with each option:
- Raise the buffers: each connection can download faster, so the calculation and the rebuild
  finish sooner, also in production. The change is one operating-system setting, done in a
  quiet minute; no cost. Small risk: the server uses a little more memory per connection.
- Leave them: the calculation and the API's rebuild stay within their time limits; they keep
  their current speed.
(c) Options:
1. Raise them, then we run the 200 MB test and add it to this report (recommended).
2. Leave them as they are.
(d) Why 1: now, it is the one test still missing; later, production's calculation gets the same
headroom as data grows. Option 2 keeps the server untouched but leaves the test undone.
(e) Hard to undo: nothing; the setting can be set back at once.
(f) If you do not decide: nothing changes and the 200 MB test stays undone.

**Answer in one line**, for example: `1: 1, 2: 1, 3: 2, 4: 1, 5: 1`.

## 2. Every endpoint against its budget

### 2.1 Requests

`siteapi bench` on the staging host against the staging siteapi over loopback, on build 4
(15 s after it was served), with 8 workers (the bench's default); section 2.1's
server-time verdict comes from a second run at concurrency 1:
- every distinct request once cold (the first request since the build appeared), once without
  compression for its size, then 200 times warm over 8 workers with brotli;
- 327 requests on 27 pages, grouped below as 55 route and query shapes;
- a shape's figures are the worst over its requests, and "(N networks)" means it was requested
  for every network's slug.

**The budgets** in this run are the bench's default, `bench.DefaultBudgets()`: the strict staging
budgets, `bench.StagingBudgets()` (#716, #754), plus `NetworkMs`, 20 ms of network time per
request. The verdict uses the strict budgets on server time (below).

| Class | Requests | Cold ms | p99 ms |
|---|---|--:|--:|
| `answer` | a precomputed answer | 22 | 22 |
| `events` | `/v1/events` | 22 | 22 |
| `search` | `/v1/search?q=` | 25 | 25 |
| `list` | a long list's first render | 70 | 22 |
| `list-search` | the same list with `q=` | 70 | 70 |

**The Statements column** names the SQL behind a request, from section 2.2:
- **B**: the build's load statements. A precomputed answer reads no database at request time;
  it is built once per build from them.
- **R**: the list-row and label reads of a row-cache miss.
- **E**: the `/v1/events` database fallback.

| Route and query shape | Class | Requests | Cold ms (max) | Warm p50 ms (max) | Warm p99 ms (max) | Bytes identity (max) | Bytes brotli (max) | Budget cold / p99 ms | Statements | Pass |
|---|---|--:|--:|--:|--:|--:|--:|--:|---|---|
| `/v1/rankings?range=24h` | answer | 1 | 0.42 | 0.14 | 0.91 | 32,683 | 4,639 | 22 / 22 | B | pass |
| `/v1/rankings?range=7d` | answer | 1 | 0.29 | 0.12 | 0.66 | 28,344 | 3,777 | 22 / 22 | B | pass |
| `/v1/rankings?range=30d` | answer | 1 | 0.14 | 0.13 | 0.54 | 32,991 | 4,271 | 22 / 22 | B | pass |
| `/v1/events` | events | 1 | 0.13 | 0.14 | 0.59 | 47,946 | 3,407 | 22 / 22 | B (load.events_*), E (events.query.*) when the window cannot answer | pass |
| `/v1/home` | answer | 1 | 0.10 | 0.14 | 0.67 | 139,603 | 26,300 | 22 / 22 | B | pass |
| `/v1/home?range=24h` | answer | 1 | 0.10 | 0.14 | 0.64 | 94,282 | 17,807 | 22 / 22 | B | pass |
| `/v1/home?range=7d` | answer | 1 | 0.09 | 0.14 | 0.62 | 138,228 | 26,019 | 22 / 22 | B | pass |
| `/v1/home?range=30d` | answer | 1 | 0.12 | 0.12 | 0.61 | 139,603 | 26,300 | 22 / 22 | B | pass |
| `/v1/meta` | answer | 1 | 0.08 | 0.10 | 0.52 | 4,301 | 1,012 | 22 / 22 | B | pass |
| `/v1/targets` | answer | 1 | 0.11 | 0.10 | 0.40 | 88,131 | 4,526 | 22 / 22 | B | pass |
| `/v1/targets?range=24h` | answer | 1 | 0.09 | 0.15 | 0.74 | 79,842 | 3,443 | 22 / 22 | B | pass |
| `/v1/targets?range=7d` | answer | 1 | 0.10 | 0.11 | 0.57 | 87,475 | 4,657 | 22 / 22 | B | pass |
| `/v1/targets?range=30d` | answer | 1 | 0.13 | 0.10 | 0.58 | 88,131 | 4,526 | 22 / 22 | B | pass |
| `/v1/targets?kind=residential&range=24h` | answer | 1 | 0.11 | 0.13 | 0.64 | 79,842 | 3,443 | 22 / 22 | B | pass |
| `/v1/targets?kind=residential&range=7d` | answer | 1 | 0.11 | 0.14 | 0.76 | 87,475 | 4,657 | 22 / 22 | B | pass |
| `/v1/targets?kind=residential&range=30d` | answer | 1 | 0.10 | 0.15 | 0.78 | 88,131 | 4,526 | 22 / 22 | B | pass |
| `/v1/targets?kind=datacenter` | answer | 1 | 0.08 | 0.14 | 0.62 | 3,234 | 697 | 22 / 22 | B | pass |
| `/v1/targets?engine=camoufox&kind=residential&range=24h` | answer | 1 | 0.09 | 0.12 | 0.62 | 79,977 | 3,443 | 22 / 22 | B | pass |
| `/v1/targets?engine=camoufox&kind=residential&range=7d` | answer | 1 | 0.10 | 0.14 | 0.47 | 87,415 | 4,619 | 22 / 22 | B | pass |
| `/v1/targets?engine=camoufox&kind=residential&range=30d` | answer | 1 | 0.10 | 0.13 | 0.58 | 88,100 | 4,523 | 22 / 22 | B | pass |
| `/v1/targets?engine=chrome&kind=residential&range=24h` | answer | 1 | 0.09 | 0.12 | 0.66 | 83,786 | 1,749 | 22 / 22 | B | pass |
| `/v1/targets?engine=chrome&kind=residential&range=7d` | answer | 1 | 0.08 | 0.12 | 0.59 | 83,980 | 1,706 | 22 / 22 | B | pass |
| `/v1/targets?engine=chrome&kind=residential&range=30d` | answer | 1 | 0.11 | 0.14 | 0.61 | 83,998 | 1,721 | 22 / 22 | B | pass |
| `/v1/networks/{slug}?kind=residential` (18 networks) | answer | 18 | 0.50 | 0.15 | 0.82 | 117,486 | 8,033 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/chart?kind=residential&range=24h` (18 networks) | answer | 18 | 0.14 | 0.18 | 1.24 | 54,313 | 3,443 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/chart?kind=residential&range=7d` (18 networks) | answer | 18 | 0.14 | 0.15 | 0.79 | 1,864 | 443 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/chart?kind=residential&range=30d` (18 networks) | answer | 18 | 0.13 | 0.12 | 0.62 | 7,014 | 649 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/coverage?kind=residential&range=24h` (18 networks) | list | 18 | 1.12 | 0.15 | 0.76 | 5,928 | 1,821 | 70 / 22 | B, R | pass |
| `/v1/networks/{slug}/coverage?kind=residential&range=7d` (18 networks) | list | 18 | 1.03 | 0.13 | 0.67 | 5,962 | 1,896 | 70 / 22 | B, R | pass |
| `/v1/networks/{slug}/coverage?kind=residential&range=30d` (18 networks) | list | 18 | 1.29 | 0.18 | 2.44 | 5,964 | 1,890 | 70 / 22 | B, R | pass |
| `/v1/networks/{slug}/coverage?kind=residential&range=24h&q={q}` (18 networks) | list-search | 18 | 540.69 | 0.13 | 0.65 | 5,943 | 2,011 | 70 / 70 | B, R | **fail**: cold over 70 ms for 11 of 18 networks, 380 to 541 ms (within: byteful, dataimpulse, decodo, evomi, geonode, massive, proxy-cheap) |
| `/v1/networks/{slug}/coverage?kind=residential&range=7d&q={q}` (18 networks) | list-search | 18 | 664.54 | 0.13 | 0.59 | 6,010 | 1,647 | 70 / 70 | B, R | **fail**: cold over 70 ms for 15 of 18 networks, 425 to 665 ms (within: byteful, dataimpulse, proxy-cheap) |
| `/v1/networks/{slug}/coverage?kind=residential&range=30d&q={q}` (18 networks) | list-search | 18 | 1071.07 | 0.13 | 0.74 | 6,012 | 1,624 | 70 / 70 | B, R | **fail**: cold over 70 ms for 16 of 18 networks, 531 to 1071 ms (within: byteful, proxy-cheap) |
| `/v1/overlap` | answer | 1 | 0.13 | 0.12 | 0.73 | 32,673 | 5,170 | 22 / 22 | B | pass |
| `/v1/overlap?range=24h` | answer | 1 | 0.10 | 0.13 | 0.70 | 3,922 | 585 | 22 / 22 | B | pass |
| `/v1/overlap?range=7d` | answer | 1 | 0.09 | 0.10 | 0.52 | 3,941 | 594 | 22 / 22 | B | pass |
| `/v1/overlap?range=30d` | answer | 1 | 0.10 | 0.16 | 0.62 | 32,673 | 5,170 | 22 / 22 | B | pass |
| `/v1/overlap?kind=residential&range=24h` | answer | 1 | 0.10 | 0.13 | 0.55 | 3,922 | 585 | 22 / 22 | B | pass |
| `/v1/overlap?kind=residential&range=7d` | answer | 1 | 0.12 | 0.12 | 0.46 | 3,941 | 594 | 22 / 22 | B | pass |
| `/v1/overlap?kind=residential&range=30d` | answer | 1 | 0.10 | 0.17 | 0.56 | 32,673 | 5,170 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/pricing-history` (18 networks) | answer | 18 | 2.68 | 0.18 | 4.02 | 226 | 138 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/pricing-history?kind=residential` (18 networks) | answer | 18 | 0.97 | 0.14 | 0.71 | 227 | 137 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/reviews` (18 networks) | answer | 18 | 2.40 | 0.14 | 0.71 | 214 | 139 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/reviews?page=1&rating=5&verified=run` (18 networks) | answer | 18 | 1.55 | 0.12 | 0.83 | 212 | 142 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/runs` (18 networks) | answer | 18 | 0.88 | 0.12 | 1.02 | 293 | 186 | 22 / 22 | B | pass |
| `/v1/networks/{slug}/runs?kind=residential&page=1&status=accepted` (18 networks) | answer | 18 | 3.46 | 0.13 | 0.77 | 299 | 183 | 22 / 22 | B | pass |
| `/v1/targets?kind=residential` | answer | 1 | 0.10 | 0.11 | 0.59 | 88,131 | 4,526 | 22 / 22 | B | pass |
| `/v1/events?limit=200` | events | 1 | 3.43 | 0.15 | 0.54 | 96,572 | 7,218 | 22 / 22 | B (load.events_*), E (events.query.*) when the window cannot answer | pass |
| `/v1/events/stats?range=24h` | answer | 1 | 0.67 | 0.11 | 0.59 | 10,050 | 651 | 22 / 22 | B (load.event_stats_head, load.event_buckets, load.event_recent) | pass |
| `/v1/events/stats?range=7d` | answer | 1 | 0.88 | 0.11 | 0.45 | 13,075 | 1,161 | 22 / 22 | B (load.event_stats_head, load.event_buckets, load.event_recent) | pass |
| `/v1/events/stats?range=30d` | answer | 1 | 0.82 | 0.11 | 0.62 | 13,637 | 1,054 | 22 / 22 | B (load.event_stats_head, load.event_buckets, load.event_recent) | pass |
| `/v1/events/stats` | answer | 1 | 0.12 | 0.10 | 0.40 | 13,637 | 1,054 | 22 / 22 | B (load.event_stats_head, load.event_buckets, load.event_recent) | pass |
| `/v1/search-index` | answer | 1 | 0.11 | 0.11 | 0.45 | 12,081 | 1,915 | 22 / 22 | B | pass |
| `/v1/search?q={q}` | search | 1 | 0.44 | 0.10 | 0.44 | 369 | 210 | 25 / 25 | B | pass |
| `/v1/version` | answer | 1 | 0.10 | 0.11 | 0.48 | 122 | 104 | 22 / 22 | B | pass |

**Result: 52 of 55 shapes pass; 3 fail.**
- **What fails:** the coverage list searched with `q=` on its first render (cold) is over its 70 ms
  budget for 42 of its 54 requests: 380 to 1,071 ms. The same requests warm take at most 0.74
  ms at p99. The coverage list without `q=` and every other list request pass, cold included.
- **Why:** a first search reads and scans the whole City × ASN list. For the largest network that
  is 145,030 rows, `netlist.rows` 35.5 ms on the database server, then sent over the 9 ms link
  to the siteapi.
- **Fix:** #797 (COVERAGE-SEARCH-1). Before a new version is swapped in, the siteapi warms
  the rows of every searchable list into its row cache, while the old version serves. The
  target is a cold `q=` within 50 ms with the rebuild still at most 60 s. This row is measured
  again after it merges.

**Server time, the verdict's measurement** (the architect's decision): the table's strict
budgets are server time.
- **The 8-worker run cannot show server time.** It gives client time with 8 workers contending
  on one host, which is only an upper bound on server time.
- **Its +20 ms per request is unused over loopback.** It is meant for a real network, so
  passing against it would raise the budget in effect.
- **So the whole bench ran again at concurrency 1.** Over loopback, warm, 200 repeats per
  request, on build 5 (the hourly build, 04:21 UTC, Shifter's catalog loaded): client time
  is then server time plus about 0.1 ms.
- **The verdict uses the concurrency-1 run** against the strict server budgets: p99 at most
  2 ms for an answer, events or a cached list page; 5 ms for search; 50 ms for a list search.
  The cold first render is judged as above.

| Class | Requests | 8 workers: max p99 ms | Over strict | 1 worker (server time): max p50 ms | 1 worker: max p99 ms | Over strict |
|---|--:|--:|--:|--:|--:|--:|
| `answer` | 216 | 4.02 (`/v1/networks/webshare/pricing-history`) | 1 | 0.11 | 0.87 (`/v1/meta`) | 0 |
| `events` | 2 | 0.59 | 0 | 0.09 | 0.22 | 0 |
| `search` | 1 | 0.44 | 0 | 0.08 | 0.11 | 0 |
| `list` (cached) | 54 | 2.44 (`/v1/networks/iproyal/coverage?range=30d`) | 1 | 0.09 | 0.68 | 0 |
| `list-search` (cached) | 54 | 0.74 | 0 | 0.09 | 0.20 | 0 |

**Result on server time: 327 of 327 requests are within their strict p99 budget.**
- **The two 8-worker outliers:** at concurrency 1, `/v1/networks/webshare/pricing-history`
  takes 0.11 ms at p99 (4.02 ms with 8 workers) and `/v1/networks/iproyal/coverage?range=30d`
  0.15 ms (2.44 ms).
- **Why they were slow with 8 workers:** contention, not cutting. `pricing-history` is cut
  from the build's held data on its first request, then kept in the version's page cache, an
  LRU shared with the list pages.
- **The cold coverage search stays the one request miss:** 919 ms at most in this run too.

### 2.2 Every SQL statement (`siteapi explain`)

`siteapi explain` as `proxy_api`, build 4, after the v15 migration:
- every one of the API's 45 statement shapes under `EXPLAIN (ANALYZE, BUFFERS)`, in one
  read-only transaction;
- the arguments come from the build: decodo's network list 35430 with 145,030 rows, largest
  dimension City × ASN; the largest place, in India; the events cursor at 2026-11-08 08:00;
- "Before v15" is #719's measurement on the same data before the migration (PR #786), the
  execution time in ms;
- the budget is at most 100 ms execution.

| Statement | Call site | Feeds | Planning ms | Execution ms | Rows | Shared hit | Shared read | Top plan node | Before v15 (ms, #786) | Pass (100 ms) |
|---|---|---|--:|--:|--:|--:|--:|---|--:|---|
| `netlist.rows` | `NetLists.read (scanList)` | R: list pages, B: the rebuild | 0.09 | 35.52 | 85,940 | 1,480 | 0 | Index Scan on calculated_list_rows_pkey | 34.18 | pass |
| `events.query.proxy_kind` | `QueryEvents` | E: `/v1/events` filters | 0.19 | 14.76 | 100 | 5,381 | 206 | Sort | 12.30 | pass |
| `labels.places` | `labels.readLabels` | R: list pages, B: the rebuild | 8.75 | 9.05 | 40,904 | 819 | 0 | Index Scan on place_names_pkey | 8.81 | pass |
| `labels.networks` | `labels.readLabels` | R: list pages, B: the rebuild | 2.14 | 6.78 | 13,296 | 333 | 0 | Hash | 6.70 | pass |
| `events.query.product` | `QueryEvents` | E: `/v1/events` filters | 0.28 | 4.02 | 100 | 55 | 272 | Sort | 0.95 | pass |
| `load.events_newest` | `Inputs.loadEvents` | B: events window and stats | 0.26 | 2.00 | 2,001 | 774 | 0 | Incremental Sort | 1.94 | pass |
| `events.query.q` | `QueryEvents` | E: `/v1/events` filters | 0.20 | 1.95 | 2,000 | 774 | 0 | Incremental Sort | 2.03 | pass |
| `load.event_buckets` | `Inputs.loadEventStats` | B: events window and stats | 0.26 | 1.35 | 5,535 | 52 | 0 | Hash | 1.22 | pass |
| `events.query.network` | `QueryEvents` | E: `/v1/events` filters | 0.21 | 1.08 | 100 | 327 | 0 | Sort | 1.32 | pass |
| `poll.fingerprint` | `readFingerprint` | every answer (the change poll) | 2.46 | 0.46 | 1 | 14 | 0 | Aggregate | 0.20 | pass |
| `load.event_recent` | `Inputs.loadEventStats` | B: events window and stats | 0.30 | 0.45 | 432 | 215 | 0 | Limit | 0.38 | pass |
| `poll.site_layout` | `ReadSiteLayout` | every answer (the change poll) | 0.17 | 0.27 | 223 | 112 | 0 | Index Scan on pg_attribute_relid_attnum_index | 0.43 | pass |
| `events.query.default` | `QueryEvents` | E: `/v1/events` filters | 0.15 | 0.18 | 100 | 90 | 0 | Incremental Sort | 0.18 | pass |
| `events.query.since` | `QueryEvents` | E: `/v1/events` filters | 0.14 | 0.17 | 100 | 90 | 0 | Incremental Sort | 0.16 | pass |
| `events.query.after` | `QueryEvents` | E: `/v1/events` filters | 0.21 | 0.16 | 101 | 58 | 0 | Nested Loop | 0.18 | pass |
| `load.events_late` | `Inputs.loadEvents` | B: events window and stats | 0.29 | 0.13 | 0 | 12 | 0 | Nested Loop | 0.07 | pass |
| `load.network_counts` | `NetLists.counts` | B: every precomputed answer | 0.04 | 0.06 | 54 | 31 | 0 | Index Scan on calculated_lists_pkey | 514.57 | pass |
| `events.query.category` | `QueryEvents` | E: `/v1/events` filters | 0.35 | 0.04 | 0 | 1 | 0 | Sort | 1.81 | pass |
| `load.snapshot` | `Inputs.loadSnapshot` | B: every precomputed answer | 0.09 | 0.04 | 1 | 3 | 0 | Aggregate | 269.05 | pass |
| `events.query.kinds` | `QueryEvents` | E: `/v1/events` filters | 0.38 | 0.04 | 0 | 2 | 0 | Nested Loop | 1.52 | pass |
| `events.query.country` | `QueryEvents` | E: `/v1/events` filters | 0.26 | 0.04 | 0 | 2 | 0 | Sort | 1.19 | pass |
| `events.query.network_notable` | `QueryEvents` | E: `/v1/events` filters | 0.39 | 0.03 | 0 | 1 | 0 | Sort | 0.57 | pass |
| `events.query.default_notable` | `QueryEvents` | E: `/v1/events` filters | 0.34 | 0.03 | 0 | 2 | 0 | Nested Loop | 3.92 | pass |
| `load.events_notable` | `Inputs.loadEvents` | B: events window and stats | 0.37 | 0.03 | 0 | 4 | 0 | Nested Loop | 4.02 | pass |
| `load.events_read` | `Inputs.loadEvents` | B: events window and stats | 0.12 | 0.03 | 1 | 5 | 0 | Result | 0.03 | pass |
| `load.site.providers` | `readSite` | B: every precomputed answer | 0.07 | 0.03 | 0 | 0 | 0 | Seq Scan on providers | 0.02 | pass |
| `events.query.target` | `QueryEvents` | E: `/v1/events` filters | 0.20 | 0.03 | 0 | 2 | 0 | Sort | 1.23 | pass |
| `load.framework_targets` | `Inputs.catalogSteps` | B: every precomputed answer | 0.03 | 0.03 | 11 | 1 | 0 | Seq Scan on targets | 0.03 | pass |
| `load.site.changelog` | `readSite` | B: every precomputed answer | 0.07 | 0.03 | 0 | 0 | 0 | Seq Scan on changelog | 0.01 | pass |
| `load.event_stats_head` | `Inputs.loadEventStats` | B: events window and stats | 0.01 | 0.02 | 1 | 3 | 0 | Result | 0.02 | pass |
| `load.site.ranking_profiles` | `readSite` | B: every precomputed answer | 0.03 | 0.02 | 0 | 0 | 0 | Seq Scan on ranking_profiles | 0.02 | pass |
| `load.framework_products` | `Inputs.frameworkSteps` | B: every precomputed answer | 0.17 | 0.02 | 18 | 2 | 0 | Index Scan on products_provider_name_key | 0.01 | pass |
| `load.site.targets` | `readSite` | B: every precomputed answer | 0.04 | 0.02 | 0 | 0 | 0 | Seq Scan on targets | 0.02 | pass |
| `load.site.scopes` | `readSite` | B: every precomputed answer | 0.04 | 0.02 | 0 | 0 | 0 | Seq Scan on scopes | 0.02 | pass |
| `load.framework_providers` | `Inputs.frameworkSteps` | B: every precomputed answer | 0.10 | 0.02 | 18 | 2 | 0 | Index Only Scan on providers_pkey | 0.02 | pass |
| `load.site.provider_products` | `readSite` | B: every precomputed answer | 0.05 | 0.02 | 0 | 0 | 0 | Seq Scan on provider_products | 0.02 | pass |
| `load.site.reviews` | `readSite` | B: every precomputed answer | 0.04 | 0.02 | 0 | 0 | 0 | Seq Scan on reviews | 0.02 | pass |
| `load.site.community_runs` | `readSite` | B: every precomputed answer | 0.04 | 0.01 | 0 | 0 | 0 | Seq Scan on community_runs | 0.02 | pass |
| `load.now` | `Inputs.loadHead` | B: every precomputed answer | 0.01 | 0.01 | 1 | 0 | 0 | Result | 0.01 | pass |
| `load.site.pricing_history` | `readSite` | B: every precomputed answer | 0.07 | 0.01 | 0 | 1 | 0 | Index Scan on pricing_history_pkey | 0.01 | pass |
| `load.site.pricing` | `readSite` | B: every precomputed answer | 0.08 | 0.01 | 0 | 1 | 0 | Index Scan on pricing_pkey | 0.01 | pass |
| `load.site.tape_events` | `readSite` | B: every precomputed answer | 0.03 | 0.01 | 0 | 0 | 0 | Seq Scan on tape_events | 0.02 | pass |
| `load.site.score_weights` | `readSite` | B: every precomputed answer | 0.03 | 0.01 | 0 | 1 | 0 | Index Scan on score_weights_pkey | 0.01 | pass |
| `load.site.trust_signals` | `readSite` | B: every precomputed answer | 0.03 | 0.01 | 0 | 1 | 0 | Index Scan on trust_signals_pkey | 0.01 | pass |
| `load.site.pricing_plans` | `readSite` | B: every precomputed answer | 0.04 | 0.01 | 0 | 1 | 0 | Index Scan on pricing_plans_pkey | 0.01 | pass |

**Result: 45 of 45 statements pass.**
- **The slowest:** `netlist.rows` at 35.5 ms, a network list's rows on a row-cache miss.
- **#789's fixes:** the two statements over 100 ms before, `load.network_counts` (514.6 ms) and
  `load.snapshot` (269.1 ms), now take 0.06 ms and 0.04 ms. They read the values the
  calculation stores.
- **#719's partial indexes:** the notable-event reads dropped from 1 to 4 ms to 0.03 to 0.04 ms.
- **Plans:** every statement on a large table reads through an index. The 13 sequential scans
  are on small tables: the site catalog (0 rows on staging), `revision` (1 row), the 11
  framework targets, the 18 products and `test_event_buckets` (5,535 rows).
- **Planning time** is under 9 ms for every statement.

## 3. Every page against its budget

Each page's requests are the bench's page specs (`bench.Specs`, the "After section 11" column of
`docs/api-web-map.md` section 10). The network page is one page per network.
- **"Sum of p99"** adds the p99 of every request the page makes.
- **"Sum of cold"** is the first view right after a new build.
- **The first table** is the 8-worker run against the bench's own budget (10 ms plus 20 ms of
  network time per request). It is shown for comparison only.
- **The verdict** is the server-time table after it.

| Page | Requests | Sum of p99 ms | Budget ms | Sum of cold ms | Pass |
|---|--:|--:|--:|--:|---|
| /proxy-index | 8 | 5.23 | 170 | 1.39 | pass |
| /proxy-index/compare | 18 | 11.23 | 370 | 2.30 | pass |
| /proxy-index/live-tape | 6 | 3.19 | 130 | 6.05 | pass |
| /proxy-index/networks | 4 | 2.62 | 90 | 0.92 | pass |
| /proxy-index/networks/byteful (residential) | 40 | 24.99 | 810 | 18.92 | pass |
| /proxy-index/networks/dataimpulse (residential) | 40 | 23.66 | 810 | 855.93 | pass |
| /proxy-index/networks/decodo (residential) | 40 | 23.21 | 810 | 1577.61 | pass |
| /proxy-index/networks/evomi (residential) | 40 | 23.19 | 810 | 1154.88 | pass |
| /proxy-index/networks/fleetproxy (residential) | 40 | 22.92 | 810 | 2021.63 | pass |
| /proxy-index/networks/geonode (residential) | 40 | 22.95 | 810 | 1048.76 | pass |
| /proxy-index/networks/infatica (residential) | 40 | 22.36 | 810 | 1692.87 | pass |
| /proxy-index/networks/iproyal (residential) | 40 | 25.34 | 810 | 1692.79 | pass |
| /proxy-index/networks/massive (residential) | 40 | 23.14 | 810 | 992.60 | pass |
| /proxy-index/networks/nodemaven (residential) | 40 | 22.72 | 810 | 1863.63 | pass |
| /proxy-index/networks/oxylabs (residential) | 40 | 22.66 | 810 | 2125.97 | pass |
| /proxy-index/networks/proxy-cheap (residential) | 40 | 23.50 | 810 | 19.71 | pass |
| /proxy-index/networks/proxy-seller (residential) | 40 | 22.17 | 810 | 2127.17 | pass |
| /proxy-index/networks/proxyempire (residential) | 40 | 23.03 | 810 | 1852.26 | pass |
| /proxy-index/networks/rayobyte (residential) | 40 | 22.37 | 810 | 1628.42 | pass |
| /proxy-index/networks/shifter (residential) | 40 | 22.64 | 810 | 1863.69 | pass |
| /proxy-index/networks/soax (residential) | 40 | 23.30 | 810 | 1480.53 | pass |
| /proxy-index/networks/webshare (residential) | 40 | 25.98 | 810 | 1899.91 | pass |
| /proxy-index/overlap | 4 | 2.45 | 90 | 0.47 | pass |
| /proxy-index/targets | 15 | 9.13 | 310 | 1.45 | pass |
| /proxy-index/true-cost | 17 | 10.71 | 350 | 2.22 | pass |
| freshness | 1 | 0.48 | 30 | 0.10 | pass |
| search palette | 2 | 0.89 | 50 | 0.56 | pass |

**Server time, the verdict** (concurrency 1, build 5, the same requests): each page's p99 sum
against the strict 10 ms.

| Page | 8 workers: sum of p99 ms | 1 worker (server time): sum of p99 ms | Budget ms | Pass |
|---|--:|--:|--:|---|
| a network page (18 pages, 40 requests each) | 22.17 to 25.98 | 6.37 to 7.43 | 10 | pass |
| every other page (9 pages) | at most 11.23 (`/proxy-index/compare`) | at most 4.23 | 10 | pass |

**Result on server time: 27 of 27 pages pass.**
- **Network pages:** the largest sum is infatica's 7.43 ms. Oxylabs (a large network) sums to
  6.54 ms, and byteful (a small one) to 6.82 ms.
- **The largest requests of a network page:**
  - `/v1/rankings?range=7d` at 0.73 ms;
  - infatica's `/coverage?range=30d` at 0.68 ms and rayobyte's `/chart?range=30d` at 0.66 ms;
  - `/v1/rankings?range=30d` and `/v1/targets?range=30d` at about 0.25 ms.

  Every other request of a network page is at most 0.24 ms. Outside the network pages the
  largest is `/v1/meta` at 0.87 ms (section 2.1).
- **The first view after a new build** is not part of the page budget. On 16 networks it sums to
  0.86 to 2.13 s, almost all of it the coverage search of section 2.1 (#797). On byteful and
  proxy-cheap it is 19 and 20 ms.


## 4. Every gap of the map

`siteapi bench --gaps docs/api-web-map-gaps.json` on build 4, after the bench:
- **114 pass, 46 fail:** the 160 `api` gaps;
- **49 listed without a verdict:** 40 `website`, 7 `owner` and 2 `withdrawn` gaps, with their
  reason from the map's sections 12 and 13.

**Every failure is a null that the answer does not explain in its `na` list, or a missing field**,
on the 24h range or on the network answer's 24h figures. They fall into two groups.
- **(a) 29 gaps: staging's catalog is empty.** The site tables (`site.pricing`,
  `ranking_profiles`, `reviews`, `targets`, `scopes` and `providers`) have 0 rows on staging.
  So list price, price per GB, trial, cost per 1,000 successes, value rank, ranking profiles,
  protocol text, review labels and target difficulty are null or missing:
  HOME-17, HOME-18, HOME-24, HOME-25, HOME-27, HOME-36, HC-1, HC-5, TGT-5, TGT-7, NETS-1,
  NET-LISTRATE, NET-LEADS, NET-PROTOCOL, NET-RANK-PRICE, NET-TARGET-META, NET-TRIAL-BOOL,
  NET-VALUE-RANK, NET-SCOPE-COPY, NET-ALT-PRICE, NET-VERIFY-LABEL, NET-SCOPE-UNUSED, CMP-8, CMP-12,
  TC-1, TC-2, TC-3, TC-6, TC-8.
- **(b) 17 gaps: data nulls the answer does not explain in `na`:**
  - `generic_https` cells, which staging never tests;
  - 24h browser cells with no run;
  - 24h uptime for 12 of 18 networks;
  - `latencyP90` and `latencyP99` in every range;
  - overlap pairs, present only in 30d;
  - the home market's 24h median latency and fastest network;
  - target score status:

  HOME-15, HOME-26, HOME-31, TGT-1, TGT-3, TGT-6, TGT-12, NET-RANK-AVAIL, NET-TARGETS-RANGE,
  NET-TARGET-RANK, NET-TARGET-LATRANK, CMP-6, CMP-9, CMP-11, OVL-1, TC-4, TC-5.

What happens to them (the architect's decision):
- **group (a), both:**
  - Shifter's committed catalog (`api/scripts/catalog-shifter.sql`, `pricing-shifter.sql`) is now
    loaded into staging, so Shifter has real catalog values in the next measurement;
  - #798 (API-NA-2) lists every null of the new fields in `na` with a reason code (`catalog`,
    `not_measured`, `no_run_in_window`, `no_evidence`, `not_computed_for_window`,
    `insufficient_data`). The other 17 networks' prices, trials, review labels and target
    categories are catalog content the owner writes (map section 12: TGT-8, CMP-15); until then
    they pass as nulls listed in `na`.
- **group (b):** #798 lists the nulls by design in `na`:
  - `generic_https`, the 24h browser cells with no run;
  - uptime without evidence, downtime and the 24h overlap.

  It also fixes three real bugs: `targets[].scoreStatus` missing (TGT-3), the home market's
  `medianLatency` and `fastest` (HOME-15), and `hardestRates[].scores` (HOME-31).

  It also finds why `latencyP90`/`P99` and the `generic_http` cell's P50 are null. Probably
  production's cadence has no load wave. It then serves the daily tests' latency flagged as
  light load if the snapshot has it, and `na` `not_measured` otherwise.
- **OVL-1's empty 7d pairs:** #799 (OVERLAP-7D-1). 7d should be counted once a UTC day and
  carried; after the deploy it was empty, a bug in the carry.

The verdict column gives the first failing request and path of each failing gap.

| Gap | Page | Class | Work item | Issue | PR | Requests checked | Verdict or reason |
|---|--:|---|---|---|---|--:|---|
| HOME-1 | 1 | website | | | | | Data age in the dock, tape ages from `at`, per-tab `category=` requests, two-letter monograms, the 26-point `trend`: Website changes; the API already serves the inputs. |
| HOME-2 | 1 | api | W10 | #714 | #755 | 1 | pass |
| HOME-3 | 1 | website | | | | | "Download the dataset" serves the website's `home.json`: The website route redirects to `/v1/dataset` (already immutable and precompressed). |
| HOME-4 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-5 | 1 | api | W1 | #704 | #731 | 18 | pass |
| HOME-6 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-7 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-8 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-9 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-10 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-11 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-12 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-13 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-14 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-15 | 1 | api | W6 | #712 | #750 | 3 | **fail**: `/v1/home?range=24h` market.medianLatency null at market.medianLatency, not listed in na |
| HOME-16 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-17 | 1 | api | W6 | #712 | #750 | 3 | **fail**: `/v1/home?range=24h` market.cheapest.slug null at market.cheapest, not listed in na |
| HOME-18 | 1 | api | W6 | #712 | #750 | 3 | **fail**: `/v1/home?range=24h` market.spread.value null at market.spread.value, not listed in na |
| HOME-19 | 1 | api | W2 | #710 | #738 | 3 | pass |
| HOME-20 | 1 | api | W3 | #711 | #752 | 18 | pass |
| HOME-21 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-22 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-23 | 1 | api | W1 | #704 | #731 | 21 | pass |
| HOME-24 | 1 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| HOME-25 | 1 | api | W6 | #712 | #750 | 3 | **fail**: `/v1/home?range=24h` market.cheapest.value null at market.cheapest, not listed in na |
| HOME-26 | 1 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` stats.*.claimShare null at stats.24h.claimShare, not listed in na |
| HOME-27 | 1 | api | W6 | #751 | #757 | 3 | **fail**: `/v1/home?range=24h` insights.value.best null at insights.value.best, not listed in na |
| HOME-28 | 1 | api | W2 | #710 | #738 | 3 | pass |
| HOME-29 | 1 | api | W6 | #751 | #757 | 3 | pass |
| HOME-30 | 1 | api | W6 | #751 | #757 | 3 | pass |
| HOME-31 | 1 | api | W6 | #751 | #757 | 3 | **fail**: `/v1/home?range=24h` insights.hardestRates[].scores.success.status null at insights.hardestRates[].scores, not listed in na |
| HOME-32 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-33 | 1 | api | W6 | #712 | #750 | 6 | pass |
| HOME-34 | 1 | api | W2 | #710 | #738 | 3 | pass |
| HOME-35 | 1 | api | W6 | #751 | #757 | 6 | pass |
| HOME-36 | 1 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` profiles[].id missing |
| HOME-37 | 1 | api | W7 | #707 | #728 | 1 | pass |
| HOME-38 | 1 | api | W1 | #704 | #731 | 3 | pass |
| HOME-39 | 1 | website | | | | | Data age in the dock, tape ages from `at`, per-tab `category=` requests, two-letter monograms, the 26-point `trend`: Website changes; the API already serves the inputs. |
| HOME-40 | 1 | api | W1 | #704 | #731 | 3 | pass |
| HOME-41 | 1 | api | W2 | #710 | #738 | 3 | pass |
| HOME-42 | 1 | api | W7 | #707 | #728 | 1 | pass |
| HOME-43 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-44 | 1 | website | | | | | Data age in the dock, tape ages from `at`, per-tab `category=` requests, two-letter monograms, the 26-point `trend`: Website changes; the API already serves the inputs. |
| HOME-45 | 1 | api | W1 | #704 | #731 | 54 | pass |
| HOME-46 | 1 | api | W2 | #710 | #738 | 3 | pass |
| HOME-47 | 1 | api | W6 | #751 | #757 | 3 | pass |
| HOME-48 | 1 | website | | | | | Demo networks in every home list (`SHOW_DEMO = true`): Website sets `SHOW_DEMO = false` when it reads the API (as OVL-8). |
| HOME-49 | 1 | owner | | | | | Headline score, grade, rank and sub-scores do not follow the dock (C19, C22-C25, C31, C32): Decide whether they follow the range; RK already serves each range, no API change. |
| HOME-50 | 1 | website | | | | | Network incidents counted over the whole list; hero and market request totals from two helpers: Website reads `stats.<r>.incidentCount` and one requests field for both tiles. |
| HOME-51 | 1 | api | W6 | #712 | #750 | 3 | pass |
| HOME-52 | 1 | website | | | | | Network incidents counted over the whole list; hero and market request totals from two helpers: Website reads `stats.<r>.incidentCount` and one requests field for both tiles. |
| HOME-53 | 1 | website | | | | | Gauge label "Non-hosting", card 3 lead over no rows, empty tape filter, hero range phrase, "0.0" for a null score, rank "–": Website copy and null states ("N/A", empty-state sentences, "Exit IPs in the requested country" label). |
| HC-1 | 2 | api | W7 | #707 | #728 | 1 | **fail**: `/v1/meta` profiles[].id missing |
| HC-2 | 2 | api | W7 | #707 | #728 | 1 | pass |
| HC-3 | 2 | api | W7 | #707 | #728 | 1 | pass |
| HC-4 | 2 | api | W2 | #710 | #738 | 3 | pass |
| HC-5 | 2 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| HC-6 | 2 | api | W2 | #710 | #738 | 3 | pass |
| HC-7 | 2 | api | W2 | #710 | #738 | 3 | pass |
| HC-8 | 2 | api | W10 | #714 | #755 | 1 | pass |
| HC-9 | 2 | website | | | | | "Download the dataset" serves the website's `home.json`: The website route redirects to `/v1/dataset` (already immutable and precompressed). |
| HC-10 | 2 | website | | | | | Gauge label "Non-hosting", card 3 lead over no rows, empty tape filter, hero range phrase, "0.0" for a null score, rank "–": Website copy and null states ("N/A", empty-state sentences, "Exit IPs in the requested country" label). |
| HC-11 | 2 | api | W10 | #714 | #755 | 1 | pass |
| HC-12 | 2 | website | | | | | "Live" hard-coded in the dock, `IndexHero` window and Overview hero; no page polls `/v1/version`: Website change: show data age from `generatedAt` and poll `/v1/version` (304 when unchanged), refetching on a new `revision`; extends HOME-1 to every page. |
| HC-13 | 2 | website | | | | | JSON-LD `dateModified` from three export dates; none on `/networks` and the network page: Website change: one `generatedAt` per build from any answer the page fetches; add it to the two network pages. |
| HC-14 | 2 | website | | | | | `SHOW_DEMO` demo networks and re-numbered ranks on every page: Website change: delete the switch and demo branches with the API switch-over; ranks from RK `rank`, `peers`. |
| HC-15 | 2 | website | | | | | Hard-coded `INDEX_NETWORK_SLUGS` for redirects, footer, sitemaps, llms.txt: Website change: build the slug table and the footer's provider lists from `/v1/networks` at build time; link only listed slugs; keep the `smartproxy` alias. |
| HC-16 | 2 | website | | | | | `NetworkStrip` crashes on 0 networks (server render): Website guards the empty list (hide the strip or an empty-state line); no API change. |
| TGT-1 | 3 | api | W4 | #705 | #732 | 12 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRate null at networks[].cells.generic_https.successRate, not listed in na |
| TGT-2 | 3 | api | W4 | #705 | #732 | 3 | pass |
| TGT-3 | 3 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` targets[].scoreStatus.*.status missing |
| TGT-4 | 3 | api | W2 | #710 | #738 | 3 | pass |
| TGT-5 | 3 | api | W4 | #705 | #732 | 4 | **fail**: `/v1/targets?kind=residential&range=24h` targets[].difficulty null at targets[].difficulty, not listed in na |
| TGT-6 | 3 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRate null at networks[].cells.generic_https.successRate, not listed in na |
| TGT-7 | 3 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` targets[].scoreWeight null at targets[].scoreWeight, not listed in na |
| TGT-8 | 3 | owner | | | | | `category` (and catalog `difficulty`) of the browser targets without a `site.targets` row: Catalog rows (owner content), written with SQL as `api/scripts/*.sql`; W4 defaults the difficulty to the scoring weight meanwhile. |
| TGT-9 | 3 | api | W4 | #705 | #732 | 3 | pass |
| TGT-10 | 3 | api | W4 | #705 | #732 | 12 | pass |
| TGT-11 | 3 | owner | | | | | Target scores rank every product group of a window together, not per proxy kind: All 18 networks are residential today, so the ranks are the residential ones; per-kind ranking waits until a second kind is measured. |
| TGT-12 | 3 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRate null at networks[].cells.generic_https.successRate, not listed in na |
| TGT-13 | 3 | api | W10 | #714 | #755 | 1 | pass |
| TGT-14 | 3 | website | | | | | Data strings (`label`, `category`, `passCondition`) used as translation keys on the targets page: Website keys target copy by `targets[].id`; the API keeps serving English catalog wording. |
| TGT-15 | 3 | website | | | | | Hard-coded "Eight"/"eight" and "ticketing queue" in the targets page's window title, meta and JSON-LD description and keywords: Website copy built from the card count (TGT-9 field once W4/W7 serve it), ticketing mention removed. |
| TGT-16 | 3 | website | | | | | Targets page still built on the mock suite (`ticketing_queue`, no `ai_answers`, no group filter, no sources breakdown): Website change when it switches to the API; reads `targets[].group`, `.source`, `.difficulty` and `sources[]` (W4 adds `sources` to its cells). |
| NETS-1 | 4 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| NETS-2 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-3 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-4 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-5 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-6 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-7 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-8 | 4 | api | W1 | #704 | #731 | 3 | pass |
| NETS-9 | 4 | api | W10 | #714 | #755 | 1 | pass |
| NETS-10 | 4 | api | W2 | #710 | #738 | 3 | pass |
| NETS-11 | 4 | api | W10 | #714 | #755 | 1 | pass |
| NETS-12 | 4 | website | | | | | "Download the dataset" serves the website's `home.json`: The website route redirects to `/v1/dataset` (already immutable and precompressed). |
| NETS-13 | 4 | website | | | | | "00" rank and blank grade for unscored networks; FAQ promises cost per 1,000 successes on every card; 30d score, grade and rank beside per-range figures: Website changes over fields RK already serves: print "N/A"; add `costPer1kSuccess` as a card figure (recommended, the FAQ's argument is the page's) or reword FAQ answers 3 and 4; read score, grade and rank from `?range=30d` and label them "30 days", or follow the range (owner decision). |
| NETS-14 | 4 | website | | | | | "00" rank and blank grade for unscored networks; FAQ promises cost per 1,000 successes on every card; 30d score, grade and rank beside per-range figures: Website changes over fields RK already serves: print "N/A"; add `costPer1kSuccess` as a card figure (recommended, the FAQ's argument is the page's) or reword FAQ answers 3 and 4; read score, grade and rank from `?range=30d` and label them "30 days", or follow the range (owner decision). |
| NETS-15 | 4 | owner | | | | | "00" rank and blank grade for unscored networks; FAQ promises cost per 1,000 successes on every card; 30d score, grade and rank beside per-range figures: Website changes over fields RK already serves: print "N/A"; add `costPer1kSuccess` as a card figure (recommended, the FAQ's argument is the page's) or reword FAQ answers 3 and 4; read score, grade and rank from `?range=30d` and label them "30 days", or follow the range (owner decision). |
| NETS-16 | 4 | api | W1 | #704 | #731 | 21 | pass |
| NET-PEERS | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-LISTRATE | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` profile.pricing.listRate null at profile.pricing.listRate, not listed in na |
| NET-LEADS | 5 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| NET-PROTOCOL | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` profile.protocol.summary null at profile.protocol.summary, not listed in na |
| NET-SEARCH-REVIEWS | 5 | api | W10 | #714 | #755 | 1 | pass |
| NET-RANK-SUCCESS | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-RANK-PRICE | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` stats.*.ranks.listRate.rank null at stats.24h.ranks.listRate.rank, not listed in na |
| NET-RANK-AVAIL | 5 | api | W3 | #711 | #752 | 21 | **fail**: `/v1/networks/byteful?kind=residential` stats.*.ranks.uptimePct.rank null at stats.24h.ranks.uptimePct.rank, not listed in na |
| NET-DC-RANGE | 5 | api | W1 | #704 | #731 | 18 | pass |
| NET-RANK-DC | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-LAT-LOAD | 5 | api | W1 | #704 | #731 | 54 | pass |
| NET-USAGE-LABEL | 5 | api | W3 | #711 | #752 | 54 | pass |
| NET-COUNTRY-RANK-NOTE | 5 | api | W3 | #711 | #752 | 54 | pass |
| NET-TARGETS-RANGE | 5 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRate null at networks[].cells.generic_https.successRate, not listed in na |
| NET-TARGET-META | 5 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` targets[].difficulty null at targets[].difficulty, not listed in na |
| NET-TARGET-RANK | 5 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRank.rank null at networks[].cells.generic_https.successRank.rank, not listed in na |
| NET-TARGET-LATRANK | 5 | api | W4 | #705 | #732 | 9 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.generic_http.latencyP50 null at networks[].cells.generic_http.latencyP50, not listed in na |
| NET-OVERLAP-RANGE | 5 | api | W5 | #706 | #726 | 3 | pass |
| NET-OWNERSHIP-LABEL | 5 | api | W5 | #706 | #726 | 3 | pass |
| NET-TRIAL-BOOL | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` profile.pricing.hasTrial null at profile.pricing.hasTrial, not listed in na |
| NET-VALUE-RANK | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` stats.*.ranks.valueRank.rank null at stats.24h.ranks.valueRank.rank, not listed in na |
| NET-PRICE-HISTORY | 5 | api | W12 | #709 | #736 | 18 | pass |
| NET-ALTERNATIVE | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-ARTICLE | 5 | website | | | | | The editorial review article (verdict, bottom line, body): Stays website content (it is editorial, written per network); no API source. |
| NET-SCOPE-COPY | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` profile.protocol.summary null at profile.protocol.summary, not listed in na |
| NET-BUCKET-TEXT | 5 | withdrawn | | | | | NET-BUCKET-TEXT (network page row 131) was withdrawn: the stored facts and the 24h series are 5-minute buckets (proxy-index `calculated/aggregate.py` `coverage_basis`, `series`); the 30-minute value is the every-request scoring slot. |
| NET-ASN-LEADER | 5 | api | W2 | #710 | #738 | 3 | pass |
| NET-ALT-PRICE | 5 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| NET-ANALYSIS-ARCHIVE | 5 | owner | | | | | Stored weekly analyses beyond 30 days: No source yet; out of scope until analyses are written. |
| NET-RANK-SENTENCE | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-REVIEW-COUNTS | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-REVIEWS-PAGED | 5 | api | W12 | #709 | #736 | 18 | pass |
| NET-VERIFY-LABEL | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` reviews[].verificationLabel missing |
| NET-SCORE-NOTE | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-SCOPE-UNUSED | 5 | api | W3 | #711 | #752 | 18 | **fail**: `/v1/networks/byteful?kind=residential` profile.protocol.schedule null at profile.protocol.schedule, not listed in na |
| NET-RECORDS-KIND | 5 | api | W3 | #711 | #752 | 18 | pass |
| NET-RUNS-PAGED | 5 | api | W12 | #709 | #736 | 18 | pass |
| NET-TWO-SOURCES | 5 | website | | | | | Network page reads two mock files that disagree on the score; a null grade prints blank or "Grade null"; the mock `ticketing_queue` row, three demo networks and the `?demo-checks` numbers: Website changes when the page reads the API: one source (`P.score`, `S[r]`), print "N/A" for a null grade (as NETS-13), drop `ticketing_queue` (as TGT-16), set `SHOW_DEMO = false` (as HOME-48) and remove `?demo-checks` or wire it to `S["30d"].latencyP90`, `P.performance.captchaRate` / `.blockRate` and `communityRuns[].targets[]`. |
| NET-NA-GRADE | 5 | website | | | | | Network page reads two mock files that disagree on the score; a null grade prints blank or "Grade null"; the mock `ticketing_queue` row, three demo networks and the `?demo-checks` numbers: Website changes when the page reads the API: one source (`P.score`, `S[r]`), print "N/A" for a null grade (as NETS-13), drop `ticketing_queue` (as TGT-16), set `SHOW_DEMO = false` (as HOME-48) and remove `?demo-checks` or wire it to `S["30d"].latencyP90`, `P.performance.captchaRate` / `.blockRate` and `communityRuns[].targets[]`. |
| NET-MOCK-DATA | 5 | website | | | | | Network page reads two mock files that disagree on the score; a null grade prints blank or "Grade null"; the mock `ticketing_queue` row, three demo networks and the `?demo-checks` numbers: Website changes when the page reads the API: one source (`P.score`, `S[r]`), print "N/A" for a null grade (as NETS-13), drop `ticketing_queue` (as TGT-16), set `SHOW_DEMO = false` (as HOME-48) and remove `?demo-checks` or wire it to `S["30d"].latencyP90`, `P.performance.captchaRate` / `.blockRate` and `communityRuns[].targets[]`. |
| CMP-1 | 6 | api | W1 | #704 | #731 | 21 | pass |
| CMP-2 | 6 | api | W1 | #704 | #731 | 21 | pass |
| CMP-3 | 6 | api | W1 | #704 | #731 | 21 | pass |
| CMP-4 | 6 | api | W1 | #704 | #731 | 24 | pass |
| CMP-5 | 6 | api | W5 | #706 | #726 | 3 | pass |
| CMP-6 | 6 | api | W2 | #710 | #738 | 21 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| CMP-7 | 6 | api | W1 | #704 | #731 | 3 | pass |
| CMP-8 | 6 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` targets[].difficulty null at targets[].difficulty, not listed in na |
| CMP-9 | 6 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRate null at networks[].cells.generic_https.successRate, not listed in na |
| CMP-10 | 6 | api | W4 | #705 | #732 | 9 | pass |
| CMP-11 | 6 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].latencyP90 null at networks[].latencyP90, not listed in na |
| CMP-12 | 6 | api | W2 | #710 | #738 | 21 | **fail**: `/v1/rankings?range=24h` networks[].trialKind null at networks[].trialKind, not listed in na |
| CMP-13 | 6 | website | | | | | Compare picks are not in the URL or stored: Website reads and writes `?networks=a,b,c,d`; no API change (every network is pre-rendered). |
| CMP-14 | 6 | website | | | | | Null grade, consent, difficulty and price states (C22, C51, C67b, C70, C74b): Website maps each null to "N/A" before formatting (also every other `gradeTone` call on the home and network pages); no API change. |
| CMP-15 | 6 | owner | | | | | Shifter's trial text and order URL are hard-coded in the website: Owner fills `site.pricing.trial` and the measured product's `url` for Shifter (`api/scripts/*.sql`); the website then drops both fallbacks. |
| CMP-16 | 6 | owner | | | | | Headline score, grade, rank and sub-scores do not follow the dock (C19, C22-C25, C31, C32): Decide whether they follow the range; RK already serves each range, no API change. |
| CMP-17 | 6 | website | | | | | By-target best mark can compare a Chrome rate with a Camoufox rate (C61, C63): Website marks the best only among cells of the same `engine`, or reads one engine's body (`/v1/targets?engine=<e>`); W4 carries `engine`, `otherEngines` and the per-engine bodies (#735). |
| CMP-18 | 6 | website | | | | | Success rate hint misdescribes the figure (C33): Website copy: "Mean of time-slot success rates, browser targets excluded" (as HOME-42). |
| OVL-1 | 7 | api | W5 | #706 | #726 | 3 | **fail**: `/v1/overlap?kind=residential&range=24h` networks[].peers[].sharedIps missing |
| OVL-2 | 7 | api | W5 | #706 | #726 | 3 | pass |
| OVL-3 | 7 | api | W5 | #706 | #726 | 3 | pass |
| OVL-4 | 7 | api | W5 | #706 | #726 | 3 | pass |
| OVL-5 | 7 | api | W5 | #706 | #726 | 3 | pass |
| OVL-6 | 7 | api | W5 | #706 | #726 | 3 | pass |
| OVL-7 | 7 | api | W5 | #706 | #726 | 3 | pass |
| OVL-8 | 7 | website | | | | | Demo overlap data (3 of 4 networks, inconsistent shares), the blank `[data-ov-empty]` state and the untranslated "N/A" on the overlap page: Website changes: read measured networks from the API with `SHOW_DEMO` off, add the empty-state copy, use the shared "N/A". |
| OVL-9 | 7 | website | | | | | Demo overlap data (3 of 4 networks, inconsistent shares), the blank `[data-ov-empty]` state and the untranslated "N/A" on the overlap page: Website changes: read measured networks from the API with `SHOW_DEMO` off, add the empty-state copy, use the shared "N/A". |
| OVL-10 | 7 | website | | | | | Demo overlap data (3 of 4 networks, inconsistent shares), the blank `[data-ov-empty]` state and the untranslated "N/A" on the overlap page: Website changes: read measured networks from the API with `SHOW_DEMO` off, add the empty-state copy, use the shared "N/A". |
| TC-1 | 8 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` cost.cheapest.slug null at cost.cheapest, not listed in na |
| TC-2 | 8 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` cost.spreadX null at cost.spreadX, not listed in na |
| TC-3 | 8 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.costPer1kSuccess null at networks[].cells.ai_answers.costPer1kSuccess, not listed in na |
| TC-4 | 8 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.successRate null at networks[].cells.generic_https.successRate, not listed in na |
| TC-5 | 8 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h` networks[].listRate null at networks[].listRate, not listed in na |
| TC-6 | 8 | api | W2 | #710 | #738 | 3 | **fail**: `/v1/rankings?range=24h&kind=residential` networks[].valueRank null at networks[].valueRank, not listed in na |
| TC-7 | 8 | api | W1 | #704 | #731 | 21 | pass |
| TC-8 | 8 | api | W4 | #705 | #732 | 3 | **fail**: `/v1/targets?kind=residential&range=24h` networks[].cells.*.costRank.rank null at networks[].cells.ai_answers.costRank.rank, not listed in na |
| TC-9 | 8 | api | W1 | #704 | #731 | 21 | pass |
| EV-COUNT | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-REPORTING | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-RANGE | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-SPARK | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-PASSRATE | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-PASS-SPARK | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-INCIDENTS | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-HEATMAP | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-RAIL | 9 | api | W9 | #713 | #760 | 3 | pass |
| EV-SLUG | 9 | api | W8 | #708 | #727 | 19 | pass |
| EV-TEXT | 9 | api | W8 | #708 | #727 | 1 | pass |
| EV-KINDS | 9 | website | | | | | Event kinds `request`, `rotation`, `pricing`, a "clean audit" event, the other network of an `ip_overlap` event: No framework source; the website maps the kinds it gets. |
| EV-SEARCH | 9 | api | W8 | #708 | #727 | 3 | pass |
| EV-CATEGORY-TAB | 9 | website | | | | | Data age in the dock, tape ages from `at`, per-tab `category=` requests, two-letter monograms, the 26-point `trend`: Website changes; the API already serves the inputs. |
| EV-CURSOR | 9 | api | W8 | #708 | #727 | 1 | pass |
| EV-CATCHUP | 9 | api | W8 | #708 | #727 | 1 | pass |
| EV-INITIAL | 9 | api | W8 | #708 | #727 | 1 | pass |
| EV-CORS | 9 | withdrawn | | | | | EV-CORS (live tape rows 3 and 80) was withdrawn: `/v1/events`, like every API answer, sends `Access-Control-Allow-Origin: *` (`api/internal/server/server.go`, `write`). |
| EV-MONOGRAM | 9 | website | | | | | Data age in the dock, tape ages from `at`, per-tab `category=` requests, two-letter monograms, the 26-point `trend`: Website changes; the API already serves the inputs. |
| EV-TARGETS-COUNT | 9 | api | W7 | #707 | #728 | 1 | pass |
| EV-SCORE | 9 | api | W4 | #705 | #732 | 6 | pass |
| EV-AUDIT-VERDICT | 9 | website | | | | | Event kinds `request`, `rotation`, `pricing`, a "clean audit" event, the other network of an `ip_overlap` event: No framework source; the website maps the kinds it gets. |
| EV-OVERLAP-PEER | 9 | website | | | | | Event kinds `request`, `rotation`, `pricing`, a "clean audit" event, the other network of an `ip_overlap` event: No framework source; the website maps the kinds it gets. |
| EV-BASELINE | 9 | api | W8 | #708 | #727 | 1 | pass |
| EV-AGO | 9 | api | W8 | #708 | #727 | 1 | pass |
| EV-NETTAPE | 9 | website | | | | | Per-network tape cap 20 (page shows 30): Website shows 20, or W8's `network=` filter. |
| EV-RAWKIND | 9 | api | W3 | #711 | #752 | 18 | pass |
| EV-NEWEST | 9 | website | | | | | Newest event(s) from a prioritized `/v1/events` answer (dateModified, hero rows, rail last event and beats): Website requests `limit` ≥ 2 (≥ 2k for the k newest); the rail uses W9. |
| EV-FEED-STATIC | 9 | website | | | | | The website feed route serves a build-time snapshot with per-build ids: Website route proxies `/v1/events` once W8 lands (EV-TEXT, EV-CURSOR); no API change beyond W8. |
| EV-EDITORIAL | 9 | api | W3 | #711 | #752 | 18 | pass |
| EV-PROXYKIND | 9 | api | W8 | #708 | #727 | 1 | pass |

## 5. What the website must change

Each page stops reading its mock JSON file and reads these answers (`docs/api-web-map.md`
section 10, "After section 11"):

| Page | Reads from the API | Instead of |
|---|---|---|
| `/proxy-index` | `/v1/home?range=` per range (market panel, insights, every network's panel), `/v1/rankings?range=` per range (leaderboard), `/v1/events` (the tape, polled) | `home.json`, `networks.json`, the 9 MB dataset file |
| `/proxy-index/targets` | `/v1/targets?range=` per range (per engine with `engine=`), `/v1/meta` | `targets.json`, the dataset file |
| `/proxy-index/networks` | `/v1/rankings?range=` per range, `/v1/meta` | `networks.json`, the dataset file |
| `/proxy-index/networks/<network>` | `/v1/networks/<slug>`, `/chart`, `/coverage`, `/places`, `/reviews`, `/runs`, `/pricing-history`, `/v1/rankings`, `/v1/targets` and `/v1/overlap` per range | `networks.json` and the network's mock review data |
| `/proxy-index/compare` | `/v1/rankings` × 3, `/v1/targets` × 3, `/v1/meta` | `networks.json`, `compare-data.ts` rows |
| `/proxy-index/overlap` | `/v1/overlap`, `/v1/rankings` | the overlap demo data |
| `/proxy-index/true-cost` | `/v1/rankings` × 3 (with the `cost` summary), `/v1/targets` × 3 (cost per target) | `cost-data.ts`, the dataset file |
| `/proxy-index/live-tape` | `/v1/events?limit=200` at render, `/v1/events/stats?range=` per range, the `/v1/events?after=` poll every 15 s | `live-tape.json` built at deploy time |
| Search palette (every page) | `/v1/search-index`, once per visit | the website's own `search.json` |
| Freshness (every page) | `GET /v1/version` with `If-None-Match` while the page is open; refetch the page's answers on a new `revision` | nothing (no page polls today) |

The section 12 rows of the map, as a short list:

1. **Demo data off:** set `SHOW_DEMO = false` and delete the demo branches and re-numbered ranks
   (HOME-48, HC-14, OVL-8, NET-MOCK-DATA); ranks come from `/v1/rankings` `rank` and `peers`.
2. **"N/A" for every null:** grade, score, consent, difficulty, price and rank print "N/A", never
   "0.0", "00", "–", "Grade null" or a blank (CMP-14, NETS-13, NET-NA-GRADE, HOME-53, HC-10).
3. **Empty states:** the overlap page's `[data-ov-empty]`, an empty tape filter, card 3's lead over
   no rows and a `NetworkStrip` with no networks get a sentence instead of a blank or a crash
   (OVL-9, HOME-53, HC-16).
4. **Data age, not "Live":** the dock, the hero and the Overview show the data's age from
   `generatedAt`, and every page polls `/v1/version` and refetches on a new `revision` (HOME-1,
   HC-12); JSON-LD `dateModified` is the build's one `generatedAt` (HC-13).
5. **The dataset download** redirects to `/v1/dataset` (HOME-3, HC-9, NETS-12).
6. **Target copy keyed by id:** translations and copy key on `targets[].id`, never on the English
   `label`, `category` or `passCondition` (TGT-14); the targets page's "Eight" and "ticketing
   queue" come from the card count (TGT-15, TGT-16).
7. **The best mark per engine:** on compare and targets, the best cell is marked only among cells
   of the same `engine`, or the page reads one engine's body (`/v1/targets?engine=<e>`) (CMP-17).
8. **One source per figure:** the network page reads its score from one answer (`P.score`,
   `S[r]`, NET-TWO-SOURCES); incidents from `stats.<r>.incidentCount` and one requests field for
   both tiles (HOME-50, HOME-52).
9. **Slugs from the API:** redirects, footer, sitemaps and `llms.txt` are built from
   `/v1/networks` at build time (HC-15); compare keeps its picks in `?networks=` (CMP-13).
10. **Tape rows:** ages from `at`, `category=` per tab, two-letter monograms, `limit` ≥ 2 for the
    newest event, and the feed route proxies `/v1/events` (HOME-44, EV-CATEGORY-TAB, EV-MONOGRAM,
    EV-NEWEST, EV-FEED-STATIC, EV-NETTAPE).
11. **Copy:** the success-rate hint (CMP-18), the "Non-hosting" gauge label (HOME-53), FAQ answers
    3 and 4 of the networks page (NETS-14).

**The new score fields to show next to the percentages** (`docs/api-web-map.md`, "Rank-based
browser target scores"):

- per target, the network's main engine: `targets[<id>].scores.success`, `.challenge`, `.block`
  (each `{score, rank, rankedOf, status}`), next to `successRate`, `captchaRate`, `blockRate`;
- per target and engine: `?engine=<e>` bodies of `/v1/targets`, next to that engine's outcomes and
  page load;
- per network: `browserOverall.<engine>.success`, next to the network's mean target success.

`score` is 1 to 10, and `score`, `rank` and `rankedOf` are null unless `status` is `ranked`. The
other statuses (`not_enough_data`, `too_few_networks`, `all_similar`) print "N/A" with a short
reason.

## 6. Data size and cadence

### 6.1 Rows and bytes per table

What each simulated day added, from #718's size report (`simulate.py`), with the expected
figures: the real reference's rows and bytes per run, times the day's runs. Day 1 is shown
apart because every IP is new on day 1. Days 2 to 7 are the mean of six days, within ±1% of
each other except where named.

| Table | Rows, day 1 | Rows a day, days 2–7 | Expected a day | Table MB a day (expected) | Index MB a day (expected) | 7 days: rows | 7 days: table + index GB |
|---|--:|--:|--:|--:|--:|--:|--:|
| `ip_observations` (daily partitions) | 27,367,502 | 27,361,543 | 24,220,262 (+13%) | 1,998 (1,769, +13%) | 2,619 (2,184, **+20%**) | 194,665,711 | 32.8 |
| `ip_enrichment` | 17,917,583 | 17,901,123 | 24,220,262 (**−26%**) | 894 (1,207, **−26%**) | 1,000 (1,399, **−29%**) | 127,352,465 | 13.4 |
| `ip_addresses` | 17,917,583 | 8,983,491 | 24,220,262 (**−63%**) | 415 (1,043, **−60%**) | 604 (1,216, **−50%**) | 73,208,912 | 8.1 |
| `pool_ips` | 17,917,583 | 8,983,583 | 24,220,262 (**−63%**) | 381 (1,024, **−63%**) | 277 (936, **−70%**) | 73,580,599 | 5.5 |
| `product_ips` | 17,917,583 | 8,983,491 | 24,220,262 (**−63%**) | 706 (1,769, **−60%**) | 1,071 (1,900, **−44%**) | 73,580,045 | 13.5 |
| `scope_ips` (new in v14) | 46,078,592 (day 4, the back-fill) | 8,978,798 (days 5–7) | (none) | 379 | 274 | 73,580,599 | 5.3 |
| `estimated_hours` | 92,352 | 92,352 | 92,352 (0%) | 12.7 (13.3, −5%) | 7.1 (6.3, **+13%**) | 654,312 | 0.14 |
| `run_buckets` | 11,221 | 11,222 | 11,382 (−1%) | 3.2 (3.8, **−15%**) | 0.4 (0.7, **−43%**) | 79,593 | 0.03 |
| `run_enrichments` | 3,848 | 3,848 | 3,848 (0%) | 0.8 (1.4, **−45%**) | 0.1 (0.3, **−67%**) | 27,263 | 0.01 |
| `test_runs` | 3,992 | 3,992 | 3,848 (+4%) | 6.3 (6.6, −5%) | 0.4 (0.9, **−56%**) | 28,344 | 0.05 |
| `test_events` | 2,834 | 2,402 | (not modelled) | 0.5 | 0.2 | 17,334 | 0.01 |
| `calculated_*` (after the calculations) | | | | | | 84M rows, mostly `calculated_ip_last_seen` (73.2M) and `calculated_list_rows` (10.5M) | 7.8 |

Every deviation over 10%, named:
- **The expected column ignores recurrence.** From day 2 on most IPs are already known, so
  `ip_addresses`, `pool_ips` and `product_ips` grow by the new IPs only (−63%).
  #784's check, which counts recurrence, finds them in line.
- **`ip_enrichment` is 26% below this column but about twice #784's.** The generator enriched
  every IP again each day, with a new DB-IP release daily; production uses one a month (#793).
- **`ip_observations`:** 13% more rows than this column, and 16% (proxy-cheap) to 70% (massive) more
  observation rows per IP than the reference in #784's per-IP check. The issue's "47% to 70%"
  covers 12 of the 18 networks; the other six are at 16% to 44%. The model writes more rows per IP (#793).
- **Small tables** (`run_buckets`, `run_enrichments`, `test_runs`, `estimated_hours`): their index
  and table bytes are smaller or larger by 13% to 67%. These are megabytes a day, from the
  reference's per-run means over a small real hour.
- **The database:** 0.7 GB before the load, 82.1 GB after the 7 days, 91.4 GB after the
  calculations, and 86 GB at this report's start (pruned builds).

**The real 24 hours** (#718's reference, REPLAY-RUN-1's real day) was measured at a different
cadence: one run per network, region and hour, not one daily test per network and country. So
its totals are not comparable per day: 320,954,087 unique IPs, 983,425,042 requests, 324
browser runs, 17,066 tries. A generated day has 17.9M unique IPs, 53M requests, 144 browser
runs and 14,400 tries.

### 6.2 Runs per day by kind

| Day | Daily tests (`generic_http`) | Browser runs (100 tries each) | HTTP pulses |
|---|--:|--:|--:|
| 2026-11-02 to 2026-11-08, each day | 3,848 | 144 | 0 |
| 2026-11-09 (the extra hour) | 137 | 6 | 0 |

This is section 14's production cadence:
- one daily test per network × country per day;
- one 100-try browser run per network × target per day (18 networks × 8 targets);
- no pulses, census or load waves.

It rests on section 14's assumption, decided from the code and configs: production has no pulse
fleet.

### 6.3 Browser runs in a 24h window

For each of the 149 hourly 24h windows ending 2026-11-03 00:00 to 2026-11-09 04:00, the runs of
each of the 144 network × browser target pairs (by run start):

| Runs in the window | Pair-windows | Share |
|---|--:|--:|
| 0 | 2,940 | 13.7% |
| 1 | 15,612 | 72.8% |
| 2 | 2,904 | 13.5% |

So about one 24h browser figure in seven is empty, and one in seven covers 200 tries.

**24h browser scores:**
- in build 4's `/v1/targets?range=24h`, 18 of the 144 browser cells have no run and 126 have
  tries;
- **every** cell's `scores` is null, in every range (7d and 30d too), and every network's
  `browserOverall` is null, so no score came out `not_enough_data`, `ranked` or any other status.

The cause is a gap of the v4 builds, not of the data:
- the build's network summaries (`calculated_snapshots.api_head`) carry the per-target figures
  but not their `scores` or `browser_overall`;
- the product parts that do hold them (18 parts in build 4) are not read by the v4 path.

It is a miss: #800 (URGENT SCORES-V4-1).
- **The cause:** the calculator scores the product groups' windows only, while a v4 build is
  served from the head's network summaries, which are built without scores.
- **#689's API tests passed** because their v4 fixtures had scores written in by hand.
- **#800's fix:**
  - scores the network summaries, ranking networks per target, window and engine;
  - adds an end-to-end test from the calculator's own head to an API fixture;
  - tightens the gap manifest, so a browser cell with tries must have a score status.

After #800 merges, staging is recalculated and `bench --gaps` runs again. This section then
gives, per range, how many browser cells are `ranked`, `not_enough_data`, `too_few_networks`
and `all_similar`. With one 100-try run a day, many 24h cells are expected to be
`not_enough_data` (13.7% of 24h windows hold no run at all, section 6.3's table).

### 6.4 Three networks' 24h figures recomputed

The API's 24h unique IPs (`/v1/networks/<slug>` `stats.24h.uniqueIps`, build 4, as-of
2026-11-09 03:00), recomputed in SQL from the stored rows with the hour formula
(`estimates/window.py`, the `distinct` method):
- **the hours:** the `estimated_hours` released by the as-of whose hour starts in the 24h window;
- **per country:** the distinct IPs of those tests' observations, times the sum of their
  `day_unique_ips` over the sum of their `measured_ips`;
- **the network:** the sum over its countries, rounded half up.

| Network | Countries | Tests | Released hours | Recomputed | API | Equal |
|---|--:|--:|--:|--:|--:|---|
| byteful | 134 | 266 | 22 | 226,696 | 226,696 | yes |
| oxylabs | 230 | 440 | 22 | 6,947,427 | 6,947,427 | yes |
| webshare | 225 | 431 | 22 | 5,094,211 | 5,094,211 | yes |

A single distinct count over all of a network's countries gives 0.02% to 0.73% more: an IP seen
in two requested countries counts in each country's window.

## 7. The pipeline at 7-day volume

| Step | Before the fixes (#718) | After, on 0b0acf9 | Budget | Pass |
|---|---|---|---|---|
| Ingest (store) at 78 ms round trip | 35,000 rows/s, about 100 daily tests a minute (days 5–7, 8 connections) | unchanged code path | (none) | |
| Full calculation (every group recounted) | 11,778 s on 9cf43c2 (window 4,508 s, writes 4,627 s in 450,298 statements, other reads 2,191 s); 1,786 s on 4315df5 | **1,720 s** (28.7 min) "after a deploy", as-of 03:00: all 3,866 groups recounted, 39,197 statements in 3,597 s, peak RSS 5.39 GB, 11.7 GB temp | (no budget; the first hour after a deploy) | |
| Hourly incremental calculation | 5,413 s on 9cf43c2 (writes 4,041 s in 431,293 statements, other reads 1,045 s, window 164 s) | **677 s** (11.3 min), as-of 04:00, the second build on the same code: 64 of 3,866 groups recounted (their windows gained the hour), the other 3,802 lists reused; 1,177 statements in 870 s; peak RSS 5.36 GB; 12.4 GB temp | 3,000 s | pass |
| siteapi full rebuild | 95.4 s (load 14.9, assemble 76.2, render 3.1) | **29.1 s** (load 18.4, assemble 6.9, render 2.8); 28.2 s for the hourly build (load 17.3, assemble 7.0, render 2.8) | 60 s | pass |
| siteapi memory | `MemoryPeak` 4.49 GB | 3.61 GB after the first rebuild, 3.95 GB after the second while serving (`MemoryMax` 5 GB) | (cap 5 GB) | pass |
| Dataset file | 43.4 MB, about 1.1 s | 43.3 MB (gzip 6.97 MB, brotli 2.38 MB), about 1.1 s to write (the rebuild minus its load, assembly and render) | 2 s | pass |

The calculation's largest statements after the deploy:

| Statement | Calls | Seconds | Largest call s |
|---|--:|--:|--:|
| one-pass window per group (`calculated/sqlwindow.py`) | 3,866 | 2,325 | 49.7 |
| series point IPs (`calculated/point_ips.py`, #787) | 44 | 505 | 151.1 |
| `COPY calculated_list_rows` | 3,866 | 120 | 1.2 |
| `SPAN` distinct IPs (`calculated/fleetsql.py`) | 9 | 77 | 14.5 |
| `COUNTS`, fleet and last-seen counts (`calculated/fleetsql.py`, #790) | 26 | 71 | 32.2 |

- **Series point IPs (#787):** 6.0 GB of temp for 9 points plus 0 MB for 144 points, against
  38.8 GB before.
- **The calculation's temp in all:** 10.6 GB as the calculator counts it, 11.7 GB by
  `pg_stat_database`; 57.4 GB before.

**The hourly calculation's time:**
- **Series point IPs: 490 s of its 677**, recounted every hour; 6.1 GB of temp for the 9 daily
  points of the 7d and 30d series.
- **`SPAN` distinct IPs:** 78 s.
- **The 64 missed groups' windows:** 43 s.

So the hour fits its budget 4.4 times over. The point IPs are now the largest part, and they
write about 6 GB of temp on the shared database server every hour.

## 8. Data statistics per network

`simulate_check.stats` on day 2 of the stored week (#784's re-check on main 79a5606): **231 of 233
checks pass.** Requests per IP, country, ASN and city are total variation distances against the
real profile (0 is identical, limit 0.05). Recurrence is the share of IPs seen again, in
percentage points (limit 5). Browser outcomes are the worst of the network's 8 targets, in
percentage points (limit 2).

| Network | Requests per IP | Country | ASN | City | Recurrence | Browser outcomes (worst of 8 targets) | Checks passed |
|---|--:|--:|--:|--:|--:|--:|--:|
| byteful | 0.003 | 0.001 | 0.023 | 0.034 | – | 1.000 | 12 of 12 |
| dataimpulse | 0.000 | 0.000 | 0.003 | 0.004 | 0.000 | 1.000 | 13 of 13 |
| decodo | 0.000 | 0.000 | 0.001 | 0.002 | -0.000 | 1.000 | 13 of 13 |
| evomi | 0.000 | 0.000 | 0.002 | 0.003 | 0.001 | 1.000 | 13 of 13 |
| fleetproxy | 0.000 | 0.000 | 0.002 | 0.003 | -0.001 | 1.500 | 13 of 13 |
| geonode | 0.000 | 0.001 | 0.006 | 0.008 | 0.002 | 1.667 | 13 of 13 |
| infatica | 0.000 | 0.000 | 0.001 | 0.002 | 0.000 | 1.618 | 13 of 13 |
| iproyal | 0.000 | 0.000 | 0.003 | 0.004 | 0.000 | 1.333 | 13 of 13 |
| massive | 0.000 | 0.000 | 0.004 | 0.005 | -0.000 | 1.000 | 13 of 13 |
| nodemaven | 0.000 | 0.000 | 0.002 | 0.002 | -0.001 | 1.333 | 13 of 13 |
| oxylabs | 0.000 | 0.000 | 0.001 | 0.002 | 0.000 | 1.000 | 13 of 13 |
| proxy-cheap | 0.007 | 0.001 | 0.077 **fail** | 0.108 **fail** | -0.070 | 1.000 | 11 of 13 |
| proxy-seller | 0.000 | 0.000 | 0.001 | 0.002 | 0.000 | 1.000 | 13 of 13 |
| proxyempire | 0.000 | 0.000 | 0.002 | 0.003 | 0.000 | 1.000 | 13 of 13 |
| rayobyte | 0.000 | 0.000 | 0.008 | 0.010 | -0.001 | 1.000 | 13 of 13 |
| shifter | 0.000 | 0.000 | 0.001 | 0.001 | 0.000 | 1.000 | 13 of 13 |
| soax | 0.000 | 0.000 | 0.003 | 0.004 | -0.000 | 1.000 | 13 of 13 |
| webshare | 0.000 | 0.000 | 0.002 | 0.002 | 0.000 | 1.265 | 13 of 13 |

proxy-cheap's ASN (0.077) and city (0.108) distances are the two failures: its places differ a
little from the reference. byteful has too few reference IPs for a recurrence check
(`insufficient_reference`).

## 9. Fix issues this report needed, and the known limits

**Fix issues during the phase, with what they measured on staging:**

| Issue | What | Before | After |
|---|---|---|---|
| #735 (TARGETS-SIZE-1) | `/v1/targets` split per engine, under the 200 KB cap | 254,816 bytes, over the cap | at most 88,131 bytes (identity) on staging |
| #739 (CI-GATE-1) | the merge queue requires `api` | red `main` merged twice | required since 2026-11-08 22:09 |
| #745, #747 | red `main`: map row out of step; package did not compile | | fixed (section 10) |
| #749 (EV-CURSOR-SETTLE-1) | `/v1/events?after=` never skips a late row | found in review | fixed |
| #754 (LATENCY-GATE-1) | CI checks a 5× guard; the strict budgets are measured here | flaky `TestPageLatency` | stable |
| #719 (DB-EXPLAIN-1) | v15 partial `test_events` indexes | notable reads 1–4 ms on staging, 47–316 ms on a synthetic year | 0.03–0.04 ms (section 2.2) |
| #787 (CALC-POINT-IPS-1) | series point IPs hash-aggregated in batches | 38.8 GB temp in 2 statements | 6.0 GB for 9 points, 0 MB for 144 points |
| #788 (SITEAPI-REBUILD-1) | the rebuild reads each list's rows once, in parallel | 95.4 s | 29.1 s |
| #789 (API-LOAD-STATEMENTS-1) | list counts and snapshot head stored at write time | 514.6 ms and 269.1 ms | 0.06 ms and 0.04 ms |
| #790 (CALC-FLEET-DISTINCT-1) | last-seen counts in one pass, DISTINCT counts hash-aggregated | `LAST_SEEN` 87 s, fleet 60 s (10.5 GB temp), network scores 59 s (4.1 GB temp) | `COUNTS` 71 s in 26 calls; the whole calculation's temp 11.7 GB against 57.4 GB |
| #793 (SIM-MODEL-3) | the simulator's per-IP rows, buckets, DB-IP cadence and browser bytes | section 6.1 | merged (PR #796) after this run; for a future load |

**Misses this report found, with their fix issues** (filed by the architect in this milestone;
#720 depends on them, and this report is measured again after they merge):

1. **Coverage search, first render:** 42 of 54 requests over 70 ms cold, 380 to 1,071 ms
   (section 2.1). Fix: #797 (COVERAGE-SEARCH-1).
2. **46 map gaps:** nulls not listed in `na`, 29 from staging's empty catalog and 17 from sparse
   24h data and fields never filled (section 4). Fix: #798 (API-NA-2) for the `na` reasons and three real bugs, #799 (OVERLAP-7D-1) for the 7d overlap pairs; Shifter's catalog is loaded into staging.
3. **Browser scores missing from v4 builds:** every `scores` and `browserOverall` null in every
   range (section 6.3). Fix: #800 (URGENT SCORES-V4-1): the network summaries get their scores, with an end-to-end test from the calculator's head.

**Known limits:**
- **`/v1/targets` size:** it fits under the 200 KB cap up to 23 networks (#735); past 22 it must
  be paged or split by target category. Its largest body on staging is 88 KB for 18 networks.
- **Staging's events:** 17,334 rows of 3 routine kinds, with no country and no target (no pulses).
  So the rare event reads were also measured on #719's synthetic year: 0.7 to 2.6 ms with the
  v15 indexes.
- **Staging's site catalog is empty:** pricing, reviews, profiles, targets, scopes and providers.
  The answers that read it were timed at that size. These tables hold at most hundreds of
  rows in production, and they are read once per build.
- **The 200 MB download test** is not run (decision 5).
- **One connection's download speed:** the database server caps each connection at about
  15 MB/s (#718's measurement), so the calculation's reads are parallel and size-first (#778).

## 10. Red main log

`main` was red four times during the phase, all on 2026-11-08 (UTC). The orchestrator's list
did not reach this worker, so the log below is built from the fix issues and the merge queue's
own runs (`merge_group` events). Before CI-GATE-1 (#739, PR #741) the queue did not require the
`api` job, so a pull request whose queue run of `api` failed could still merge; before
MAP-GAPS-RED-1 (#745, PR #746) `tier1` ran pytest only when framework code changed.

| # | Red from (commit, PR) | Failing check | Fixed by (issue, PR, commit) | Merged while red | Did its own checks cover the failing test? |
|--:|---|---|---|---|---|
| 1 | b7a2d13, PR #732 (`/v1/targets`) 20:12 | `api`: `TestPageLatency`, `/v1/targets/residential/24h` 254,816 bytes over the 204,800-byte answer cap | #735 (TARGETS-SIZE-1), PR #740, e92c363 at 20:54 | PR #734 (ec593ca), PR #737 (36bce42) | #734: yes, its queue run of `api` failed on the same test, but `api` was not required. #737: no, it changed no API file, so `api` ran nothing. |
| 2 | e92c363, PR #740, 20:54 | `tier1`: `tests/test_api_web_map_gaps.py` (3 tests, map row CMP-17 not equal to the gap manifest) | #745 (MAP-GAPS-RED-1), PR #746, 79f367e at 21:31 | PR #736 (47d40d8), PR #728 (edcd4e6), PR #738 (4645100) | No: their `tier1` runs passed without running pytest (no framework code changed). PR #744 and PR #743 ran it, failed and left the queue. |
| 3 | 4645100, PR #738, 21:12 | `api`: the package does not compile (`TargetCounts` declared twice, by PR #738 and PR #728) | #747 (BUILD-RED-1), PR #748, 8abac7a at 21:59 | PR #746 (79f367e), PR #743 (2822315), PR #727 (0813d2f), PR #744 (19174a0), PR #750 (bd21e88) | #727 and #750: yes, their queue runs of `api` failed, but `api` was not required. #746, #743, #744: no, they changed no API file. |
| 4 | 2665471, PR #741, 22:05 | `api`: `TestPageLatency` over its 2 ms budgets on a shared CI server (noise, not a regression) | #754 (LATENCY-GATE-1), PR #756, c89f12a at 22:38 | PR #752 (f15be22), PR #758 (55c4a6f), PR #755 (83015c1) | #741's own queue run failed the test and merged (the last merge before `api` became required). From 22:09 `api` was required; #758 and #755 passed it in the queue. A run for #752 at 22:12 failed the same test after #752 had merged. |

Since c89f12a (2026-11-08 22:38) no merge-queue run of `main` has failed a required check that
then merged; the one later queue failure of a pull request (PR #773 on 2026-11-09, `tier1`) left the queue and
`main` stayed green.
