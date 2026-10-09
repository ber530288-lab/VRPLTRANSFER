# PitchX Transfer Market Bot

## Setup
1. https://discord.com/developers/applications → New Application → Bot → copy token.
   Enable **Server Members Intent**. Invite with scopes `bot` + `applications.commands`.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`, fill in token (+ GUILD_ID, LEAGUE_LOGO_URL).
4. `python bot.py`

## First steps in your server
- `/setup #transactions`
- `/team_add name:Elite Manchester United tier:D-Tier`  (logo_url optional, add later with `/team_logo`)
- `/sign`, `/transfer`, `/loan`, `/release` send the player an **Accept / Decline** DM. Nothing changes until they accept; then the result embed posts in the transactions channel. Offers expire after 48h.
- Every 14 days the bot DMs all rostered players a league reminder, rotating: Who You're Playing Next → Rivals Making Moves → Weekly Pulse. Test with `/reminder_now`.
- `/matchday gameweek:3 home:... away:...` → DMs every rostered player a brief
- `/teams`, `/roster`, `/profile` for everyone

Staff commands require the **Manage Server** permission.

## Hosting on a panel host (upload-and-run style)
1. Upload everything in this folder (bot.py, main.py, requirements.txt) to your server's files.
2. Set the startup file to `bot.py` (or `main.py`). Python 3.10+ is needed.
3. Install requirements: most panels do this automatically from requirements.txt,
   otherwise run `pip install -r requirements.txt` in the console.
4. Add variables in the host's Startup/Environment tab (or upload a `.env` file):
   DISCORD_TOKEN, GUILD_ID, LEAGUE_LOGO_URL, PROFILE_URL
5. Start the bot. Keep `pitchx.db` in place - it holds your teams and rosters.
   Back it up before reinstalling or wiping the server.

## Troubleshooting
- **Commands show twice**: set `GUILD_ID` in your variables, restart once. The bot removes the duplicate copies on startup.
- **Staff commands**: usable by Administrators, anyone with Manage Server, the server owner, or the role set with `/staffrole`. Everyone can see the commands; non-staff just get a "Staff only" message.
- **Errors**: any failure now replies with the error name, and the full traceback prints in the host console.
- **Bot can't post**: give it View Channel, Send Messages and Embed Links in the transactions channel.

## League system (schedule, results, records)
1. `/setchannel` for **Matchdays** and **Results** (and Transactions). Also run `/timezone America/New_York` (or yours).
2. Build the schedule: `/schedule_generate tier:D-Tier start:2026-10-12 20:00 days_between:7 legs:2`
   (round-robin for every team in that tier), or add games one by one with `/schedule_add`.
3. **Matchday is automatic**: 24h before kickoff (`MATCHDAY_LEAD_HOURS`) the bot DMs each rostered player a brief with
   their live league position, form, head-to-head and kickoff time, posts a public card in the matchday channel,
   and pings anyone whose DMs are closed there. `/matchday` re-sends one manually.
4. After the game: `/result fixture home_score away_score` (winner and table update automatically and the result
   posts in the results channel). Also `/forfeit`, `/result_add` (unscheduled games), `/result_remove` (undo).
5. Everyone: `/standings`, `/fixtures`, `/results`, `/nextmatch`, `/record`, `/h2h`, `/freeagents`.
6. Staff: `/schedule_move`, `/schedule_remove`, `/schedule_clear`, `/brief` (text of the matchday brief).

Players must allow DMs from server members (Server > Privacy Settings) to receive briefs by DM.

## Team managers (so teams can sign their own players)
- Staff run `/manager_add team:@user` for each team owner/captain. `/manager_remove` and `/managers` to undo/list.
- Managers can use `/sign`, `/transfer` and `/loan` **for their own team only** (leave the team box empty and the bot
  uses it automatically). The player still gets an Accept / Decline DM, and the manager is told their answer.
- `/release`, results, schedule and setup stay staff-only.

## Logos
Logo links must be permanent **direct image links** (end in .png/.jpg). Upload to imgur.com or postimages.org and use
*Copy image address*. Discord attachment links expire, so the bot refuses them. `/team_logo` and `/team_add` now test the
link before saving. Run **/health** any time to test every logo, the channels and the bot's permissions.
Set `/setchannel Bot logs` to get command errors posted in a staff channel.

