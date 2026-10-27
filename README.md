# windows-audio-listener

A small, read-only Reddit listener for per-app volume and output device settings
that reset after a reboot, USB reconnect, Windows update, or game relaunch.
It runs on my PC
using Python, PRAW, one Reddit OAuth client, and a local SQLite file.

## What it does

- Immediately on startup, then every 15 minutes, fetches the newest 100 posts
  from each subreddit in `config.json`, sequentially through one PRAW client.
- Keeps a post when its title **or** text body contains a configured keyword
  phrase. Matching uses case-insensitive literal substrings, not regular
  expressions. Comments are not searched for keywords.
- Takes one comment snapshot for each new matching post, requesting the newest
  comments with a limit of 100. It saves the initial comment tree returned by
  Reddit and discards `MoreComments` placeholders without extra expansion
  requests. A large thread can have omitted comments; `comments_truncated`
  records whether placeholders were omitted. This is not a complete archive.
- Stores the post's ID, Reddit discussion URL, author, creation time, title,
  body, comments, subreddit, matched phrases, and snapshot time. Each saved
  comment includes its ID, URL, author, creation time, body, and parent ID.
  Times are UTC Unix seconds; deleted authors are stored as `null`.
- Deduplicates by post ID in SQLite, including across restarts. Once saved,
  a post and its comments are not refreshed. A failed comment fetch can be
  retried on a later poll if the post is still in the newest listing.
- Prints a digest of newly saved matches with titles, URLs, matching phrases,
  and saved comment counts. It also reports when there are no new matches.
- Deletes stored entries older than 30 days, including their comments, based
  on the post's Reddit creation time. Already-expired posts are never saved.
  Cleanup runs on startup and at the start and end of each poll, even when
  a subreddit is unavailable. SQLite secure deletion is enabled.

The newest-post limit is intentionally bounded. More than 100 new posts between
polls, downtime, or an unavailable subreddit can cause posts to be missed. The
listener skips failed subreddits and continues with the rest. It does not run
parallel requests or catch up missed polling slots in a burst.

## What it does not do

It **never submits posts, comments, votes, or messages**. It does not reply,
edit Reddit content, subscribe to communities, moderate, or send notifications
to another service. It has no posting commands, account login, user password,
or refresh token. PRAW is explicitly set to read-only mode and uses
application-only OAuth. The OAuth token request itself is an HTTP POST; Reddit
content is only read. The HTTP transport also blocks methods other than GET,
except for that OAuth token request.

**Any replies to Redditors are posted manually by me from my personal account,
u/JustFound9999Silver.** This program does not draft, schedule, or post them.

## Subreddits and phrases

The exact subreddit list in `config.json` is:

- r/Windows11
- r/Windows10
- r/WindowsHelp
- r/techsupport
- r/pcgaming
- r/buildapc
- r/headphones
- r/audio
- r/obs
- r/Twitch
- r/VoiceMeeter

The exact keyword phrase list in `config.json` is:

- `volume resets`
- `volume keeps resetting`
- `output device keeps changing`
- `default device keeps changing`
- `audio device switches`
- `audio output keeps switching`
- `app volume after restart`
- `per-app volume`
- `per app volume`
- `sound settings reset`
- `volume mixer resets`
- `goes back to speakers`
- `switches to monitor speakers`
- `forgets my headset`
- `app volume and device preferences`
- `EarTrumpet`

These phrases target one problem: per-app volume and output device settings
that reset. Matching is literal; generic audio problems alone do not match.

Edit these lists in `config.json` and restart. Use subreddit names without `r/`.
`post_limit` accepts 1–100; `sqlite_path` is relative to the config file.

## Request volume and rate limiting

Expected volume is **about 1,100 requests a day, using one OAuth client** for one
continuously running instance. With the 11 configured subreddits, 96 polls
per day × 11 listings = **1,056 listing requests/day**. About 20 newly matched
posts/day would add about 20 initial comment-tree requests; periodic OAuth
token renewals add a small number more (roughly 24/day), for about 1,100 total.

This is a workload estimate, not a fixed request quota: actual matching volume,
failures, retries, and config changes affect the total. No requests expand
`MoreComments`, and saved post IDs do not trigger repeat comment fetches. PRAW's
built-in rate limiting and normal retry handling remain enabled and respect
Reddit's rate-limit headers. There is no custom rate-limit bypass, client
rotation, or extra OAuth client.

The exact user agent is:

```text
windows-audio-listener/0.1 by u/JustFound9999Silver
```

## Local data and retention

**Collected data stays on my PC and is deleted after 30 days.** The default file
is `data/listener.sqlite3`; the listener does not upload or sync this file,
export data, run a server, or use analytics. Reddit receives the API requests
needed to read the selected public content. PRAW update checks are disabled.
The repository contains source and examples, not collected Reddit data.

Deletion runs on the schedule above, so expiry is enforced on the next cleanup
(normally within 15 minutes). While the PC or listener is stopped, no code can
delete files; expired records are removed before fetching on the next start.
To clean up without Reddit credentials or network access, run:

```powershell
.\.venv\Scripts\python.exe listener.py --purge-only
```

Choose a local directory outside cloud-sync folders to keep the file on the PC.
Copies, backups, and saved terminal output are outside the listener's retention
cleanup. Snapshots do not track later Reddit edits or deletions; they expire
under the same 30-day rule.

## Run on Windows

Requires Python 3.11 or newer and the credentials for one Reddit OAuth
application with access to the public Reddit API.

```powershell
git clone https://github.com/OMGLookItsGavo/windows-audio-listener.git
cd windows-audio-listener
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally with the application's `REDDIT_CLIENT_ID` and
`REDDIT_CLIENT_SECRET`. Keep these private. No Reddit username, password, or
user authorization is required. Environment variables with those names, if
set, take precedence over `.env`.

```powershell
# One poll, then exit:
.\.venv\Scripts\python.exe listener.py --once

# Keep running; Ctrl+C stops it:
.\.venv\Scripts\python.exe listener.py
```

Run one instance. The loop runs only while the process and PC are awake; it
does not install a Windows service or scheduled task. Optional `--config PATH`
and `--env-file PATH` select different local files. Defaults are beside
`listener.py`, regardless of the shell's current directory.

Only `.env.example` is committed. `.env`, local databases, `data/`, logs, and
the virtual environment are ignored by Git.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use synthetic posts and an intercepted PRAW HTTP transport, without
contacting Reddit or requiring credentials. They check matching, persistence,
one-time comment snapshots, read-only OAuth requests, cleanup, and errors.

## License

[MIT](LICENSE).
