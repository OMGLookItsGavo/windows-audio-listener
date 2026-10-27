"""Behavior checks with temporary SQLite files and synthetic Reddit data."""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
from prawcore.exceptions import PrawcoreException
from pydantic import ValidationError

import listener

SUBREDDITS = [
    "Windows11",
    "Windows10",
    "WindowsHelp",
    "techsupport",
    "pcgaming",
    "buildapc",
    "headphones",
    "audio",
    "obs",
    "Twitch",
    "VoiceMeeter",
]
KEYWORDS = [
    "volume resets",
    "volume keeps resetting",
    "output device keeps changing",
    "default device keeps changing",
    "audio device switches",
    "audio output keeps switching",
    "app volume after restart",
    "per-app volume",
    "per app volume",
    "sound settings reset",
    "volume mixer resets",
    "goes back to speakers",
    "switches to monitor speakers",
    "forgets my headset",
    "app volume and device preferences",
    "EarTrumpet",
]


def comment(id="c1", created=None, author="reader"):
    return SimpleNamespace(
        id=id,
        permalink=f"/r/Windows11/comments/abc/title/{id}/",
        author=author,
        created_utc=time.time() if created is None else created,
        body="Synthetic comment",
        parent_id="t3_abc",
    )


class FakeForest:
    def __init__(self, comments, truncated=False):
        self.comments = comments
        self.truncated = truncated

    def replace_more(self, *, limit):
        if limit != 0:
            raise AssertionError("comment expansions must not be requested")
        return [object()] if self.truncated else []

    def list(self):
        return self.comments


class FakePost:
    def __init__(
        self,
        id="abc",
        title="Per-app volume resets",
        body="",
        created=None,
        comments=None,
        error=False,
        truncated=False,
    ):
        self.id, self.title, self.selftext = id, title, body
        self.author = None
        self.created_utc = time.time() if created is None else created
        self.permalink = f"/r/Windows11/comments/{id}/title/"
        self.comment_reads = 0
        self.forest = FakeForest(
            [comment()] if comments is None else comments, truncated
        )
        self.error = error

    @property
    def comments(self):
        self.comment_reads += 1
        if self.error:
            raise PrawcoreException("synthetic failure with private request details")
        return self.forest


