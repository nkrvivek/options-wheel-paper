# Options wheel Cloudflare dispatcher

This Worker replaces GitHub Actions' drifting `schedule` event. Cloudflare runs
at 14:45 UTC Monday-Friday and sends a `workflow_dispatch` request for
`wheel-daily.yml` on `main`.

Cloudflare weekday numbering is `1=Sunday`, so the Wrangler cron is
`45 14 * * 2-6` for Monday-Friday.

`GITHUB_TOKEN` is a Worker secret. The initial value comes from the authenticated
`gh` CLI token; replace it with a fine-grained PAT limited to Actions write on
`nkrvivek/options-wheel-paper`.
