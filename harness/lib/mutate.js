// Targeted, in-memory project mutations for the negative fixtures. Each breaks exactly
// one scenario's behavior so that scenario's assertion goes red — proving the assertion
// binds (a green run means something). Every mutation throws if its target block is not
// found, so a generator change that moves the ground under a fixture fails loudly rather
// than silently mutating nothing.

function target(project, name) {
  const t = project.targets.find((x) => (name === 'Stage' ? x.isStage : x.name === name));
  if (!t) throw new Error(`mutate: no target '${name}'`);
  return t;
}

function listNamed(t, name) {
  for (const id of Object.keys(t.lists || {})) {
    if (t.lists[id][0] === name) return t.lists[id];
  }
  throw new Error(`mutate: no list '${name}' on '${t.name}'`);
}

function variableId(t, name) {
  for (const id of Object.keys(t.variables || {})) {
    if (t.variables[id][0] === name) return id;
  }
  throw new Error(`mutate: no variable '${name}' on '${t.name}'`);
}

/** Remove a transition from the Stage allow-list, so that transition can no longer fire. */
export function removeAllowedTransition(project, entry) {
  const list = listNamed(target(project, 'Stage'), 'allowed transitions');
  const before = list[1].length;
  list[1] = list[1].filter((e) => e !== entry);
  if (list[1].length === before) {
    throw new Error(`mutate: '${entry}' not in allowed transitions`);
  }
}

/** Make every `change <var> by N` on a sprite a no-op (change by 0), freezing the counter. */
export function freezeVariableChange(project, spriteName, varName) {
  const t = target(project, spriteName);
  const vid = variableId(t, varName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode === 'data_changevariableby' && b.fields.VARIABLE && b.fields.VARIABLE[1] === vid) {
      b.inputs.VALUE = [1, [4, '0']];
      patched += 1;
    }
  }
  if (!patched) throw new Error(`mutate: no 'change ${varName}' block on ${spriteName}`);
}

/** Change an `operator_equals` literal right-hand value on a sprite (breaks an == guard). */
export function changeEqualsOperand(project, spriteName, fromValue, toValue) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode === 'operator_equals' && b.inputs.OPERAND2) {
      const shadow = b.inputs.OPERAND2[1];
      if (Array.isArray(shadow) && String(shadow[1]) === String(fromValue)) {
        b.inputs.OPERAND2 = [1, [10, String(toValue)]];
        patched += 1;
      }
    }
  }
  if (!patched) throw new Error(`mutate: no 'operator_equals == ${fromValue}' on ${spriteName}`);
}

/** Raise an `operator_gt` literal right-hand threshold on a sprite (breaks a > gate). */
export function raiseGreaterThreshold(project, spriteName, fromValue, toValue) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode === 'operator_gt' && b.inputs.OPERAND2) {
      const shadow = b.inputs.OPERAND2[1];
      if (Array.isArray(shadow) && String(shadow[1]) === String(fromValue)) {
        b.inputs.OPERAND2 = [1, [10, String(toValue)]];
        patched += 1;
      }
    }
  }
  if (!patched) throw new Error(`mutate: no 'operator_gt > ${fromValue}' on ${spriteName}`);
}
