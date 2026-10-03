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
4. **The Odds API (implemented and scheduled):** re-checked 2026-10-02, the free Starter plan
   is **500 credits a month for all sports and markets** (an earlier note here said 25
   calls/day for NBA/MLB only; that was wrong or has changed). Paid plans start at US$30/month
   for 20,000 credits. A `/odds` call costs `markets x regions` credits, and nothing when it
   returns no events. `/sports` and `/sports/{key}/events` are free. Archive every odds
   observation with its retrieval timestamp—using closing odds discovered after an event in a
   backtest would leak future information. Requests go to `https://api.the-odds-api.com/v4`
   as `GET /sports/{sport_key}/odds/`, authenticated with an `apiKey` **query parameter**
   (not a header), and `regions` is required — see `THE_ODDS_API_REGIONS`.
   Sources: <https://the-odds-api.com/#get-access> and
   <https://the-odds-api.com/liveapi/guides/v4/>
5. **API-Sports / API-Football (implemented and scheduled for soccer):** 100 free requests a
   day *per sport API* with one key. What the free plan actually allows, found live on
   2026-10-03 (the probe's `usable` verdict only read page 1, so it missed the first two):
   - `/odds?date=` reads current-season odds but refuses `page` above 3: 30 games a query,
     out of ~380 priced on a Sunday.
   - `/odds?league=&season=` is refused for the current season ("try from 2022 to 2024").
   - `/odds?fixture=` reads one current-season game, all nine books including Pinnacle.
   - `/fixtures?date=` returns every game of the day (~800) in one request.
   - Some leagues carry no odds on any plan (`coverage.odds` false): the Champions League,
     Europa League, Portugal, Turkey, Belgium, Serie B, 2. Bundesliga, Ligue 2 and others.
     The Odds API's focus list covers the Champions League instead.

   So the job spends, per day: one fixtures call, three date pages (30 games), then one call
   per not-yet-started game in `API_SPORTS_LEAGUES` (priority order, kick-off order within a
   league) until the 90/day ledger budget is spent. Today is collected before tomorrow. Bet
   id 1 is Match Winner (confirmed live). Hockey, basketball and the other sport APIs have
   their own 100/day each and are not yet probed.
   Source: <https://www.api-football.com/news/post/how-to-get-started-with-api-football-the-complete-beginners-guide>
6. **ClubElo (not implemented):** <http://clubelo.com/API> publishes free win/draw/loss
   probabilities for upcoming European and South American club matches, lower divisions
   included. It is a rating model, weaker than a market price, and states no terms of use:
   ask its author before collecting it.

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
- API-Sports (optional, for the probe): <https://dashboard.api-football.com/register> →
  `API_SPORTS_KEY`

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

Two more odds-bearing importers were added on 2026-09-29: `football-data-extra`
(`football_data_uk_extra_matches`, 16 countries) and `tennis-data` (`tennis_data_matches`,
ATP/WTA 2013+). Re-checked the same day:

- Football-Data.co.uk and Tennis-Data.co.uk (same operator) now restrict use to **private
  individuals**, excluding commercial or data-training products built with automated
  bots/scrapers/AI, and their `robots.txt` blocks AI crawlers. Run these importers yourself
  for private research; an agent should not fetch them.
- Football-Data flags its Pinnacle odds after 2025-07-23 as unreliable, and no longer uses
  them in its average and maximum odds. Do not treat post-2025-07-23 `PS*` columns as a
  sharp-market benchmark.
- Tennis-Data serves files from an opaque directory that has moved before. If the importer
  gets 404s, take the current path from <http://www.tennis-data.co.uk/alldata.php> and update
  `TENNIS_DATA_URL`.

## Scheduler ownership and quotas

`sports_betting` owns provider selection, cadence, retries, and quota policy. The sibling
`data-lake` directory remains passive private storage; it should not know which application
wants which sports. Jobs are single-writer and run every six hours:

- football-data.org: two date requests/run, paced at least 7 seconds apart versus 10/min free.
- BALLDONTLIE: two dates for each of three free sports, paced 13 seconds apart versus 5/min.
- TheSportsDB: two dates per configured sport, conservatively paced 2 seconds apart.
- The Odds API: a monthly credit budget (`THE_ODDS_API_MONTHLY_BUDGET`, default 450 of the
  free 500) held in the persistent ledger, so restarts cannot reset it. Each run lists
  in-season sports for free, keeps the focus leagues (`THE_ODDS_API_SPORTS`, priority order,
  globs allowed), and pays only for those with a game inside `THE_ODDS_API_LOOKAHEAD_HOURS`.
  Stalest league first, at most an even share of the credits left this month
  (`sports_betting/odds_plan.py`). The provider's own `x-requests-remaining` also stops the
  job 25 credits short of zero. Last-refresh times live in `logs/odds-api-refresh.json`.
- API-Sports football: today then tomorrow (`API_SPORTS_DAYS`), each once per UTC day, at
  least 7 seconds apart (free plan: 10/min), claiming the persistent daily ledger
  (`API_SPORTS_DAILY_BUDGET`, default 90 of 100) before every request. Odds are written as
  they arrive, so a budget that runs out mid-day keeps what it collected; that day stays due,
  and running out is reported in the job's detail rather than as a failure. Refresh times
  live in `logs/api-sports-refresh.json`.

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

## Mise-o-jeu+ is not a collection source

Checked 2026-10-02. Mise-o-jeu+ (`miseojeuplus.espacejeux.com/sports`) is the only book this
project's bets can be placed on, so its prices are the ones a recommendation must clear. It
publishes **no data feed, API, or historical odds archive.**

The page is an SG Digital (OpenBet) sportsbook front end. Its odds come from an undocumented,
unauthenticated JSON service (`content.mojp-sgdigital-jel.com/content-service/api/v1/q/...`,
e.g. `drilldown-tree`) behind Cloudflare. `robots.txt` allows everything, but that is not
permission, and two things rule out a scheduled collector:

- Loto-Québec's general Conditions of Use, which the Mise-o-jeu+ footer links and which name
  `miseojeu.lotoquebec.com` and Espacejeux as part of the "Portal", say in §7: *"Except for the
  purposes of navigating the Internet, or unless otherwise indicated, it is strictly prohibited
  to copy, redistribute, reproduce, republish, store on any medium, retransmit or modify the
  information contained on the Portal and Accounts, or to make public or commercial use
  thereof in any way whatsoever."* Archiving its odds to Parquet is storing them on a medium.
- The Loto-Québec pages load Radware (perfdrive/ShieldSquare) bot management. A plain HTTP
  client gets a challenge page. Collecting anyway would mean working around an access control.

Do not add a Mise-o-jeu+ provider, scraper, or browser-automation job unless Loto-Québec gives
written permission. Ask through the Customer Service contact in §14 of the Conditions of Use.
Until then:

- **Train on other sources covering the same events.** Use the free bulk importers above plus
  the scheduled providers. An outcome model learns from results and features, not from this
  book's prices. Other books' odds remain a fair proxy for the market's implied probability.
- **Apply Mise-o-jeu+ prices only at decision time, in the operator's own browser.** The
  value overlay below does this. Expect Mise-o-jeu+ margins to differ from the archived books',
  so set the edge threshold against the price actually offered, never against an archived
  consensus price.
- **Scope collection to what Mise-o-jeu+ offers.** On 2026-10-02 its sport tree included
  NHL and European hockey, NFL and CFL, wide soccer coverage (EPL, Spain, Italy, Germany,
  France, MLS, and others), MLB, NBA and many basketball leagues, ATP/WTA/Challenger tennis,
  golf, MMA, boxing, F1/NASCAR, esports, and smaller sports. `sports-betting
  overlay-coverage` measures which of the leagues the operator actually browses get priced.

### The value overlay (operator's choice, 2026-10-02)

The operator chose a read-only browser overlay over typing prices in by hand. It is a grey
area, not a cleared use: it reads the page's content while the operator browses, which is
arguably still "navigating", but Loto-Québec has not said so. Its constraints keep it as close
to plain browsing as possible. They are tested in `tests/test_overlay.py`; keep them:

| Constraint | How it holds |
| --- | --- |
| No extra traffic to the sportsbook | `capture.js` reads only responses the page already requested; it never calls `fetch` itself |
| Odds queries only | The URL pattern allows the content-service event lists, never bet, account, payment or identity services |
| No odds stored | Verdicts live in tab memory; the extension has no `storage` permission. The service writes one file, `logs/overlay-coverage.json`: counts per day, sport and league, with no event IDs, teams or prices |
| No wagering | The extension never touches the bet slip, login or account; bets stay manual |
| Local only | The service binds `127.0.0.1`; the extension's only host permission is that port |

The page's sportsbook routes `fetch` through an XHR polyfill that requests a `blob`, so
`capture.js` hooks XHR as well. Another script on the page also wraps `fetch` and XHR after
the extension does. If the overlay stops seeing odds after a site update, check those two
things first.

The proof of concept's fair line comes from the archived snapshots of both odds sources
(`sports_betting/overlay/lines.py`): two-way moneylines, and three-way win/draw/loss for soccer
(Mise-o-jeu+'s `MR` market). Each book's margin is removed. When Pinnacle prices the game its
line is used alone (the panel says "Pinnacle"); otherwise the books are averaged. When both
sources price the same game, the Pinnacle line wins, then the newer one. Replace it with
model probabilities once a model exists.
