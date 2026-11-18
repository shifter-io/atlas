# Release checklist

Every production deploy of the API follows these steps. They exist because a change can pass
every test and still break the website: a redirect loop on one URL form that only the website
requests is enough to take every page down. The check below requests every URL the website
uses, in the exact forms it uses, before and after each deploy.

## The site check

`scripts/site-check.sh` requests every URL in `scripts/site-check.urls` (each network from
`/v1/networks`, each range) and, with `--site`, the website's own pages. A URL passes when it
answers 200 with at most one redirect hop and no redirect to itself; an API URL must also answer
valid JSON, and a page must not contain the text "temporarily unavailable". The script prints a
summary line and a Markdown table and exits 0 only when every URL passes.

```sh
scripts/site-check.sh --site <website base> <API base>          # failures and redirects
scripts/site-check.sh --site <website base> --full <API base>   # every URL
scripts/site-check.sh --list <API base>                          # the URL list only
```

When the website starts or stops requesting a URL, update `scripts/site-check.urls` in the same
change: it is the one list of the URLs to check.

## Before asking for a production deploy

1. Deploy the release to staging.
2. Run the site check on staging. Every URL must pass.
3. Include the check's summary line and table in the deploy request. A request without them, or
   with any failure in them, is not ready.

## Right after the production deploy

1. Run the site check on production with `--site` and the production website base.
2. Open the website's start page and one network page in a browser.
3. If anything fails, roll back to the previous release at once, then investigate. Do not try a
   fix forward on production first.
4. Post the check's summary line and table with the deploy report.
