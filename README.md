# Taylor Solar for Home Assistant

A custom integration that reads the [Taylor Solar](https://taylor.solar) cloud API and imports solar production and the other energy flows into Home Assistant's long-term statistics at the hour they actually happened. You can then use them in the Energy dashboard.

A RESTful sensor would book energy at poll time, couldn't backfill, and would read Taylor's retroactive corrections as meter resets. This integration does the following instead:

- It writes Taylor's 30-minute (or 15-minute) buckets into **external statistics** at their real UTC hour.
- It **backfills** history on first setup, from 0 to 1095 days.
- On every run it **re-imports yesterday and today** to pick up Taylor's revisions, and rewrites the running sums consistently.
- It handles token refresh, rate limiting (HTTP 429) and API-version breaks (HTTP 418, raised as a repair issue).

Requires Home Assistant 2026.1 or newer. It has no dependencies beyond what Home Assistant ships.

## Installation

**HACS:** HACS → ⋮ → Custom repositories → add `https://github.com/lucian-tosa/ha-taylor` with category *Integration*. Then install *Taylor Solar* and restart Home Assistant. If the repository is private, HACS may not be able to download it. In that case, use the manual install.

**Manual:** copy `custom_components/taylor_solar` into `/config/custom_components/` and restart Home Assistant.

Then go to Settings → Devices & services → Add integration → **Taylor Solar**. Enter your Taylor email and password, and choose how many days of history to import. If the account has several sites, pick one. Add the integration again for each additional site.

## Energy dashboard

Settings → Dashboards → Energy → Solar production → add **Taylor ‹site› solar production**.

The statistics appear once the first hour has been imported. On first setup, the backfill runs in the background at about 8 requests per minute (Taylor allows about 10), so a year takes about 45 minutes. The *Statistics imported through* diagnostic sensor shows progress.

Statistics (`taylor_solar:<site>_<key>`) are created for every type present in the data: `solar_production`, `consumption`, `grid_import`, `grid_export`, `battery_charge` and `battery_discharge`. If grid import/export already comes from a P1 meter, keep using that for the grid.

## Entities

One device per site, with these entities:

| Entity | Notes |
| --- | --- |
| *‹Type› today* (kWh) | Today's total per type present. There is no `state_class`, so these don't show up in the Energy dashboard picker. Use the statistics above there. |
| *Solar power* (W) | Average power over the latest bucket. Attribute `bucket_start`. |
| *Panel ‹id› energy today* (kWh) | Attributes `cell_string_a_wh`, `cell_string_b_wh`, `cell_string_c_wh`. |
| *Statistics imported through* (date, diagnostic) | The importer's cursor. |

Data is polled every 15 minutes. Taylor's timestamps are naive site-local times, and they are interpreted in Home Assistant's configured time zone.

## Removing

Removing the integration deletes its import cursor, but the **imported statistics stay in place**. To delete them, go to Developer tools → Statistics. If you re-add the integration, it backfills again and overwrites the same statistic IDs.

To change the backfill period, remove the integration and add it again.

## Verifying the API

A few behaviors of the Taylor API are undocumented. The code paths that depend on them are marked `TODO(verify)`. You can check them with `curl` and `jq`:

```bash
H=(-H 'Client-Version: 1.0.0' -H 'Client-Name: end-user-api' -H 'Content-Type: application/json')
API=https://clientapi.taylor.solar
AUTH=$(curl -s "${H[@]}" -X POST $API/api/authenticate \
  -d '{"userName":"you@example.com","password":"…","persistUserSession":true}')
TOKEN=$(jq -r .token <<<"$AUTH")
SITE=$(curl -s "${H[@]}" -H "Authorization: Taylor $TOKEN" $API/api/public/sites | jq -r '.[0].id')
day() { curl -s "${H[@]}" -H "Authorization: Taylor $TOKEN" "$API/api/public/site/$SITE/data/$1"; }
```

1. **Are consumption, import and export (types 1–3) populated without a Taylor meter or battery?** Print the day totals per type:
   ```bash
   day 2026/6/15 | jq '[.systemMetrics[].dataPoints[].data[]] | group_by(.type) | map({type: .[0].type, wh: (map(.wh) | add)})'
   ```
2. **What is returned before the install date (2024-04-13)?** Check whether it's a 404, empty `dataPoints` or zeros:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' "${H[@]}" -H "Authorization: Taylor $TOKEN" "$API/api/public/site/$SITE/data/2024/4/1"
   day 2024/4/1 | jq '{points: [.systemMetrics[].dataPoints[]] | length, wh: [.systemMetrics[].dataPoints[].data[].wh] | add}'
   ```
3. **Are 02:00/02:30 repeated on the DST fall-back day?** 2025-10-26 was the last one; the next is 2026-10-25:
   ```bash
   day 2025/10/26 | jq -r '.systemMetrics[0].dataPoints[].timestamp' | sed -n '1,10p'; day 2025/10/26 | jq '[.systemMetrics[0].dataPoints[]] | length'
   ```
4. **How long does the token live with `persistUserSession: true`?**
   ```bash
   jq -r .exp <<<"$AUTH"; date -u +%FT%TZ
   ```
5. **Is `panelData` present for past days?**
   ```bash
   day 2025/6/1 | jq '[.systemMetrics[].panelData[]?] | length'
   ```

## Development

```bash
uv venv -p 3.14 .venv && . .venv/bin/activate
uv pip install -r requirements_test.txt
pytest
```

CI runs hassfest, HACS validation and pytest (see `.github/workflows/validate.yml`).

## License

MIT
