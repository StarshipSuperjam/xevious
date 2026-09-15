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

/**
 * Pin every `set <var> to ...` on a sprite to a constant, severing whatever expression fed the
 * set. Mirrors freezeVariableChange but for `data_setvariableto`: replaces inputs.VALUE with a
 * literal shadow so the variable can no longer track its source. Used to break the density chain
 * (pin `formation count` to a fixed value so it no longer follows the AI-level table lookup).
 */
export function pinVariableSet(project, spriteName, varName, constValue) {
  const t = target(project, spriteName);
  const vid = variableId(t, varName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode === 'data_setvariableto' && b.fields.VARIABLE && b.fields.VARIABLE[1] === vid) {
      b.inputs.VALUE = [1, [10, String(constValue)]];
      patched += 1;
    }
  }
  if (!patched) throw new Error(`mutate: no 'set ${varName}' block on ${spriteName}`);
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

/**
 * Reintroduce the mathop field-name bug on a sprite's `operator_mathop` blocks: rename the
 * OPERATOR field to OPERATION so scratch-vm cannot resolve the function and `floor` returns 0.
 * Every digit then computes `floor(score / divisor) mod 10 = 0`, collapsing the HUD to all
 * `digit/0` — the exact class of "structurally present, runtime wrong" bug this scenario guards.
 */
export function misnameMathopOperator(project, spriteName) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode === 'operator_mathop' && b.fields && b.fields.OPERATOR) {
      b.fields = { OPERATION: b.fields.OPERATOR };
      patched += 1;
    }
  }
  if (!patched) throw new Error(`mutate: no operator_mathop on ${spriteName}`);
}

/**
 * Rewrite an `operator_add` numeric literal on a sprite (either operand, NUM1 or NUM2). Used to zero
 * the bomb crosshair's forward lead (`craft_row*256 + (-3072)` → `+ 0`) so the sight sits on the craft
 * instead of 96 px ahead — the severing negative for the crosshair-lead / target-lock scenarios.
 */
export function changeAddLiteral(project, spriteName, fromValue, toValue) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode !== 'operator_add') continue;
    for (const slot of ['NUM1', 'NUM2']) {
      const input = b.inputs[slot];
      if (
        Array.isArray(input) &&
        Array.isArray(input[1]) &&
        String(input[1][1]) === String(fromValue)
      ) {
        b.inputs[slot] = [1, [4, String(toValue)]];
        patched += 1;
      }
    }
  }
  if (!patched) throw new Error(`mutate: no 'operator_add ${fromValue}' on ${spriteName}`);
}

/**
 * Change the literal right-hand value of an `operator_equals` whose LEFT operand (OPERAND1) is a
 * specific variable reporter — a surgical variant of changeEqualsOperand for a `<var> == N` guard
 * that shares its literal (e.g. `0`) with many other equals on the same target. Used to break the
 * bomb arm gate (`bomb in flight == 0`, now Stage-owned) without touching every other `== 0`.
 */
export function changeVarEqualsOperand(project, spriteName, varName, fromValue, toValue) {
  const t = target(project, spriteName);
  const vid = variableId(t, varName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode !== 'operator_equals' || !b.inputs.OPERAND1 || !b.inputs.OPERAND2) continue;
    const lhs = b.inputs.OPERAND1[1];
    const isVar = Array.isArray(lhs) && lhs[0] === 12 && lhs[2] === vid;
    const rhs = b.inputs.OPERAND2[1];
    const matchesLiteral = Array.isArray(rhs) && String(rhs[1]) === String(fromValue);
    if (isVar && matchesLiteral) {
      b.inputs.OPERAND2 = [1, [10, String(toValue)]];
      patched += 1;
    }
  }
  if (!patched) {
    throw new Error(`mutate: no 'operator_equals ${varName} == ${fromValue}' on ${spriteName}`);
  }
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

/**
 * Empty a custom procedure's body on a sprite: find the `procedures_definition` whose prototype
 * carries `proccode`, and cut its `next` so the definition runs nothing. Callers of the proc still
 * execute (the call block is untouched) but the proc becomes a no-op — the surgical way to prove a
 * scenario binds to that proc actually running (e.g. `update toroid` moving a live enemy), without
 * disturbing anything upstream (the game still reaches playing, enemies are still spawned).
 */
export function neutralizeProc(project, spriteName, proccode) {
  const t = target(project, spriteName);
  let prototypeId = null;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode === 'procedures_prototype' && b.mutation && b.mutation.proccode === proccode) {
      prototypeId = id;
      break;
    }
  }
  if (!prototypeId) throw new Error(`mutate: no procedures_prototype '${proccode}' on ${spriteName}`);
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (
      b.opcode === 'procedures_definition' &&
      b.inputs &&
      b.inputs.custom_block &&
      b.inputs.custom_block[1] === prototypeId
    ) {
      b.next = null;
      return;
    }
  }
  throw new Error(`mutate: no procedures_definition for '${proccode}' on ${spriteName}`);
}