class FakeReddit:
    def __init__(self, subreddits):
        self.subreddits = subreddits
        self.calls = []

    def subreddit(self, name):
        def newest(*, limit):
            self.calls.append((name, limit))
            posts = self.subreddits[name]
            if isinstance(posts, Exception):
                raise posts
            return iter(posts)

        return SimpleNamespace(new=newest)


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "listener.sqlite3"
        self.db = listener.open_database(self.path)
        self.addCleanup(self.db.close)
        self.config = listener.Config(subreddits=["Windows11"], keywords=KEYWORDS)
        self.output, self.errors = io.StringIO(), io.StringIO()

    def poll(self, reddit, config=None):
        return listener.poll_once(
            reddit,
            self.db,
            config or self.config,
            self.output,
            self.errors,
        )

    def test_repository_config_and_readme_use_exact_user_lists(self):
        config = json.loads((listener.ROOT / "config.json").read_text())
        self.assertEqual(config["subreddits"], SUBREDDITS)
        self.assertEqual(config["keywords"], KEYWORDS)
        readme = (listener.ROOT / "README.md").read_text()
        subreddit_lines = [
            line[4:] for line in readme.splitlines() if line.startswith("- r/")
        ]
        phrase_lines = [
            line[3:-1] for line in readme.splitlines() if line.startswith("- `")
        ]
        self.assertEqual(subreddit_lines, SUBREDDITS)
        self.assertEqual(phrase_lines, KEYWORDS)

    def test_literal_case_insensitive_matching_anywhere_in_either_field(self):
        self.assertEqual(
            listener.match_keywords("xxEARTRUMPETxx", "", KEYWORDS), ["EarTrumpet"]
        )
        self.assertEqual(
            listener.match_keywords("unrelated", "My PER APP VOLUME broke", KEYWORDS),
            ["per app volume"],
        )
        self.assertEqual(
            listener.match_keywords("no sound crackling delay", "microphone", KEYWORDS),
            [],
        )
        self.assertEqual(listener.match_keywords("volume", " resets", KEYWORDS), [])
        self.assertEqual(listener.match_keywords("a.*b", "", ["a.*b"]), ["a.*b"])

    def test_persists_required_post_and_comment_fields_and_digest(self):
        post = FakePost(body="Settings disappear after reboot", truncated=True)
        self.assertEqual(self.poll(FakeReddit({"Windows11": [post]})), 1)
        row = self.db.execute(
            "SELECT id,url,author,created_utc,title,body,comments,comments_truncated FROM posts"
        ).fetchone()
        self.assertEqual(
            row[:3], ("abc", "https://www.reddit.com" + post.permalink, None)
        )
        self.assertEqual(row[3:6], (post.created_utc, post.title, post.selftext))
        saved = json.loads(row[6])[0]
        self.assertEqual(
            set(saved), {"id", "url", "author", "created_utc", "body", "parent_id"}
        )
        self.assertEqual(saved["body"], "Synthetic comment")
        self.assertEqual(row[7], 1)
        self.assertIn("1 new match(es)", self.output.getvalue())
        self.assertIn(post.permalink, self.output.getvalue())
        self.assertIn("partial tree", self.output.getvalue())
        self.assertEqual(post.comment_limit, 100)
        self.assertEqual(post.comment_sort, "new")

    def test_comments_fetched_once_across_repeated_polls_and_database_reopen(self):
        post = FakePost()
        reddit = FakeReddit({"Windows11": [post, post]})
        self.assertEqual(self.poll(reddit), 1)
        self.assertEqual(self.poll(reddit), 0)
        with listener.closing(listener.open_database(self.path)) as reopened:
            self.assertEqual(
                listener.poll_once(reddit, reopened, self.config, self.output), 0
            )
        self.assertEqual(post.comment_reads, 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM posts").fetchone()[0], 1)

    def test_nonmatches_old_posts_and_comment_only_matches_are_not_fetched(self):
        posts = [
            FakePost(title="ordinary question"),
            FakePost(id="old", created=time.time() - listener.RETENTION_SECONDS - 1),
        ]
        posts[0].forest.comments[0].body = "EarTrumpet"
        self.assertEqual(self.poll(FakeReddit({"Windows11": posts})), 0)
        self.assertEqual([p.comment_reads for p in posts], [0, 0])

    def test_retention_clears_bodies_comments_and_does_not_keep_old_comments(self):
        now = time.time()
        marker = "EXPIRED_SYNTHETIC_PERSONAL_DATA_123456789"
        post = FakePost(created=now - listener.RETENTION_SECONDS - 1, body=marker)
        listener.store_match(
            self.db,
            post,
            "Windows11",
            ["volume resets"],
            [{"body": marker}],
            False,
            now,
        )
        self.assertEqual(listener.purge_old(self.db, now), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM posts").fetchone()[0], 0)
        self.assertNotIn(marker.encode(), self.path.read_bytes())
        self.assertEqual(self.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        recent = FakePost(
            comments=[
                comment(created=now - listener.RETENTION_SECONDS - 1),
                comment(id="fresh", author=None),
            ]
        )
        self.assertEqual(self.poll(FakeReddit({"Windows11": [recent]})), 1)
        comments = json.loads(
            self.db.execute("SELECT comments FROM posts").fetchone()[0]
        )
        self.assertEqual([c["id"] for c in comments], ["fresh"])
        self.assertIsNone(comments[0]["author"])

    def test_cleanup_still_runs_when_reddit_is_unavailable(self):
        post = FakePost(created=time.time() - listener.RETENTION_SECONDS - 1)
        listener.store_match(self.db, post, "Windows11", [], [], False, time.time())
        self.assertEqual(
            self.poll(FakeReddit({"Windows11": PrawcoreException("details")})), 0
        )
        self.assertEqual(self.db.execute("SELECT count(*) FROM posts").fetchone()[0], 0)
        self.assertIn("PrawcoreException", self.errors.getvalue())
        self.assertNotIn("details", self.errors.getvalue())

    def test_failed_comments_retry_later_and_other_subreddits_continue(self):
        broken, good = FakePost(error=True), FakePost(id="good")
        reddit = FakeReddit({"Windows11": [broken], "Windows10": [good]})
        config = listener.Config(
            subreddits=["Windows11", "Windows10"], keywords=KEYWORDS
        )
        self.assertEqual(self.poll(reddit, config), 1)
        broken.error = False
        self.assertEqual(self.poll(reddit, config), 1)
        self.assertEqual((broken.comment_reads, good.comment_reads), (2, 1))
        self.assertNotIn("private request", self.errors.getvalue())

    def test_cleanup_boundary_and_digest_control_characters(self):
        now = time.time()
        post = FakePost(created=now - listener.RETENTION_SECONDS)
        listener.store_match(self.db, post, "Windows11", [], [], False, now)
        self.assertEqual(listener.purge_old(self.db, now), 0)
        self.assertEqual(listener.purge_old(self.db, now + 1), 1)
        self.assertEqual(listener.terminal_text("Title\n\x1b[31m\x00x"), "Title [31mx")

    def test_validation_rejects_empty_lists_multireddits_and_outsize_listing(self):
        for overrides in (
            {"keywords": []},
            {"keywords": [" "]},
            {"subreddits": ["a+b"]},
            {"post_limit": 101},
            {"sqlite_path": ":memory:"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                listener.Config.model_validate(
                    {"subreddits": ["Windows11"], "keywords": KEYWORDS, **overrides}
                )

    def test_purge_only_requires_no_credentials_or_reddit_client(self):
        cfg = Path(self.temp.name) / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "subreddits": ["Windows11"],
                    "keywords": KEYWORDS,
                    "sqlite_path": str(self.path),
                }
            )
        )
        with (
            patch.object(listener, "create_reddit") as create,
            redirect_stdout(self.output),
        ):
            result = listener.main(["--config", str(cfg), "--purge-only"])
        self.assertEqual(result, 0)
        create.assert_not_called()

    def test_env_file_credentials_are_masked_and_invalid_values_are_not_printed(self):
        env = Path(self.temp.name) / ".env"
        env.write_text(
            "REDDIT_CLIENT_ID=synthetic-id\nREDDIT_CLIENT_SECRET=synthetic-secret\n"
        )
        with patch.dict("os.environ", {}, clear=True):
            credentials = listener.Credentials(_env_file=env)
        self.assertEqual(credentials.client_id.get_secret_value(), "synthetic-id")
        self.assertNotIn("synthetic-secret", repr(credentials))
        env.write_text(
            "REDDIT_CLIENT_ID=replace_me_bad\nREDDIT_CLIENT_SECRET=replace_me_private\n"
        )
        cfg = Path(self.temp.name) / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "subreddits": ["Windows11"],
                    "keywords": KEYWORDS,
                    "sqlite_path": str(self.path),
                }
            )
        )
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(self.errors):
            self.assertEqual(
                listener.main(["--config", str(cfg), "--env-file", str(env)]), 1
            )
        self.assertNotIn("replace_me_private", self.errors.getvalue())

    def test_loop_uses_fifteen_minute_interval_and_once_does_not_sleep(self):
        reddit = FakeReddit({"Windows11": []})
        with (
            patch.object(listener, "poll_once"),
            patch.object(listener.time, "sleep") as sleep,
        ):
            listener.run(reddit, self.db, self.config, once=True)
            sleep.assert_not_called()
        for elapsed, expected in [(12, 888), (901, 900)]:
            with (
                self.subTest(elapsed=elapsed),
                patch.object(listener, "poll_once"),
                patch.object(listener.time, "monotonic", side_effect=[0, elapsed]),
                patch.object(
                    listener.time, "sleep", side_effect=KeyboardInterrupt
                ) as sleep,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    listener.run(reddit, self.db, self.config, once=False)
                sleep.assert_called_once_with(expected)

    def test_real_praw_transport_only_requests_oauth_and_public_reads(self):
        """Exercise the actual installed PRAW and prawcore; no network is allowed."""
        now = time.time()
        post = {
            "kind": "t3",
            "data": {
                "id": "abc",
                "name": "t3_abc",
                "title": "Volume resets after a reboot",
                "selftext": "Synthetic post",
                "author": "reader",
                "created_utc": now,
                "permalink": "/r/Windows11/comments/abc/title/",
                "subreddit": "Windows11",
                "num_comments": 1,
                "is_self": True,
            },
        }
        comm = {
            "kind": "t1",
            "data": {
                "id": "c1",
                "name": "t1_c1",
                "body": "Synthetic comment",
                "author": "helper",
                "created_utc": now,
                "permalink": "/r/Windows11/comments/abc/title/c1/",
                "parent_id": "t3_abc",
                "link_id": "t3_abc",
                "replies": "",
            },
        }

        def listing(children):
            return {
                "kind": "Listing",
                "data": {"children": children, "after": None, "before": None},
            }

        calls = []

        def route(request, **kwargs):
            url = urlparse(request.url)
            calls.append((request.method, url.path))
            self.assertEqual(request.headers["User-Agent"], listener.USER_AGENT)
            if request.method == "POST" and url.path == "/api/v1/access_token":
                body = (
                    request.body.decode()
                    if isinstance(request.body, bytes)
                    else request.body
                )
                self.assertEqual(parse_qs(body), {"grant_type": ["client_credentials"]})
                payload = {
                    "access_token": "synthetic-token",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "scope": "*",
                }
            elif request.method == "GET" and url.path == "/r/Windows11/new":
                payload = listing([post])
            elif request.method == "GET" and url.path == "/comments/abc/":
                self.assertEqual(parse_qs(url.query)["limit"], ["100"])
                self.assertEqual(parse_qs(url.query)["sort"], ["new"])
                payload = [listing([post]), listing([comm])]
            else:
                self.fail(f"Unexpected Reddit operation: {request.method} {url.path}")
            response = requests.Response()
            response.status_code = 200
            response.url, response.request = request.url, request
            response.headers.update(
                {
                    "Content-Type": "application/json",
                    "X-Ratelimit-Used": "1",
                    "X-Ratelimit-Remaining": "999",
                    "X-Ratelimit-Reset": "600",
                }
            )
            response._content = json.dumps(payload).encode()
            return response

        with (
            patch.dict(
                "os.environ",
                {
                    "praw_username": "unused",
                    "praw_password": "unused",
                    "praw_refresh_token": "unused",
                },
            ),
            patch("requests.sessions.Session.send", side_effect=route),
        ):
            credentials = listener.Credentials(
                client_id="synthetic-id", client_secret="synthetic-secret"
            )
            with listener.create_reddit(credentials) as reddit:
                self.assertTrue(reddit.read_only)
                self.assertEqual(self.poll(reddit), 1, self.errors.getvalue())
                self.assertEqual(self.poll(reddit), 0)
        self.assertEqual(
            calls,
            [
                ("POST", "/api/v1/access_token"),
                ("GET", "/r/Windows11/new"),
                ("GET", "/comments/abc/"),
                ("GET", "/r/Windows11/new"),
            ],
        )

    def test_transport_rejects_content_writes_before_sending_requests(self):
        with (
            listener.closing(
                listener.ReadOnlyRequestor(user_agent=listener.USER_AGENT)
            ) as requestor,
            patch("requests.sessions.Session.send") as send,
        ):
            for method, path in [
                ("POST", "/api/comment"),
                ("POST", "/api/vote"),
                ("POST", "/api/submit"),
                ("POST", "/api/compose"),
                ("PUT", "/anything"),
                ("DELETE", "/anything"),
            ]:
                with (
                    self.subTest(method=method, path=path),
                    self.assertRaises(RuntimeError),
                ):
                    requestor.request(method, "https://oauth.reddit.com" + path)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
