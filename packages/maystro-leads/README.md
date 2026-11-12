# Maystro lead collector

The collector searches public Meta Ad Library ads in Tunisia, scores advertisers, and enriches qualified leads with details from their business pages. The defaults retain the supplied script's four keywords, three-day lookback, and minimum score of 3 out of 5.

Each qualified lead goes into Twenty as a Company and an Opportunity linked to that company. The Opportunity uses the workspace's default stage, which is **New** in a standard workspace. Its linked note records the Facebook page, qualification signals, score, followers, and any public contact details found. The SQLite database deduplicates source pages by Facebook ID, domain, or phone. The importer reuses a Company with the same website URL and can resume a partial import without resetting an existing Opportunity's stage.

## Enable in Coolify

1. In Twenty, open **Settings → APIs & Webhooks → API Keys** and create a key for the Maystro workspace. Give its role access to read and create Companies, Opportunities, Notes, and Note Targets.
2. Set `LEADS_TWENTY_API_KEY` as a runtime environment variable on the Maystro Coolify application. The collector uses the existing `SERVER_URL` as its Twenty API origin.
3. Redeploy the application from `docker-compose.coolify.yml`. The collector runs at 08:00 Tunisia time. If you enable it later in the day, it runs once after startup. The schedule, browser profile, and deduplication state persist in `lead-collector-data`.

Set `LEADS_CONFIG_JSON` as a runtime variable to override the defaults. For example, `{"max_ads_per_keyword":50,"min_score":4}` reduces the scan size and raises the score threshold. The original French and Arabic keywords remain the default unless you supply a `keywords` array.

## Run manually

Set the Compose environment variables and run these from the repository root. Use `--mode full` for a scan without the date filter.

```bash
docker compose -f docker-compose.coolify.yml run --rm --no-deps lead-collector python leads_daily.py run --dry-run
docker compose -f docker-compose.coolify.yml run --rm --no-deps lead-collector python leads_daily.py push
```

The dry run previews scoring without writing to Twenty or changing the lead database. `push` retries pending leads without starting a new browser collection. Collector output is available in the service logs. A run that reads zero cards does not fabricate records. Meta's public page layout and availability determine what the collector can read.

Run the offline checks with:

```bash
python packages/maystro-leads/leads_daily.py selftest
python -m unittest discover -s packages/maystro-leads -v
```
