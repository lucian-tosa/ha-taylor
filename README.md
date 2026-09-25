# Taylor Solar for Home Assistant

A custom integration that reads the [Taylor Solar](https://taylor.solar) cloud API and imports solar production and the other energy flows into Home Assistant's long-term statistics at the hour they actually happened. You can then use them in the Energy dashboard.

A RESTful sensor would book energy at poll time, couldn't backfill, and would read Taylor's retroactive corrections as meter resets. This integration does the following instead:

- It writes Taylor's 15-minute buckets into **external statistics** at their real UTC hour.
- It **backfills** history on first setup, from 0 to 1095 days.
- On every run it **re-imports yesterday and today** to pick up Taylor's revisions, and rewrites the running sums consistently.
- It handles token refresh, rate limiting (HTTP 429) and API-version breaks (HTTP 418, raised as a repair issue).

Requires Home Assistant 2026.1 or newer. It has no dependencies beyond what Home Assistant ships.

## Installation

**HACS:**

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=lucian-tosa&repository=ha-taylor&category=integration)

The button opens this repository in HACS on your own Home Assistant, which offers to add it. You can also add it by hand: HACS → ⋮ → Custom repositories → add `https://github.com/lucian-tosa/ha-taylor` with category *Integration*. Then install *Taylor Solar* and restart Home Assistant. HACS then offers updates when new versions are released.

**Manual:** copy `custom_components/taylor_solar` into `/config/custom_components/` and restart Home Assistant.

Then add the integration:

[![Open your Home Assistant instance and start setting up Taylor Solar.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=taylor_solar)

Or go to Settings → Devices & services → Add integration → **Taylor Solar**. Enter your Taylor email and password, and choose how many days of history to import. If the account has several sites, pick one. Add the integration again for each additional site.

## Energy dashboard

Settings → Dashboards → Energy:

- **Solar production** → Add solar production → pick **Taylor ‹site› solar production**.
- **Electricity grid** → use your P1 meter (or other grid meter) sensors for grid consumption and return to grid.

With solar and grid configured, the Energy dashboard calculates your home's consumption itself (solar + grid import − grid export). You don't need a consumption value from Taylor.

The solar source is a long-term statistic (`taylor_solar:<site>_solar_production`), not an entity. It holds hourly kWh at the hour the energy was produced, including backfilled history, and the dashboard's picker lists it next to sensors. The *today* sensors below have no `state_class` on purpose. A sensor would record energy when it is polled (15–60 minutes late), couldn't backfill, and Taylor's later corrections would look like meter resets.

The statistic appears once the first hour has been imported. On first setup, the backfill runs in the background at about 8 requests per minute (Taylor allows about 10), so a year takes about 45 minutes. The *Statistics imported through* diagnostic sensor shows progress.

A `consumption` statistic is also created if Taylor reports consumption. For a gateway that only reads the inverter, it reports `null`. Taylor's grid and battery values are signed, and their sign convention hasn't been confirmed, so they are not imported yet.

## Entities

One device per site, with these entities:

| Entity | Notes |
| --- | --- |
| *Solar production today* (kWh) | Today's total so far. |
| *Consumption today* (kWh) | Only when Taylor reports consumption. |
| *Solar power* (W) | Average power over the latest 15-minute interval Taylor has reported. Attribute `bucket_start`. |
| *Panel ‹n› energy today* (kWh) | One per panel, numbered as in the Taylor app's layout. Attributes `taylor_panel_id`, `cell_string_a_wh`, `cell_string_b_wh`, `cell_string_c_wh`. |
| *Statistics imported through* (date, diagnostic) | How far the history import has got. |

Data is polled every 15 minutes. Taylor's timestamps are naive site-local times, and they are interpreted in Home Assistant's configured time zone.

## Removing

Removing the integration deletes its import cursor, but the **imported statistics stay in place**. To delete them, go to Developer tools → Statistics. If you re-add the integration, it backfills again and overwrites the same statistic IDs.

To change the backfill period, remove the integration and add it again.

## Verifying the API

A few behaviors of the Taylor API are undocumented. The code paths that depend on them are marked `TODO(verify)`. You can check them with `curl` (7.76 or newer) and `jq`. Start with this setup block. It prompts for your credentials, so the password stays out of your shell history. Each step prints its result, or the error body if a request fails:

```bash
H=(-H 'Client-Version: 1.0.0' -H 'Client-Name: end-user-api' -H 'Content-Type: application/json')
API=https://clientapi.taylor.solar
read -rp 'Taylor email: ' TAYLOR_USER; read -rsp 'Taylor password: ' TAYLOR_PASS; echo
AUTH=$(curl -sS --fail-with-body "${H[@]}" -H 'Accept: text/plain' -X POST "$API/api/authenticate" \
  -d "$(jq -n --arg u "$TAYLOR_USER" --arg p "$TAYLOR_PASS" '{userName: $u, password: $p, persistUserSession: true}')") \
  && jq '{exp, token: (.token[0:8] + "…")}' <<<"$AUTH" || echo "Login failed: $AUTH"
TOKEN=$(jq -r .token <<<"$AUTH")
SITES=$(curl -sS --fail-with-body "${H[@]}" -H "Authorization: Taylor $TOKEN" "$API/api/public/sites") \
  && jq . <<<"$SITES" || echo "Listing sites failed: $SITES"
SITE=$(jq -r '.[0].id' <<<"$SITES"); echo "Using site $SITE"
day() { curl -sS --fail-with-body "${H[@]}" -H "Authorization: Taylor $TOKEN" "$API/api/public/site/$SITE/data/$1"; }
```

When it works, it prints the token expiry with the start of the token, your sites, and the site it will use. If you see a `curl: (…)` error instead, the request never reached Taylor. Run `curl -v "$API"` to check DNS, proxy or TLS.

### What the live API returns

These were checked against a live site in September 2026 (one 3.7 kWp system without a Taylor meter or battery). The response differs from Taylor's API document:

- Each data point has an `inverterEnergyData` object instead of a `data` list of `type`/`wh` items. It holds `solarProduction` (Wh per interval), plus `consumption`, `grid`, `battery` and `balance`, which are `null` without a Taylor meter or battery. There is also a `quality` field, always 0 so far.
- Per-panel production comes per data point, in `panelEnergyData` (`id` plus cell strings A/B/C in Wh). There is no `panelData`. It is present for past days too.
- Intervals are 15 minutes (`dayDataPointDurationSeconds: 900`), and only daylight hours are reported. That means the DST fall-back hour never appears.
- Timestamps are naive site-local times. Points whose timestamp ends in `Z` are zero-filled padding for intervals without data, and are skipped. Days before installation return only padding, not a 404.

The integration accepts both this format and the one in Taylor's document.

### Still open

1. **What are the sign conventions of `grid` and `battery`?** This needs a system with a Taylor meter or battery:
   ```bash
   day 2026/6/15 | jq '[.systemMetrics[].dataPoints[].inverterEnergyData | {grid, battery, balance}] | map(select(.grid != null)) | .[0:5]'
   ```
2. **How long does the token live with `persistUserSession: true`?** Compare the `exp` from the setup block with the current time:
   ```bash
   jq -r .exp <<<"$AUTH"; date -u +%FT%TZ
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
