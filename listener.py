"""Read public Reddit posts and keep matching snapshots on this computer."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import praw
from praw.exceptions import PRAWException
from praw.models import Submission
from prawcore import Requestor
from prawcore.exceptions import PrawcoreException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

USER_AGENT = "windows-audio-listener/0.1 by u/JustFound9999Silver"
POLL_SECONDS = 15 * 60
RETENTION_SECONDS = 30 * 24 * 60 * 60
COMMENT_LIMIT = 100
ROOT = Path(__file__).resolve().parent


class ReadOnlyRequestor(Requestor):
    """Keep the exact user agent and permit only reads plus OAuth token requests."""

    def request(self, method: str, url: str, **kwargs):
        target = urlsplit(url)
        oauth = (
            method.upper() == "POST"
            and target.scheme == "https"
            and target.netloc == "www.reddit.com"
            and target.path == "/api/v1/access_token"
        )
        if method.upper() != "GET" and not oauth:
            raise RuntimeError("Only Reddit reads and OAuth token requests are allowed")
        # PRAW normally appends its version. Override just the header, keeping
        # its request transport, rate limiting, and retries intact.
        kwargs["headers"] = {**(kwargs.get("headers") or {}), "User-Agent": USER_AGENT}
        return super().request(method, url, **kwargs)


class Config(BaseModel):
    """Public settings, loaded from JSON; paths are relative to that JSON file."""

    model_config = ConfigDict(extra="forbid", strict=True)

    subreddits: list[str] = Field(min_length=1)
    keywords: list[str] = Field(min_length=1)
    post_limit: int = Field(default=100, ge=1, le=100)
    sqlite_path: str = "data/listener.sqlite3"

    @field_validator("subreddits", "keywords")
    @classmethod
    def clean_list(cls, values: list[str], info: ValidationInfo) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not value for value in cleaned):
            raise ValueError("list entries must not be blank")
        if info.field_name == "subreddits" and any(
            not re.fullmatch(r"[a-zA-Z0-9_]{2,21}", value) for value in cleaned
        ):
            raise ValueError("use individual subreddit names without r/ or +")
        return cleaned

    @field_validator("sqlite_path")
    @classmethod
    def local_database(cls, value: str) -> str:
        if not value.strip() or value == ":memory:":
            raise ValueError("sqlite_path must name a persistent local file")
        return value


class Credentials(BaseSettings):
    """Only application credentials; no Reddit user password or refresh token."""

    model_config = SettingsConfigDict(
        env_prefix="REDDIT_",
        env_file_encoding="utf-8-sig",
        extra="ignore",
        hide_input_in_errors=True,
    )

    client_id: SecretStr
    client_secret: SecretStr

    @field_validator("client_id", "client_secret")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value().strip()
        if not secret or secret.startswith("replace_me"):
            raise ValueError("supply your OAuth application credentials in .env")
        return SecretStr(secret)


def create_reddit(credentials: Credentials) -> praw.Reddit:
    """Use one application-only client and PRAW's unmodified rate limiter."""
    reddit = praw.Reddit(
        client_id=credentials.client_id.get_secret_value(),
        client_secret=credentials.client_secret.get_secret_value(),
        user_agent=USER_AGENT,
        username=None,
        password=None,
        refresh_token=None,
        check_for_updates=False,
        requestor_class=ReadOnlyRequestor,
    )
    reddit.read_only = True
    return reddit


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    # Wipe deleted cell content; use a deleted rollback journal rather than WAL.
    db.execute("PRAGMA journal_mode = DELETE")
    db.execute("PRAGMA secure_delete = ON")
    with db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                subreddit TEXT NOT NULL,
                url TEXT NOT NULL,
                author TEXT,
                created_utc REAL NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                comments TEXT NOT NULL,
                comments_truncated INTEGER NOT NULL,
                matched_keywords TEXT NOT NULL,
                fetched_utc REAL NOT NULL
            )"""
        )
        db.execute("CREATE INDEX IF NOT EXISTS posts_created ON posts(created_utc)")
    return db


def purge_old(db: sqlite3.Connection, now: float) -> int:
    """Hard-delete the entire snapshot when the post is older than 30 days."""
    with db:
        return db.execute(
            "DELETE FROM posts WHERE created_utc < ?", (now - RETENTION_SECONDS,)
        ).rowcount


def match_keywords(title: str, body: str, keywords: list[str]) -> list[str]:
    """Case-insensitive literal substrings in either field, never in comments."""
    title, body = title.casefold(), body.casefold()
    return [
        phrase
        for phrase in keywords
        if phrase.casefold() in title or phrase.casefold() in body
    ]


def comment_snapshot(submission: Submission, cutoff: float) -> tuple[list[dict], bool]:
    """Fetch the initial comment tree once; never expand 'more comments'."""
    submission.comment_limit = COMMENT_LIMIT
    submission.comment_sort = "new"
    forest = submission.comments
    omitted = forest.replace_more(limit=0)
    comments = [
        {
            "id": comment.id,
            "url": "https://www.reddit.com" + comment.permalink,
            "author": str(comment.author) if comment.author is not None else None,
            "created_utc": comment.created_utc,
            "body": comment.body,
            "parent_id": comment.parent_id,
        }
        for comment in forest.list()
        if comment.created_utc >= cutoff
    ]
    return comments, bool(omitted)


def store_match(
    db: sqlite3.Connection,
    submission: Submission,
    subreddit: str,
    keywords: list[str],
    comments: list[dict],
    truncated: bool,
    now: float,
) -> bool:
    with db:
        result = db.execute(
            """INSERT OR IGNORE INTO posts
               (id, subreddit, url, author, created_utc, title, body, comments,
                comments_truncated, matched_keywords, fetched_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                submission.id,
                subreddit,
                "https://www.reddit.com" + submission.permalink,
                str(submission.author) if submission.author is not None else None,
                submission.created_utc,
                submission.title,
                submission.selftext,
                json.dumps(comments, ensure_ascii=False),
                int(truncated),
                json.dumps(keywords, ensure_ascii=False),
                now,
            ),
        )
    return result.rowcount == 1


