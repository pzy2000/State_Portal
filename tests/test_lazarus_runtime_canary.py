"""Browser-level regressions for the provider-free canary driver."""

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from scripts.run_lazarus_p2p_only_runtime_canary import CanaryRunner
from warptest.lazarus_p2p_only_runtime import JupyterOnlineSession


def test_jupyter_canary_waits_for_delayed_workspace_frame(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    document = """<textarea id="state-json"></textarea>
        <button id="apply-state" onclick="window.applied=document.querySelector('#state-json').value">Apply</button>
        <script>window.p2pRestore = function() {};</script>"""
    encoded = json.dumps(document).replace("</", "<\\/")
    page_html = """<button id="open">Open task workspace</button><script>
        document.querySelector('#open').onclick = () => setTimeout(() => {
          const frame = document.createElement('iframe');
          frame.name = 'stateportal-p2p-task';
          frame.setAttribute('data-stateportal-p2p-task', '');
          frame.width = '600'; frame.height = '300'; frame.srcdoc = DOCUMENT;
          document.body.appendChild(frame);
        }, 1200);
        </script>""".replace("DOCUMENT", encoded)
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright.Error as error:
            pytest.skip(f"Chromium runtime unavailable: {error}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(page_html)
            session = SimpleNamespace(page=page)
            session._task_frame = lambda: JupyterOnlineSession._task_frame(session)
            runner = object.__new__(CanaryRunner)
            state = {"component": "delayed-frame-test"}
            runner.samples = {"delayed-frame": {"spec": {"target": {"state": state}}}}
            context = SimpleNamespace(key=SimpleNamespace(task_id="delayed-frame"), task={"benchmark": "jupyterlab"})
            result = runner._phase(session, None, context, tmp_path, "phase-a", 100)
            assert result.incomplete is False
            assert json.loads(session._task_frame().evaluate("window.applied")) == state
        finally:
            browser.close()


def test_inactive_jupyter_workspace_is_not_a_live_start(tmp_path: Path) -> None:
    from warptest.lazarus_p2p_only_generated_runtime import GeneratedJupyterSession
    from warptest.lazarus_p2p_only_paid_runtime import P2POnlyPaidRuntimeRunner
    playwright = pytest.importorskip('playwright.sync_api')
    document = '''<div id="surface" style="width:400px;height:180px;background:#469">Task workspace</div>
      <script>window.p2pObserve = () => ({visible:true,state:{configured:true}});</script>'''
    html = '''<div id="other">Other document tab</div><div id="panel"><iframe
      name="stateportal-p2p-task" style="width:600px;height:300px"></iframe></div>'''
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':800,'height':600})
            page.set_content(html)
            page.locator('iframe').evaluate('(node, text) => node.srcdoc = text', document)
            page.evaluate('window.__warptestStartComplete = true')
            frame = page.frame(name='stateportal-p2p-task')
            frame.wait_for_function('typeof window.p2pObserve === "function"')
            session = object.__new__(GeneratedJupyterSession)
            session.page = page
            session.last_policy_frame_id = 0
            assert P2POnlyPaidRuntimeRunner._observe(session)['visible'] is True
            page.locator('#panel').evaluate('node => node.style.display = "none"')
            # The sticky flag and iframe still exist after switching tabs.
            assert page.evaluate('window.__warptestStartComplete') is True
            assert page.frame(name='stateportal-p2p-task') is not None
            observed = P2POnlyPaidRuntimeRunner._observe(session)
            assert observed['visible'] is False
            assert observed['failure'] == 'task_workspace_not_open'
            screenshot = P2POnlyPaidRuntimeRunner._capture(session, tmp_path/'hidden.png', 'jupyterlab', allow_policy=True)
            assert screenshot.is_file()
            assert session.last_policy_frame_id == 1
            page.locator('#panel').evaluate('node => node.style.display = "block"')
            assert P2POnlyPaidRuntimeRunner._observe(session)['visible'] is True
            assert P2POnlyPaidRuntimeRunner._capture(session, tmp_path/'visible.png', 'jupyterlab').is_file()
            assert session.last_policy_frame_id == 1
            page.close()
            with pytest.raises(playwright.Error):
                P2POnlyPaidRuntimeRunner._observe(session)
        finally:
            browser.close()
