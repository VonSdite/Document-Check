import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, Response, g, jsonify, stream_with_context

from app.bootstrap.asgi import create_asgi_app
from app.identity.models import UserIdentity
from app.persistence.connection import close_db, get_db
from app.persistence.defaults import seed_defaults
from app.persistence.permissions import register_subject, replace_subject_permissions
from app.persistence.schema import init_db
from app.web import register_routes
from app.web.formatting import render_markdown
from app.web.overview import _admin_totals


class DatabaseRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            DATABASE=str(Path(self.root.name) / "test.sqlite3"), WEB_THREADS=3
        )
        self.closed = []

        @self.app.teardown_appcontext
        def close_connection(error):
            db = g.get("db")
            if db is not None:
                db.execute("SELECT 1").fetchone()
                close_db(error)
                self.closed.append((db, threading.get_ident()))

        with patch("app.bootstrap.factory.create_app", return_value=self.app):
            self.adapter = create_asgi_app()

    def tearDown(self):
        self.adapter.executor.shutdown(wait=True)
        self.root.cleanup()

    async def exchange(self, path, *, ip="127.0.0.1"):
        events = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(event):
            events.append(event)

        await self.adapter(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "query_string": b"",
                "server": ("127.0.0.1", 31945),
                "client": (ip, 50000),
                "headers": [(b"host", b"localhost")],
            },
            receive,
            send,
        )
        status = next(e["status"] for e in events if e["type"] == "http.response.start")
        body = b"".join(
            e.get("body", b"") for e in events if e["type"] == "http.response.body"
        )
        return status, body

    def test_concurrent_requests_isolate_inherited_context_and_close_connections(self):
        connections = []
        barrier = threading.Barrier(3)

        @self.app.get("/probe/<name>")
        def probe(name):
            initial_marker = g.get("scope_marker")
            g.scope_marker = name
            db = get_db()
            row = db.execute("SELECT label FROM probe").fetchone()
            connections.append((db, threading.get_ident()))
            barrier.wait(timeout=5)
            return jsonify(
                initial_marker=initial_marker, marker=g.scope_marker, label=row["label"]
            )

        async def exchange_all():
            return await asyncio.gather(
                *(self.exchange(f"/probe/{n}") for n in range(6))
            )

        parent_thread = threading.get_ident()
        with self.app.app_context():
            parent = get_db()
            parent.execute("CREATE TABLE probe(label TEXT)")
            parent.execute("INSERT INTO probe VALUES ('shared-data')")
            parent.commit()
            g.scope_marker = "parent"
            results = asyncio.run(exchange_all())
            self.assertEqual([status for status, _ in results], [200] * 6)
            for n, (_, body) in enumerate(results):
                self.assertEqual(
                    json.loads(body),
                    {"initial_marker": None, "marker": str(n), "label": "shared-data"},
                )
            self.assertIs(get_db(), parent)
            self.assertEqual(g.scope_marker, "parent")
            self.assertEqual(len({id(db) for db, _ in connections}), 6)
            self.assertEqual(len({thread for _, thread in connections}), 3)
            self.assertTrue(all(thread != parent_thread for _, thread in connections))
            self.assertCountEqual(self.closed, connections)
            self.assertEqual(
                parent.execute("SELECT label FROM probe").fetchone()["label"],
                "shared-data",
            )
        self.assertCountEqual(self.closed, [*connections, (parent, parent_thread)])

    def test_task_lists_query_own_connection_and_resolve_request_permissions(self):
        root = Path(self.root.name)
        self.app.template_folder = str(
            Path(__file__).resolve().parents[1] / "app/web/templates"
        )
        self.app.config.update(
            SECRET_KEY="isolated-database-test",
            ADMIN_URL="/test-console",
            ROOT_DIR=root,
            UPLOAD_FOLDER=str(root / "uploads"),
            AUTH={"mode": "ip"},
            NETWORK={"proxy_mode": "direct", "proxy": "", "ssl_verify": False},
        )
        with self.app.app_context():
            init_db()
            seed_defaults()
            register_subject("ip:127.0.0.1")
            replace_subject_permissions("ip:127.0.0.1", {"tasks.view_all"})
        register_routes(self.app)
        self.app.add_template_filter(render_markdown, "markdown")

        async def load_lists():
            return [await self.exchange("/"), await self.exchange("/", ip="192.0.2.1")]

        with self.app.app_context():
            parent = get_db()
            identity = UserIdentity("ip:127.0.0.1", "", "ip", "127.0.0.1")
            g.user_identity = identity
            g.user_permissions = frozenset({"tasks.view_all"})
            with patch(
                "app.web.task_lists._admin_totals", wraps=_admin_totals
            ) as totals:
                results = asyncio.run(load_lists())
            self.assertEqual([status for status, _ in results], [200, 200])
            totals.assert_called_once()
            self.assertIs(get_db(), parent)
            self.assertIs(g.user_identity, identity)
            self.assertEqual(
                parent.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )

    def test_stream_keeps_own_connection_until_body_is_closed(self):
        connections = []
        body_closed = []

        @self.app.get("/stream")
        def stream():
            db = get_db()
            g.scope_marker = "stream"
            connections.append((db, threading.get_ident()))

            @stream_with_context
            def generate():
                try:
                    for _ in range(2):
                        self.assertIs(get_db(), db)
                        self.assertEqual(db.execute("SELECT 1").fetchone()[0], 1)
                        self.assertEqual(self.closed, [])
                        yield g.scope_marker
                finally:
                    body_closed.append(threading.get_ident())

            return Response(generate())

        with self.app.app_context():
            parent = get_db()
            g.scope_marker = "parent"
            status, body = asyncio.run(self.exchange("/stream"))
            self.assertEqual(status, 200)
            self.assertEqual(body, b"streamstream")
            self.assertIs(get_db(), parent)
            self.assertEqual(g.scope_marker, "parent")
            self.assertCountEqual(self.closed, connections)
            self.assertEqual(body_closed, [connections[0][1]])

    def test_stream_error_closes_connection_in_its_request_thread(self):
        connections = []

        @self.app.get("/broken-stream")
        def broken_stream():
            db = get_db()
            connections.append((db, threading.get_ident()))

            @stream_with_context
            def generate():
                db.execute("SELECT 1").fetchone()
                yield "started"
                raise RuntimeError("响应生成失败")

            return Response(generate())

        with self.app.app_context():
            parent = get_db()
            with self.assertRaisesRegex(RuntimeError, "响应生成失败"):
                asyncio.run(self.exchange("/broken-stream"))
            self.assertIs(get_db(), parent)
            self.assertCountEqual(self.closed, connections)


if __name__ == "__main__":
    unittest.main()