## Economy, stats and player cards
- Every team starts with **$100,000,000** (`START_BUDGET`). `/budget` shows budgets, `/budget_set` (staff) changes one.
- `/transfer player fee` needs a fee (`5000000`, `5m`, `750k`). The buyer must afford it, and it must be at least the
  player's market value (`MIN_FEE_PERCENT`). When the player accepts, the buyer pays and the selling team receives it.
  `/loan` takes an optional fee. `/sign` (free agents) is free.
- `/result fixture home_score away_score scorers assists clean_sheets`: tag players with @ and put a number after a name
  for several (`@tung 2 @bob`). The bot checks the numbers against the score (no more goals than the team scored, assists
  can't exceed goals, clean sheets only if the opponent scored 0). Nothing is saved if a check fails.
- Player market value: $500k base, +$600k per goal ⚽, +$350k per assist 🎯, +$400k per clean sheet 🧤, half rate past
  $6M, capped at $20M. Rank points: 2 per goal/assist/clean sheet, 10 points per rank step.
- `/profile` is a player card (team logo, value, rank, stats, trophies). `/trophy_give` and `/trophy_remove` manage the cabinet.
- `/table` draws the league table as an image with every team's logo, visible only to you.
- `/schedule_shift tier new_start` moves a whole schedule (fixes a wrong start date).

## Trophies
- **/make_trophy name image** (admins only: Administrator, Manage Server or the owner): upload the trophy image (or paste a
  direct link). The bot stores its own copy, so it never expires like Discord links do.
- **/trophy_give player trophy** (staff) awards it: posts the trophy with its logo in the results channel and shows it in the
  player's `/profile`, in a trophy shelf image directly under the "Player trophy cabinet" text (×N if won several times).
- `/trophy_remove`, `/trophy_delete` (admins), `/trophies` (list with images).
- Trophies given as plain text before this update get linked automatically when you create a trophy with the same name.

## Updating the bot WITHOUT losing anything
All teams, logos, budgets, rosters, fixtures, results, stats, trophies and channel settings live in **pitchx.db**.
Updating the code never changes that file, and the bot only ever *adds* missing columns/tables.

**To update:** stop the bot, replace only `bot.py`, `main.py` and `requirements.txt`, start it again.
Do **not** delete or re-upload `pitchx.db`, `.env` or the `backups` folder, and don't use the host's "reinstall / wipe" button.

Safety nets (automatic):
1. **Backup at every start**, and every 6 hours, into the `backups` folder (newest 30 kept).
2. **Auto-restore:** if the bot starts with an empty database but a backup exists, it restores the newest one by itself.
3. **Discord copy:** run `/setchannel Backups #private-admin-channel` and the bot posts the database file there about once a day,
   so even if the whole host is erased your data is still in Discord.
4. `/backup` (admins) sends you the file now; `/restore database:<file>` (admins) loads one back. Restoring first saves the current data as a backup.
5. On startup the console prints how many teams/players/fixtures were loaded, and `/health` shows where the data file is.

If the data ever looks reset after an update, it usually means the bot was started from a *new folder* (new empty database).
Fix: set `DB_PATH` to one fixed location in your variables, or run `/restore` with your latest backup file.

## Uploads, divisions, banners and transfer history (latest update)
- **Image uploads, no links:** `/team_add`, `/team_logo`, `/make_division`, `/division_logo`, `/make_trophy` all have an `image`
  box: just upload the picture. The bot keeps its own permanent copy (links still work as a fallback). Teams that only had a logo
  link are converted to stored copies automatically on the first start.
- **Divisions:** admins run `/make_division name` (optionally upload a logo). `/team_add name division` puts a team in one.
  Existing tiers were turned into divisions automatically. `/divisions`, `/division_logo`, `/division_delete`.
  The division logo shows on `/table` and `/standings`.
- **Logos next to names:** results, matchday briefs/announcements, `/nextmatch` and `/h2h` now show a banner:
  `[RMA logo] RMA  1 – 1  BARCA [BARCA logo]` (`VS` before the game).
- **Transfers:** the price paid becomes the player's market value (bought for $100M = worth $100M; later goals/assists add on top).
  The public post says who bought whom and for how much, and `/budget` shows spent, received, and who each team bought/sold.
- **Name:** embeds say NAPXR. Change it any time with `/league_name` (admins).

## "Here we go" images, team roles, free agents
- Every accepted transfer posts a stadium image: the player's Discord photo in the middle, the new club's crest on the tifo behind it,
  and `[old club] >>> [new club]` along the bottom. Transfers say **HERE WE GO!**, free-agent signings say **OFFICIAL**, loans say **LOAN DEAL**.
  Admins can use their own stadium photo with `/transfer_background` (no upload = default). `/herewego_preview` shows one without making a transfer.
- **Team roles:** on sign / transfer / loan / release the bot finds an EXISTING role whose name contains the team name
  (e.g. `NAPXR | RMA` for RMA; roles with words like Manager/Owner/Captain are ignored), gives it, and removes the old team's role.
  It never creates roles. The bot needs **Manage Roles** and its own role must sit ABOVE the team roles. `/sync_roles` (admins) gives
  everyone on a roster their role in one go; `/health` lists teams with no matching role.
- **Signing raises value:** a free agent who signs for a team gains `SIGN_VALUE_BONUS` ($500k by default). A transfer sets value to the price paid.
- **/freeagents** lists EVERY member without a club (not just released players), 20 per page with ◀ ▶ buttons, with an optional `role` filter.
- **/table** is faster: logos are stored copies (no downloads), fonts are cached, and an unchanged table is reused instantly.

## Manager dashboard (`/dashboard`)
Managers (and staff) run `/dashboard` and get a private control panel for their team (only they see it). Pick a section from the menu:
- **Overview**: league position, record, form, budget, squad value, next match, last result, top performers, pending offers.
- **Squad**: every player with value, goals, assists and clean sheets.
- **Fixtures**: next 5 games and last 5 results.
- **Transfers & budget**: spent, received, who you bought and sold.
- **Offers**: offers waiting for a player's answer (cancel one from the menu) and recent answers.
- **Scouting**: opponent analysis of your next opponent, or any team you choose: record, form, attack/defence ranking, best players, danger man, head to head and a game plan.
Buttons: **Sign**, **Transfer** (asks for the fee), **Loan**, **Refresh**, and **Release** (staff only). They use the same rules as the slash commands.
Managers only see their own team; staff can open any team with `/dashboard team:...`.

## If a transfer announcement doesn't appear
The bot needs **View Channel, Send Messages, Embed Links and Attach Files** in the transactions channel. Check with `/health`,
or run `/herewego_preview ... post:True` to post a test. The bot now always posts something: if Discord refuses the image it posts the text
version, and the staff member who made the offer gets a DM saying what went wrong. If no transactions channel is set it posts where the offer was made.

## Why data could be lost on update, and the automatic protection
All teams, signings, budgets, fixtures and results live in ONE file: `pitchx.db`. The bot's code never deletes it. It only disappears
if the host replaces/wipes the folder, reinstalls, or the bot is started from a new folder (a new empty file is created).
Protection that now runs by itself:
1. **Off-host copy in Discord:** at every start and every 6 hours the bot posts `pitchx.db` into a private channel `#pitchx-backups`
   (it creates it, hidden from @everyone, if it has Manage Channels; otherwise make that channel yourself, or set `BACKUP_CHANNEL_ID`). The newest 30 are kept.
2. **Automatic restore:** if the bot ever starts with an EMPTY database, it downloads the newest backup from that channel and restores it, then DMs the server owner.
3. If it starts empty and finds nothing to restore, it DMs the owner exactly why (and the file path), instead of failing silently.
4. `/health` shows the data file path, when it last changed, and when the last off-host backup was posted. The console prints how many teams were loaded at start.
5. Local copies in the `backups` folder, `/backup` and `/restore` still work too.
To check right now: run `/health` and confirm it shows an off-host backup.

## Images
Text drawn onto images (Here we go, banners, tables, trophy shelf) now uses only plain characters ("-", "...", "x"), so you never see a square box,
whatever fonts your host has. Anything the font can't draw is removed automatically.
