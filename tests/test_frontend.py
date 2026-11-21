import json
import unittest
from pathlib import Path

from flask import Flask

from app.web.frontend import frontend_assets


class FrontendAssetsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.app = Flask(__name__, static_folder=str(self.root / "app/web/static"))

    def test_manifest_assets_and_dynamic_chunks_are_served(self):
        manifest = json.loads(
            (self.root / "app/web/static/frontend/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        client = self.app.test_client()
        for chunk in manifest.values():
            for filename in [chunk["file"], *chunk.get("css", [])]:
                with self.subTest(filename=filename):
                    response = client.get(f"/static/frontend/{filename}")
                    self.assertEqual(response.status_code, 200)
                    response.close()
            for dependency in chunk.get("imports", []) + chunk.get(
                "dynamicImports", []
            ):
                self.assertIn(dependency, manifest)

    def test_assets_follow_script_prefix_and_refresh_manifest_cache(self):
        with self.app.test_request_context(
            "/", environ_overrides={"SCRIPT_NAME": "/document-check"}
        ):
            assets = frontend_assets()
            self.assertTrue(
                assets["script"].startswith("/document-check/static/frontend/assets/")
            )
            self.assertTrue(assets["styles"])
            for stylesheet in assets["styles"]:
                self.assertTrue(
                    stylesheet.startswith("/document-check/static/frontend/")
                )
            self.assertEqual(frontend_assets(), assets)
            self.app.extensions["frontend_manifest"] = (0, {})
            self.assertEqual(frontend_assets(), assets)

    def test_business_network_calls_belong_to_api_layer(self):
        for path in (self.root / "frontend/src").rglob("*"):
            if path.suffix not in {".js", ".ts", ".vue"}:
                continue
            if "api" in path.relative_to(self.root / "frontend/src").parts:
                continue
            source = path.read_text(encoding="utf-8")
            if "fetch(" in source:
                self.assertIn("request as fetch", source, str(path))


if __name__ == "__main__":
    unittest.main()
