"""Sanity checks for the built mobile PWA and its server mount.

These tests never run Node: they validate the committed build output, so a
broken/refreshed build is caught before shipping.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "mobile_web" / "dist"
SERVER = ROOT / "core" / "api_server.py"


class BuildOutputTests(unittest.TestCase):
    def setUp(self):
        if not (DIST / "index.html").exists():
            self.skipTest("mobile_web/dist not built (cd mobile_web && npm run build)")

    def test_assets_are_served_under_app_base(self):
        html = (DIST / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r'src="/app/assets/[^"]+\.js"')
        self.assertRegex(html, r'href="/app/assets/[^"]+\.css"')

    def test_manifest_is_installable(self):
        manifest = json.loads((DIST / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "./")
        self.assertTrue(manifest.get("icons"))
        for icon in manifest["icons"]:
            self.assertTrue((DIST / icon["src"].lstrip("./")).exists(), icon["src"])

    def test_service_worker_is_shipped(self):
        sw = (DIST / "sw.js").read_text(encoding="utf-8")
        self.assertIn("/api/", sw)  # API responses must never be cached
        self.assertIn("caches", sw)

    def test_service_worker_handles_push(self):
        sw = (DIST / "sw.js").read_text(encoding="utf-8")
        self.assertIn('addEventListener("push"', sw)
        self.assertIn('addEventListener("notificationclick"', sw)
        self.assertIn("showNotification", sw)
        self.assertIn("openWindow", sw)
        self.assertIn("tab=approvals", sw)


class PushUiTests(unittest.TestCase):
    def test_settings_exposes_push_toggle(self):
        settings = (ROOT / "mobile_web" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")
        self.assertIn("Notifiche push", settings)
        self.assertIn("enablePush", settings)
        self.assertIn("sendTestPush", settings)

    def test_push_helpers_use_the_api_endpoints(self):
        push_ts = (ROOT / "mobile_web" / "src" / "push.ts").read_text(encoding="utf-8")
        api_ts = (ROOT / "mobile_web" / "src" / "api.ts").read_text(encoding="utf-8")
        for call in ("api.pushKey", "api.pushSubscribe", "api.pushUnsubscribe", "api.pushTest"):
            self.assertIn(call, push_ts)
        for endpoint in ("/api/push/key", "/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"):
            self.assertIn(endpoint, api_ts)

    def test_api_server_registers_push_routes(self):
        source = SERVER.read_text(encoding="utf-8")
        for route in ('"/api/push/key"', '"/api/push/subscribe"', '"/api/push/unsubscribe"', '"/api/push/test"'):
            self.assertIn(route, source)


class RunTimelineTests(unittest.TestCase):
    def test_api_server_serves_run_events(self):
        source = SERVER.read_text(encoding="utf-8")
        self.assertIn('"/api/runs/{run_id}/events"', source)
        self.assertIn("after_seq", source)

    def test_pwa_fetches_events_incrementally(self):
        api_ts = (ROOT / "mobile_web" / "src" / "api.ts").read_text(encoding="utf-8")
        screen = (ROOT / "mobile_web" / "src" / "screens" / "RunDetail.tsx").read_text(encoding="utf-8")
        self.assertIn("runEvents", api_ts)
        self.assertIn("after_seq=", api_ts)
        self.assertIn("api.runEvents", screen)
        self.assertIn("eventsCursor", screen)


class FileUiTests(unittest.TestCase):
    def test_api_server_registers_file_routes(self):
        source = SERVER.read_text(encoding="utf-8")
        for route in (
            '"/api/runs/{run_id}/files"',
            '"/api/files"',
            '"/api/files/preview"',
            '"/api/files/download"',
            '"/api/files/share"',
            '"/api/files/shared/{token}"',
        ):
            self.assertIn(route, source)

    def test_share_endpoint_is_public(self):
        source = SERVER.read_text(encoding="utf-8")
        block = source.split('"/api/files/shared/{token}"', 1)[1]
        block = block.split("async def", 1)[0]
        self.assertNotIn("Depends(verify_control)", block)

    def test_pwa_exposes_run_files(self):
        api_ts = (ROOT / "mobile_web" / "src" / "api.ts").read_text(encoding="utf-8")
        screen = (ROOT / "mobile_web" / "src" / "screens" / "RunDetail.tsx").read_text(encoding="utf-8")
        for wrapper in ("runFiles", "filePreview", "downloadFile", "shareFile"):
            self.assertIn(wrapper, api_ts)
        for call in ("api.runFiles", "api.filePreview", "api.downloadFile", "api.shareFile"):
            self.assertIn(call, screen)

    def test_pwa_types_include_file_models(self):
        types_ts = (ROOT / "mobile_web" / "src" / "types.ts").read_text(encoding="utf-8")
        for model in ("export interface RunFile", "export interface FilePreview", "export interface ShareLink"):
            self.assertIn(model, types_ts)


class UploadUiTests(unittest.TestCase):
    def test_api_server_registers_upload_routes(self):
        source = SERVER.read_text(encoding="utf-8")
        for route in (
            '"/api/files/upload"',
            '"/api/files/upload/{upload_id}"',
            '"/api/files/upload/{upload_id}/chunks/{index}"',
            '"/api/files/upload/{upload_id}/complete"',
        ):
            self.assertIn(route, source)
        self.assertIn("application/octet-stream", source)
        self.assertIn("await request.body()", source)

    def test_pwa_wraps_upload_endpoints(self):
        api_ts = (ROOT / "mobile_web" / "src" / "api.ts").read_text(encoding="utf-8")
        for wrapper in ("listFiles", "startUpload", "uploadChunk", "completeUpload", "abortUpload"):
            self.assertIn(wrapper, api_ts)
        for endpoint in (
            "/api/files/upload",
            "/chunks/",
            "/complete",
        ):
            self.assertIn(endpoint, api_ts)

    def test_upload_helper_slices_the_file(self):
        upload_ts = (ROOT / "mobile_web" / "src" / "upload.ts").read_text(encoding="utf-8")
        for call in ("api.startUpload", "api.uploadChunk", "api.completeUpload", "api.abortUpload"):
            self.assertIn(call, upload_ts)
        self.assertIn("file.slice", upload_ts)
        self.assertIn("UPLOAD_CHUNK_SIZE", upload_ts)

    def test_files_screen_browses_and_uploads(self):
        screen = (ROOT / "mobile_web" / "src" / "screens" / "Files.tsx").read_text(encoding="utf-8")
        for call in ("api.listFiles", "uploadBrowserFile", "api.filePreview", "api.downloadFile", "api.shareFile"):
            self.assertIn(call, screen)
        self.assertIn("FilePreviewCard", screen)
        self.assertIn("breadcrumb", screen)
        app = (ROOT / "mobile_web" / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("FilesScreen", app)
        self.assertIn('"files"', app)

    def test_types_include_upload_models(self):
        types_ts = (ROOT / "mobile_web" / "src" / "types.ts").read_text(encoding="utf-8")
        for model in ("export interface FileListing", "export interface UploadSession", "export interface UploadChunkStatus"):
            self.assertIn(model, types_ts)
        self.assertIn('"files"', types_ts)


class GpuUiTests(unittest.TestCase):
    def test_api_server_registers_system_status(self):
        source = SERVER.read_text(encoding="utf-8")
        self.assertIn('"/api/system/status"', source)
        self.assertIn('"/api/v1/system/status"', source)

    def test_pwa_wraps_system_status(self):
        api_ts = (ROOT / "mobile_web" / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("systemStatus", api_ts)
        self.assertIn("/api/system/status", api_ts)
        types_ts = (ROOT / "mobile_web" / "src" / "types.ts").read_text(encoding="utf-8")
        for model in ("export interface SystemStatus", "export interface GpuQueueState", "export interface GpuMetrics"):
            self.assertIn(model, types_ts)

    def test_settings_shows_gpu_queue(self):
        settings = (ROOT / "mobile_web" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")
        self.assertIn("Sistema e GPU", settings)
        self.assertIn("Coda GPU", settings)
        self.assertIn("systemStatus", settings)

    def test_chat_warns_about_the_queue(self):
        chat = (ROOT / "mobile_web" / "src" / "screens" / "Chat.tsx").read_text(encoding="utf-8")
        self.assertIn("GPU occupata", chat)
        self.assertIn("systemStatus", chat)


class ServerMountTests(unittest.TestCase):
    def test_api_server_serves_the_pwa(self):
        source = SERVER.read_text(encoding="utf-8")
        self.assertIn('app.mount("/app"', source)
        self.assertIn("mobile_web", source)
        self.assertRegex(source, re.compile(r"StaticFiles\(directory=MOBILE_WEB_DIR"))


if __name__ == "__main__":
    unittest.main()
