"""Gate 8--13 real Edge closure for JupyterLab #18518.

The authoritative defect (issue #15083) makes the client send the wrong XSRF
token: when duplicate ``_xsrf`` cookies exist at different paths the browser lists
the most specific path first, the server keeps the last value, and the client used
to take the first regular expression match.  The server then rejects the request
with "XSRF Cookies do not match Post Argument".

The observation uses real browser cookies on a routed origin: the candidate builds
a request, the intercepted endpoint reports back the ``X-XSRFToken`` header it
actually received, and the page renders that value.  Both golden frames render an
expected token directly, so they never depend on the candidate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .jupyterlab_real_edge_closure import RealEdgeClosureSpec
from .stable_protocol import ArtifactReference


_CHANGE = 18518
_EPOCH = "real-xsrf-v1"
_SERVERCONNECTION = "packages/services/lib/serverconnection.js"
_SERVICES_INDEX = "packages/services/lib/index.js"
_VARIABLES_CSS = "packages/theme-light-extension/style/variables.css"
_MARKER = "C1_LAZARUS_18518_EDGE_OBSERVATION"
_ORIGIN = "https://c1-lazarus.invalid"
_PAGE_PATH = "/lab/tree/index.html"
_SPECIFIC_TOKEN = "stale-path-token"
_ROOT_TOKEN = "server-truth-token"

_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_18518_transition/jupyterlab/18518/current-v1",
    "candidate_gate57_18518_transition",
    "1.0",
)
_GATE57_POOL = ArtifactReference(
    "candidate_gate57_pool_admission/jupyterlab/18518/current-v1",
    "candidate_gate57_pool_admission",
    "1.0",
)


def _harness_files(run: Path, tree: Path) -> Mapping[str, str]:
    services = (tree / _SERVICES_INDEX).as_posix()
    entry = f"""import {{ ServerConnection }} from {json.dumps(services)};

const display = () => document.getElementById('token-display');

window.__candidateReady = true;
window.__candidateCookie = () => document.cookie;
window.__candidateReset = () => {{
  display().textContent = '';
}};
window.__candidateRenderToken = value => {{
  display().textContent = value;
}};
window.__candidateRequest = async () => {{
  const settings = ServerConnection.makeSettings({{
    baseUrl: {json.dumps(_ORIGIN + "/")}
  }});
  const response = await ServerConnection.makeRequest(
    {json.dumps(_ORIGIN + "/api/contents")},
    {{ method: 'POST', body: '{{}}' }},
    settings
  );
  const payload = await response.json();
  display().textContent = payload.received_xsrf_token;
  return payload.received_xsrf_token;
}};
"""
    config = f"""module.exports = {{
  mode: 'production', target: 'web', devtool: false,
  context: {json.dumps(tree.as_posix())},
  entry: {json.dumps((run / "entry.js").as_posix())},
  output: {{ path: {json.dumps((run / "dist").as_posix())}, filename: 'bundle.js',
    clean: true }},
  resolve: {{
    modules: [{json.dumps((tree / "node_modules").as_posix())}], symlinks: false
  }},
  module: {{ rules: [
    {{ test: /\\.svg$/, type: 'asset/source' }},
    {{ test: /\\.(css|gif|ttf|eot|woff2?)$/, type: 'asset/source' }}
  ] }},
  optimization: {{ minimize: false }}
}};
"""
    html_document = f"""<!doctype html>
<meta charset="utf-8"><title>JupyterLab XSRF token replay</title>
<link rel="stylesheet" href="{(tree / _VARIABLES_CSS).as_uri()}">
<style>
html,body{{margin:0;width:100%;height:100%;background:#fff;
  font:16px var(--jp-ui-font-family,system-ui)}}