def terminal_text(text: str) -> str:
    """Keep untrusted Reddit text on one line and strip terminal controls."""
    return " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())


def poll_once(
    reddit: praw.Reddit,
    db: sqlite3.Connection,
    config: Config,
    output: TextIO | None = None,
    errors: TextIO | None = None,
) -> int:
    output = sys.stdout if output is None else output
    errors = sys.stderr if errors is None else errors
    started = time.time()
    deleted = purge_old(db, started)
    stamp = datetime.fromtimestamp(started, UTC).isoformat(timespec="seconds")
    print(f"[{stamp}] New matches (removed {deleted} expired snapshots):", file=output)
    count = 0
    for name in config.subreddits:
        try:
            for post in reddit.subreddit(name).new(limit=config.post_limit):
                now = time.time()
                if post.created_utc < now - RETENTION_SECONDS:
                    continue
                if db.execute(
                    "SELECT 1 FROM posts WHERE id = ?", (post.id,)
                ).fetchone():
                    continue
                keywords = match_keywords(post.title, post.selftext, config.keywords)
                if not keywords:
                    continue
                try:
                    comments, truncated = comment_snapshot(
                        post, now - RETENTION_SECONDS
                    )
                except (PRAWException, PrawcoreException) as exc:
                    print(
                        f"r/{name}: comment snapshot unavailable ({type(exc).__name__}); "
                        "will retry if still in the next listing.",
                        file=errors,
                    )
                    continue
                if post.created_utc < time.time() - RETENTION_SECONDS:
                    continue
                if store_match(
                    db, post, name, keywords, comments, truncated, time.time()
                ):
                    count += 1
                    print(f"- r/{name}: {terminal_text(post.title)}", file=output)
                    print(f"  https://www.reddit.com{post.permalink}", file=output)
                    print(
                        f"  Keywords: {', '.join(terminal_text(k) for k in keywords)}; "
                        f"{len(comments)} comments saved"
                        + (" (partial tree)" if truncated else ""),
                        file=output,
                    )
        except (PRAWException, PrawcoreException) as exc:
            # Exception text can contain request details; report its class only.
            print(
                f"r/{name}: unavailable ({type(exc).__name__}); continuing.",
                file=errors,
            )
    purge_old(db, time.time())
    print(f"{count} new match(es).", file=output, flush=True)
    return count


def run(
    reddit: praw.Reddit, db: sqlite3.Connection, config: Config, once: bool
) -> None:
    """Run sequentially every 15 minutes; skip missed slots instead of catching up."""
    while True:
        started = time.monotonic()
        poll_once(reddit, db, config)
        if once:
            return
        elapsed = time.monotonic() - started
        time.sleep(
            max(0, POLL_SECONDS - elapsed) if elapsed < POLL_SECONDS else POLL_SECONDS
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--purge-only", action="store_true", help="delete expired data without Reddit"
    )
    args = parser.parse_args(argv)
    try:
        config = Config.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        db_path = args.config.resolve().parent / config.sqlite_path
        with closing(open_database(db_path)) as db:
            removed = purge_old(db, time.time())
            if args.purge_only:
                print(f"Removed {removed} expired snapshots.")
                return 0
            credentials = Credentials(_env_file=args.env_file)
            with create_reddit(credentials) as reddit:
                run(reddit, db, config, args.once)
    except KeyboardInterrupt:
        print("Listener stopped.")
    except ValidationError:
        print(
            "Invalid settings: check config.json and the OAuth client values in .env.",
            file=sys.stderr,
        )
        return 1
    except (OSError, sqlite3.Error, PRAWException, PrawcoreException) as exc:
        print(
            f"Listener stopped ({type(exc).__name__}). Check local files and settings.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
