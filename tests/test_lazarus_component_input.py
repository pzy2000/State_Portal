from pathlib import Path
import json
import re
import pytest


def test_invalid_public_state_remains_observable_and_can_be_corrected():
    from warptest.lazarus_p2p_only_generated_runtime import validate_observed_start
    task = {"id":"lzp-jupyterlab-18464-debugger-zero-variable-detail", "benchmark":"jupyterlab"}
    playwright = pytest.importorskip('playwright.sync_api')
    source_path = Path(__file__).resolve().parents[1] / 'warptest/lazarus_p2p_only_components.js'
    source = re.sub(r'^import .*;\n', '', source_path.read_text(), flags=re.MULTILINE)
    # Only candidate classes are substituted; execute the production host and
    # its real public Apply-state handler in a browser.
    classes = '''class VariablesModel { constructor() { this.scopes = []; } }
      class VariablesBodyTree {
        constructor({model}) { this.model=model; this.node=document.createElement('div'); }
        update() { this.node.textContent=JSON.stringify(this.model.scopes); }
        dispose() { this.node.remove(); }
      }
      class CommandRegistry {}
      const Widget = {attach: (widget, target) => target.append(widget.node)};
      window.p2pThemes = {light: '', dark: '', high_contrast: ''};'''
    html = '<style id="candidate-theme"></style><div id="surface"></div><textarea id="state-json"></textarea><span id="error"></span>'
    html += ''.join(f'<button id="{name}">{name}</button>' for name in ['apply-state','add-variable','apply-class','append-button'])
    valid = {'component':'variables','theme':'light','props':{'scope':'Locals','variables':[{'name':'total','value':'7'}]}}
    invalid = [
        {'component':'variables','scopes':[{'name':'Locals','variables':[{'name':'total','value':7}]}]},
        {},
        {'component':'variables','theme':'light','props':{'scope':'Locals'}},
        {'component':'unknown','theme':'light','props':{}},
    ]
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            page.add_script_tag(content=classes+'\n'+source)
            for payload in invalid:
                page.evaluate('value => window.p2pRestore(value)', valid)
                page.locator('#state-json').fill(json.dumps(payload))
                page.locator('#apply-state').click()
                page.wait_for_function('document.querySelector("#error").textContent.length > 0')
                observed = page.evaluate('window.p2pObserve()')
                assert observed['state'] == {'configured':False}
                assert observed['visible'] is False
                assert not validate_observed_start(task, observed)
                assert page.locator('#surface').inner_text() == ''
                page.locator('#state-json').fill(json.dumps(valid))
                page.locator('#apply-state').click()
                page.wait_for_function('document.querySelector("#error").textContent === ""')
                assert page.evaluate('window.p2pSnapshot()') == valid
                restored_observation = page.evaluate('window.p2pObserve()')
                assert restored_observation['visible'] is True
                assert validate_observed_start(task, restored_observation)
        finally:
            browser.close()
