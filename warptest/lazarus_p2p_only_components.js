// Generic candidate-component host. No task IDs or task-specific state/action defaults.
import { ToolbarButton, LabIcon, HTMLSelect } from '__CANDIDATE__/packages/ui-components/lib/index.js';
import { VariablesBodyTree } from '__CANDIDATE__/packages/debugger/lib/panels/variables/tree.js';
import { VariablesModel } from '__CANDIDATE__/packages/debugger/lib/panels/variables/model.js';
import { CommandRegistry } from '@lumino/commands';
import { Widget } from '@lumino/widgets';
import * as React from 'react';
import { createRoot } from 'react-dom/client';

let state = null, widget = null, model = null;
const host = document.querySelector('#surface');
const clone = x => JSON.parse(JSON.stringify(x));
const settled = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
const el = (tag, attrs = {}, text = '') => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.textContent = text;
  return node;
};
const glyph = (name, shape) => new LabIcon({name: 'p2p-only:' + name,
  svgstr: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">' + shape + '</svg>'});
const entry = v => ({name: v.name, value: v.value, type: 'int', variablesReference: 0, evaluateName: v.name});
const updateVariables = () => { model.scopes = [{name: state.props.scope, variables: state.props.variables.map(entry)}]; widget.update(); };
const paint = () => { const r = document.querySelector('#readout'); if (r && widget) r.textContent = 'engaged: ' + String(widget.pressed); };

const restoreComponent = async value => {
  if (widget) widget.dispose();
  widget = null; model = null; state = clone(value);
  const props = state.props;
  document.querySelector('#candidate-theme').textContent = window.p2pThemes[state.theme];
  host.replaceChildren(); host.dataset.component = state.component;
  host.style.background = state.theme === 'light' ? '#fff' : '#000';
  const page = el('div', {class: 'page jp-ThemedContainer'}); host.append(page);
  switch (state.component) {
    case 'toolbar_button': {
      const strip = el('div', {class: 'strip'}), target = el('div', {id: 'button-host'});
      page.append(target, el('div', {id: 'readout', class: 'readout'}));
      widget = new ToolbarButton({icon: glyph('rest', '<circle cx="12" cy="12" r="9" fill="#202020"/>'),
        pressedIcon: glyph('engaged', '<rect x="4" y="4" width="16" height="16" fill="none" stroke="#202020" stroke-width="3"/>'),
        pressed: props.pressed, label: props.label, onClick: () => setTimeout(paint, 0)});
      Widget.attach(widget, target); paint(); break;
    }
    case 'variables': {
      const target = el('div', {id: 'tree-host'}); page.append(target);
      model = new VariablesModel();
      const service = {inspectVariable: async () => [], getVariableDetails: async () => [],
        hasRichVariableRendering: false, model: {hasRichVariableRendering: false, callstack: {frame: null}}};
      widget = new VariablesBodyTree({model, commands: new CommandRegistry(), service, translator: undefined});
      Widget.attach(widget, target); updateVariables(); break;
    }
    case 'html_select': {
      const strip = el('div', {class: 'strip'}), target = el('div', {id: 'panel-host'});
      const button = el('button', {}, props.label);
      button.onclick = () => window.p2pAction({type: 'open_panel', value: document.querySelector('#option-value').value,
        options: document.querySelector('#options').value.split(',')});
      strip.append(el('span', {}, 'Cell type'), button); page.append(strip, target); break;
    }
    case 'shortcut_chip': case 'selection': {
      const row = el('div', {class: state.component === 'selection' ? 'panel' : 'row'});
      const line = el('span', {id:'line', class:props.class_name}, props.text);
      row.append(line); page.append(row); break;
    }
    case 'themed_button': {
      page.id = 'host'; page.append(el('button', {class:props.class_name}, props.text)); break;
    }
    default: throw new Error('unsupported candidate component');
  }
  window.__ready = true;
  await settled();
  return window.p2pSnapshot();
};
window.p2pRestore = async value => {
  try {
    return await restoreComponent(value);
  } catch (error) {
    // Invalid public UI input must not leave a partially constructed component
    // that crashes later observation. Keep the original error visible to callers.
    try { if (widget) widget.dispose(); } catch (_) {}
    widget = null; model = null; state = null;
    host.replaceChildren();
    delete host.dataset.component;
    window.__ready = false;
    throw error;
  }
};
window.p2pSnapshot = () => {
  if (!state) return {configured:false};
  const value = clone(state), p = value.props;
  if (state.component === 'toolbar_button') p.pressed = widget.pressed;
  if (['shortcut_chip','selection','themed_button'].includes(state.component)) {
    const node = host.querySelector(state.component === 'themed_button' ? 'button' : '#line');
    p.text = node.textContent; p.class_name = node.className;
  }
  if (state.component === 'variables') p.variables = model.scopes[0].variables.map(v => ({name:v.name,value:v.value}));
  return value;
};
window.p2pObserve = () => ({state:window.p2pSnapshot(), visible:!!state,
  panel_open:!!host.querySelector('.overflow-panel'),
  selected:!!host.querySelector('.marked, .jp-ShortcutInput-key, .jp-mod-selected-InputText'),
  text:host.innerText});
window.p2pAction = async action => {
  if (!state) throw new Error('component not configured');
  switch (action.type) {
    case 'click': { const node = host.querySelector(action.selector); if(!node) throw new Error('selector missing'); node.click(); paint(); break; }
    case 'set_class': { const node = host.querySelector(action.selector); if(!node) throw new Error('selector missing'); node.className = action.class_name; break; }
    case 'select_text': {
      const node = host.querySelector(action.selector); if(!node || !node.firstChild) throw new Error('selection target missing');
      const range = document.createRange(); range.setStart(node.firstChild, action.start); range.setEnd(node.firstChild, action.end);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      // The public container draws the selected region using the candidate palette.
      if (state.component === 'selection' && action.end > action.start) node.classList.add('marked');
      break;
    }
    case 'append_button': { if (state.component !== 'themed_button') throw new Error('not a button container'); document.querySelector('#host').append(el('button',{class:action.class_name},action.text)); break; }
    case 'add_variable': if (!model) throw new Error('not a variables component');
      state.props.variables.push({name:action.name,value:action.value}); updateVariables(); break;
    case 'open_panel': {
      if (state.component !== 'html_select') throw new Error('not a selector component');
      const panel = el('div', {class:'overflow-panel'}); document.querySelector('#panel-host').append(panel);
      createRoot(panel).render(React.createElement(HTMLSelect,{value:action.value,onChange:()=>undefined},
        ...action.options.map(o => React.createElement('option',{value:o},o)))); break;
    }
    default: throw new Error('unsupported semantic action');
  }
  await settled(); return window.p2pObserve();
};
document.querySelector('#apply-state').onclick = async () => {
  try { await window.p2pRestore(JSON.parse(document.querySelector('#state-json').value)); document.querySelector('#error').textContent = ''; }
  catch(e) { document.querySelector('#error').textContent = String(e); }
};
document.querySelector('#add-variable').onclick = () => window.p2pAction({type:'add_variable',name:document.querySelector('#variable-name').value,value:document.querySelector('#variable-value').value});
document.querySelector('#apply-class').onclick = () => window.p2pAction({type:'set_class',selector:document.querySelector('#selector').value,class_name:document.querySelector('#class-name').value});
// Natural pointer text selection is visible to Agent-S and feeds the same palette-backed region.
host.addEventListener('mouseup', () => {if (state?.component === 'selection' && window.getSelection().toString()) host.querySelector('#line').classList.add('marked');});
window.__ready = true;

document.querySelector('#append-button').onclick = () => window.p2pAction({type:'append_button',text:document.querySelector('#button-text').value,class_name:document.querySelector('#button-class').value});