.panel{{padding:40px}}
.label{{color:#6a737d;margin-bottom:12px}}
#token-display{{font:22px ui-monospace,SFMono-Regular,Menlo,monospace;color:#202124}}
</style>
<div class="panel">
  <div class="label">XSRF token received by the server</div>
  <pre id="token-display"></pre>
</div>
<script src="/dist/bundle.js"></script>
"""
    playwright = (tree / "node_modules/playwright/index.js").as_posix()
    browser = f"""const fs = require('node:fs');
const path = require('node:path');
const {{ chromium }} = require({json.dumps(playwright)});
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const ORIGIN = {json.dumps(_ORIGIN)};
const PAGE_PATH = {json.dumps(_PAGE_PATH)};
(async () => {{
  const browser = await chromium.launch({{ channel: 'msedge', headless: true }});
  const context = await browser.newContext({{
    viewport: {{ width: 720, height: 240 }}, deviceScaleFactor: 1
  }});
  // The browser lists the most specific cookie path first; the server keeps the
  // last value, so only the last value is the server truth.
  await context.addCookies([
    {{
      name: '_xsrf', value: {json.dumps(_SPECIFIC_TOKEN)},
      domain: 'c1-lazarus.invalid', path: '/lab/tree', secure: true
    }},
    {{
      name: '_xsrf', value: {json.dumps(_ROOT_TOKEN)},
      domain: 'c1-lazarus.invalid', path: '/', secure: true
    }}
  ]);
  let received_xsrf_token = null;
  await context.route(ORIGIN + '/**', async route => {{
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === PAGE_PATH) {{
      return route.fulfill({{
        status: 200, contentType: 'text/html; charset=utf-8',
        body: fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8')
      }});
    }}
    if (url.pathname === '/dist/bundle.js') {{
      return route.fulfill({{
        status: 200, contentType: 'text/javascript; charset=utf-8',
        body: fs.readFileSync(path.join(__dirname, 'dist/bundle.js'), 'utf8')
      }});
    }}
    if (url.pathname === '/api/contents') {{
      received_xsrf_token = await request.headerValue('x-xsrftoken');
      return route.fulfill({{
        status: 200, contentType: 'application/json',
        body: JSON.stringify({{ received_xsrf_token }})
      }});
    }}
    return route.fulfill({{ status: 404, body: 'not routed' }});
  }});
  const page = await context.newPage();
  page.on('pageerror', error => console.error('PAGEERROR ' + error.message));
  await page.goto(ORIGIN + PAGE_PATH, {{ timeout: 20000 }});
  await page.waitForFunction(
    () => window.__candidateReady === true, null, {{ timeout: 20000 }}
  );
  const document_cookie = await page.evaluate(() => window.__candidateCookie());

  // Golden probe one: the server truth token rendered directly.
  await page.evaluate(
    value => window.__candidateRenderToken(value), {json.dumps(_ROOT_TOKEN)}
  );
  await wait(200);
  await page.screenshot({{ path: path.join(__dirname, 'fixed-golden.png') }});

  // Golden probe two: the stale most-specific-path token rendered directly.
  await page.evaluate(
    value => window.__candidateRenderToken(value), {json.dumps(_SPECIFIC_TOKEN)}
  );
  await wait(200);
  await page.screenshot({{ path: path.join(__dirname, 'buggy-golden.png') }});

  // Registered start: an empty display before any candidate request.
  await page.evaluate(() => window.__candidateReset());
  await wait(200);
  const before_display = await page.evaluate(
    () => document.getElementById('token-display').textContent
  );
  await page.screenshot({{ path: path.join(__dirname, 'before.png') }});

  // Real observation: the candidate builds the request and the routed endpoint
  // reports the header it actually received.
  const displayed = await page.evaluate(() => window.__candidateRequest());
  await wait(300);
  await page.screenshot({{ path: path.join(__dirname, 'after.png') }});

  console.log('{_MARKER}=' + JSON.stringify({{
    document_cookie, before_display, displayed, received_xsrf_token,
    browser_channel: 'msedge', browser_version: browser.version(),
    frames: {{ before: 'before.png', after: 'after.png',
      fixed_golden: 'fixed-golden.png', buggy_golden: 'buggy-golden.png' }}
  }}));
  await browser.close();
}})().catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    return {
        "entry.js": entry,
        "rspack.config.cjs": config,
        "index.html": html_document,
        "edge-observe.cjs": browser,
    }


_EXPECTED_COOKIE = f"_xsrf={_SPECIFIC_TOKEN}; _xsrf={_ROOT_TOKEN}"


def _semantic_state(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "sent_xsrf_token": data.get("received_xsrf_token"),
        "displayed_token": data.get("displayed"),
    }


def _observation_payload(data: Mapping[str, object]) -> dict[str, object]:
    return {
        "document_cookie": data.get("document_cookie"),
        "before_display": data.get("before_display"),
        "displayed_token": data.get("displayed"),
        "received_xsrf_token": data.get("received_xsrf_token"),
        "real_browser_cookie_jar": True,
    }


def _start_is_registered(payload: Mapping[str, object]) -> bool:
    return (
        payload.get("document_cookie") == _EXPECTED_COOKIE
        and payload.get("before_display") == ""
    )


SPEC = RealEdgeClosureSpec(
    change_number=_CHANGE,
    epoch=_EPOCH,
    gate57_transition=_GATE57_TRANSITION,
    gate57_pool=_GATE57_POOL,
    production_modules=(_SERVERCONNECTION,),
    common_assets=(_SERVICES_INDEX, _VARIABLES_CSS),
    harness_files=_harness_files,
    observation_marker=_MARKER,
    registered_start={
        "origin": _ORIGIN,
        "page_path": _PAGE_PATH,
        "document_cookie": _EXPECTED_COOKIE,
        "display": "",
    },
    replay_steps=(
        "load_candidate_local_bundle_from_the_routed_origin",
        "seed_duplicate_xsrf_cookies_at_two_paths_in_the_real_cookie_jar",
        "let_the_candidate_build_one_authenticated_post_request",
        "report_the_xsrf_header_the_endpoint_received",
        "render_and_read_the_reported_token",
    ),
    authoritative_defect=(
        "with duplicate _xsrf cookies the client sends the most specific path "
        "value while the server keeps the last one, so the request is rejected"
    ),
    expected_after={
        "fixed": {
            "sent_xsrf_token": _ROOT_TOKEN,
            "displayed_token": _ROOT_TOKEN,
        },
        "buggy": {
            "sent_xsrf_token": _SPECIFIC_TOKEN,
            "displayed_token": _SPECIFIC_TOKEN,
        },
    },
    semantic_state=_semantic_state,
    observation_payload=_observation_payload,
    start_is_registered=_start_is_registered,
    golden_frame_source="browser_constructed_hidden_semantic_oracle",
    actual_after_source="candidate_computed_request_header",
    global_cross_threshold=0.05,
)


__all__ = ["SPEC"]
