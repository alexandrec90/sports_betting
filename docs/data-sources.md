# Sports data sources and Québec boundary

Checked 2026-08-04. Prices, coverage, terms, and law can change; verify them again before
depending on a provider, and preserve the provider name and raw response snapshot in the lake.

## Recommended sequence

1. **TheSportsDB v1 (implemented and scheduled):** no signup; its shared free key is published in the
   documentation. It covers many sports and exposes day schedules/results, but the free day
   endpoint returns only three events. That makes it good for bootstrapping the normalized
   pipeline, not a production-quality complete history. Premium is currently US$9/month.
   Source: <https://www.thesportsdb.com/docs_api_guide>
2. **football-data.org (implemented and scheduled):** free forever for 12 competitions,
   delayed scores/schedules, fixtures, and tables at 10 calls/minute. It has a documented v4
   API and a clean upgrade path; use this before relying on scraped soccer sites.
   Sources: <https://www.football-data.org/pricing> and
   <https://www.football-data.org/documentation/quickstart>
3. **BALLDONTLIE (implemented and scheduled):** free games endpoints for NBA, NFL, and MLB
   at 5 requests/minute after signup. Paid sport plans currently start at US$9.99/month;
   deeper statistics, injuries, and odds are paid features. **EPL is not free:** the
   `/epl/v2/matches` endpoint requires ALL-STAR or higher (re-checked 2026-08-08), so `epl`
   is not in the default `BALLDONTLIE_SPORTS`. Free EPL access is limited to teams, rosters,
   players, and standings — none of which is a fixture feed.
   Sources: <https://www.balldontlie.io/account/>, <https://docs.balldontlie.io/>, and
   <https://epl.balldontlie.io/>
4. **The Odds API (implemented and scheduled):** the current free plan is 25 calls/day and
   only NBA/MLB moneylines; US$29/month adds 25 sports, spreads, and totals. Archive every odds
   observation with its retrieval timestamp—using closing odds discovered after an event in a
   backtest would leak future information. Requests go to `https://api.the-odds-api.com/v4`
   as `GET /sports/{sport_key}/odds/`, authenticated with an `apiKey` **query parameter**
   (not a header), and `regions` is required — see `THE_ODDS_API_REGIONS`.
   Sources: <https://the-odds-api.com/#get-access> and
   <https://the-odds-api.com/liveapi/guides/v4/>

League-operated NHL/MLB endpoints and ESPN endpoints can be useful for research, but their
public interfaces are undocumented or do not offer a clear data licence/SLA. Treat them as
fragile fallbacks, cache politely, and obtain permission before commercial use. Sportradar and
Sportmonks become relevant only after coverage/reliability requirements justify their cost.

## API keys to create

TheSportsDB needs no signup while using its published `123` key. Create the other three free
accounts, then put the keys in the checkout's uncommitted `.env`:

- football-data.org: <https://www.football-data.org/client/register> → `FOOTBALL_DATA_API_KEY`
- BALLDONTLIE: <https://app.balldontlie.io/signup> → `BALLDONTLIE_API_KEY`
- The Odds API: <https://the-odds-api.com/#get-access> → `THE_ODDS_API_KEY`

The scheduler remains useful before these are set: those jobs report `skipped`, and
TheSportsDB continues collecting. Keys belong only in `.env`; they are removed from raw
payload fields and never included in provider error messages.

football-data.org's registration terms restrict a key to one application and require visible
attribution (“Football data provided by the Football-Data.org API”). Add that attribution when
the application gains a UI, and re-check its retention/cancellation terms before publication.

## Free historical training data

These bulk datasets are much better for initial model training than slowly reconstructing
history from live endpoints:

- **Soccer results and historical bookmaker odds:** Football-Data.co.uk publishes 31 seasons
  of results, 26 seasons of odds, and 26 seasons of match statistics as free CSV/Excel files,
  updated at least twice weekly: <https://www.football-data.co.uk/data.php>. This is the best
  free starting point for an odds-aware model. It is unrelated to football-data.org.
- **Soccer event-level data:** Hudl StatsBomb Open Data includes full event data for selected
  competitions and historical tournaments, including the 2015/16 Big Five leagues:
  <https://statsbomb.com/what-we-do/hub/free-data/>. Check its attribution terms per dataset.
- **NFL:** nflverse offers cleaned play-by-play back to 1999 plus schedules, rosters, and other
  releases, with in-season updates: <https://nflverse.nflverse.com/>.
- **NHL:** MoneyPuck provides game-level files and roughly two million historical shots from
  2007 onward. It is free for non-commercial use with attribution; commercial use needs
  permission: <https://moneypuck.com/data.htm>.

Do not merge these directly into `sports_events`: each has its own schema, licence, natural
key, and point-in-time semantics. Add one bulk importer per dataset and retain source/version
metadata. The recommended next importer is Football-Data.co.uk because it includes both match
outcomes and historical pre-match odds—the target variable and the market baseline in one
download.

All four importers are now available through `sports-betting bulk-import`. They require no API
keys and write separate datasets: `football_data_uk_matches`, `statsbomb_matches` plus
`statsbomb_events`, `nflverse_play_by_play`, and `moneypuck_shots` plus
`moneypuck_team_games`. Original files, licences, hashes, and immutable publisher revisions are
retained alongside query-ready Parquet. See the README for commands. MoneyPuck remains limited
to non-commercial use unless written permission is obtained.

## Scheduler ownership and quotas

`sports_betting` owns provider selection, cadence, retries, and quota policy. The sibling
`data-lake` directory remains passive private storage; it should not know which application
wants which sports. Jobs are single-writer and run every six hours:

- football-data.org: two date requests/run, paced at least 7 seconds apart versus 10/min free.
- BALLDONTLIE: two dates for each of three free sports, paced 13 seconds apart versus 5/min.
- TheSportsDB: two dates per configured sport, conservatively paced 2 seconds apart.
- The Odds API: NBA and MLB once/run, eight planned calls/day. A persistent 20/day safety
  budget stays below the provider's 25/day free limit even across process restarts.

## Why Betfair is not the execution target

Betfair's current general terms list **Canada** as a prohibited territory and forbid bypassing
access restrictions. Its developer program also requires a KYC-verified Betfair account before
API licensing. Do not create a Betfair account integration or attempt a location workaround.

- Betfair terms: <https://support.betfair.com/app/answers/detail/betfair-general-terms-and-conditions/>
- Betfair API licensing: <https://support.developer.betfair.com/hc/en-us/articles/360002464152-Which-API-Licence-Do-I-Require>

Loto-Québec states that `lotoquebec.com` is the only legal online gaming website in Québec and
offers sports betting through Mise-o-jeu. No supported public wagering API was identified.
Therefore this codebase remains data/research-only. Before adding execution, obtain Québec legal
advice and written operator confirmation that programmatic wagering is authorized; availability
of a website is not permission to automate it.

- Loto-Québec online gaming statement:
  <https://societe.lotoquebec.com/en/offering/online-gaming>
- Mise-o-jeu: <https://miseojeu.lotoquebec.com/en/home>
