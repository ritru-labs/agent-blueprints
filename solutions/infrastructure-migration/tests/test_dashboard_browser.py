"""Real Chromium → authenticated HTTP API → PostgreSQL → LangGraph → reviewer campaign."""

import io
import json
import os
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from test_service import AUDIENCE, ISSUER, access_token
from test_service import signing as signing
from test_service_postgres import scoped_inventory
from test_service_postgres import stores as stores

from infra_migration.runner import DockerRunner
from infra_migration.service.api import create_app
from infra_migration.service.auth import JwtVerifier
from infra_migration.service.runtime import PreparationWorker, Registry, TenantRuntime

pytestmark = pytest.mark.skipif(
    not os.environ.get("INFRA_TEST_POSTGRES_DSN") or os.environ.get("INFRA_BROWSER_TESTS") != "1",
    reason="Real PostgreSQL and Chromium campaign not configured",
)


def test_dashboard_full_browser_review_download_rejection_and_cancellation(
    stores, signing, tmp_path
):
    store = stores[0]
    inventory = scoped_inventory(store)
    data = inventory.model_dump(mode="json")
    data["resources"][0]["configuration"]["tags"]["note"] = (
        '</code><img src=x onerror="window.__injected=true">'
    )
    inventory_path = tmp_path / "snapshot.json"
    inventory_path.write_text(json.dumps(data))
    registry = Registry(
        {
            store.tenant_id: TenantRuntime(
                store,
                frozenset([inventory.scope.account_id]),
                frozenset(inventory.scope.regions),
                tmp_path / "artifacts",
            )
        }
    )
    app = create_app(JwtVerifier(ISSUER, AUDIENCE, signing[1]), registry)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    runner = (
        DockerRunner(os.environ["INFRA_RUNNER_IMAGE"])
        if os.environ.get("INFRA_RUNNER_IMAGE")
        else None
    )
    worker = PreparationWorker(registry, runner=runner)
    screenshots = Path(
        os.environ.get("INFRA_BROWSER_ARTIFACTS", str(tmp_path / "browser-evidence"))
    )
    screenshots.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1440, "height": 1120}, accept_downloads=True
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(address)
            expect(
                page.get_by_role("heading", name="Migration workspace", exact=True)
            ).to_be_visible()
            page.screenshot(path=str(screenshots / "dashboard-disconnected.png"), full_page=True)

            def connect(subject):
                page.get_by_label("Organization ID", exact=True).fill(str(store.tenant_id))
                page.get_by_label("Access token", exact=True).fill(
                    access_token(signing[0], subject)
                )
                page.get_by_role("button", name="Connect workspace", exact=True).click()
                expect(page.get_by_role("button", name="Disconnect", exact=True)).to_be_visible()
                expect(page.locator("#access-token")).to_have_value("")

            connect("requester")
            page.get_by_label("Choose an inventory snapshot", exact=False).set_input_files(
                str(inventory_path)
            )
            page.get_by_role("button", name="Select supported", exact=True).click()
            expect(page.locator("#selection-count")).to_have_text("2 selected")
            interrupted = False

            def lose_first_submission_response(route):
                nonlocal interrupted
                if route.request.method == "POST" and not interrupted:
                    interrupted = True
                    response = route.fetch()
                    assert response.status == 202
                    route.abort()
                else:
                    route.continue_()

            page.route("**/runs", lose_first_submission_response)
            page.get_by_role("button", name="Prepare package →", exact=True).click()
            expect(page.locator("#notice")).to_contain_text("Request outcome unclear")
            page.get_by_role("button", name="Prepare package →", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Queued")
            with store.connection() as db:
                assert db.execute("SELECT count(*) AS n FROM service_jobs").fetchone()["n"] == 1
            page.unroute("**/runs", lose_first_submission_response)
            worker.once(store.tenant_id)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Awaiting review")
            expect(page.locator("#code-content")).to_contain_text("aws.ec2.Vpc")
            expect(page.get_by_role("button", name="Approve package", exact=True)).to_be_hidden()
            assert page.evaluate("window.__injected") is None
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            if runner:
                expect(page.locator("#checks")).to_contain_text("Passed in isolated runner")
            run = page.locator("#detail-run").inner_text().split(" ")[1]
            page.screenshot(path=str(screenshots / "dashboard-assessor.png"), full_page=True)
            page.get_by_role("button", name="Disconnect", exact=True).click()
            expect(page.locator("#code-content")).to_have_text("")
            connect("reviewer")
            page.get_by_role("button", name="Open preparation " + run, exact=True).click()
            expect(page.get_by_role("button", name="Approve package", exact=True)).to_be_visible()
            page.get_by_label("Generated file", exact=True).select_option("expected-inputs.json")
            expect(page.locator("#code-content")).to_contain_text("10.0.0.0/16")
            assert page.evaluate("window.__injected") is None
            with page.expect_download() as download_info:
                page.get_by_role("button", name="Download package ↓", exact=True).click()
            downloaded = download_info.value
            assert downloaded.suggested_filename == f"migration-{run}.zip"
            with zipfile.ZipFile(io.BytesIO(Path(downloaded.path()).read_bytes())) as package:
                assert "index.ts" in package.namelist() and "import.json" in package.namelist()
            page.screenshot(path=str(screenshots / "dashboard-reviewer.png"), full_page=True)
            page.get_by_role("button", name="Approve package", exact=True).click()
            page.get_by_label(
                "I reviewed the generated files, validation results, and blockers."
            ).check()
            page.get_by_role("button", name="Confirm approval", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Review queued")
            worker.once(store.tenant_id)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Reviewed · execution blocked")
            expect(page.get_by_text("Live execution disabled", exact=True)).to_be_visible()
            page.screenshot(path=str(screenshots / "dashboard-reviewed.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(screenshots / "dashboard-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1440, "height": 1120})
            page.get_by_role("button", name="Disconnect", exact=True).click()
            connect("requester")
            page.get_by_label("Choose an inventory snapshot", exact=False).set_input_files(
                str(inventory_path)
            )
            page.get_by_role("button", name="Select supported", exact=True).click()
            page.get_by_role("button", name="Prepare package →", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Queued")
            worker.once(store.tenant_id)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Awaiting review")
            second_run = page.locator("#detail-run").inner_text().split(" ")[1]
            page.get_by_role("button", name="Disconnect", exact=True).click()
            connect("reviewer")
            page.get_by_role("button", name="Open preparation " + second_run, exact=True).click()
            page.get_by_role("button", name="Reject package", exact=True).click()
            page.get_by_label(
                "I reviewed the generated files, validation results, and blockers."
            ).check()
            page.get_by_role("button", name="Confirm rejection", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Review queued")
            worker.once(store.tenant_id)
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Rejected")
            page.get_by_role("button", name="Disconnect", exact=True).click()
            connect("requester")
            page.get_by_label("Choose an inventory snapshot", exact=False).set_input_files(
                str(inventory_path)
            )
            page.get_by_role("button", name="Select supported", exact=True).click()
            page.get_by_role("button", name="Prepare package →", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Queued")
            page.get_by_role("button", name="Cancel preparation", exact=True).click()
            expect(page.locator("#detail-status")).to_have_text("Cancelled")
            page.reload()
            expect(page.get_by_role("button", name="Connect workspace", exact=True)).to_be_visible()
            expect(page.locator("#access-token")).to_have_value("")
            assert not errors, errors
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
