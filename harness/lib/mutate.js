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

// The proccode of the procedure whose definition holds block `id` (null for a block under a hat).
function procOf(t, id) {
  let top = id;
  while (t.blocks[top].parent) top = t.blocks[top].parent;
  const def = t.blocks[top];
  if (def.opcode !== 'procedures_definition') return null;
  const proto = t.blocks[def.inputs.custom_block[1]];
  return proto && proto.mutation ? proto.mutation.proccode : null;
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
 * Rewrite only the `change <var> by <fromValue>` blocks on a sprite (leaving the variable's other
 * changes alone). Used to undo the area clock's carry (`change area progress by -65536` → by 0)
 * without freezing the clock's own +32 step.
 */
export function changeVariableChangeBy(project, spriteName, varName, fromValue, toValue) {
  const t = target(project, spriteName);
  const vid = variableId(t, varName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (
      b.opcode === 'data_changevariableby' &&
      b.fields.VARIABLE &&
      b.fields.VARIABLE[1] === vid &&
      Array.isArray(b.inputs.VALUE) &&
      Array.isArray(b.inputs.VALUE[1]) &&
      String(b.inputs.VALUE[1][1]) === String(fromValue)
    ) {
      b.inputs.VALUE = [1, [4, String(toValue)]];
      patched += 1;
    }
  }
  if (!patched) throw new Error(`mutate: no 'change ${varName} by ${fromValue}' on ${spriteName}`);
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

/**
 * Rewrite the literal ITEM of every `replace item (...) of <list> with <fromValue>` on a sprite. Used to
 * start the ground seeders' `slot x` one tick down the field (0 → 32) — a one-line shift of where every
 * spawned ground object rides against the map, the severing negative for the terrain-phase scenario.
 * `withinProc` (optional) limits the rewrite to blocks inside that procedure's definition, so the culls
 * and clears elsewhere that also write the literal are left alone.
 */
export function changeListReplaceLiteral(project, spriteName, listName, fromValue, toValue, withinProc = null) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode !== 'data_replaceitemoflist' || !b.fields.LIST || b.fields.LIST[0] !== listName) continue;
    if (withinProc !== null && procOf(t, id) !== withinProc) continue;
    const item = b.inputs.ITEM;
    if (Array.isArray(item) && Array.isArray(item[1]) && String(item[1][1]) === String(fromValue)) {
      b.inputs.ITEM = [1, [10, String(toValue)]];
      patched += 1;
    }
  }
  if (!patched) {
    const scope = withinProc === null ? spriteName : `'${withinProc}' on ${spriteName}`;
    throw new Error(`mutate: no 'replace item of ${listName} with ${fromValue}' in ${scope}`);
  }
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
  // A sprite may compare a Stage-owned global (referenced by the Stage's variable id) rather than a
  // sprite-local var; resolve the id from the sprite first, then fall back to the Stage.
  let vid;
  try {
    vid = variableId(t, varName);
  } catch {
    vid = variableId(target(project, 'Stage'), varName);
  }
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

/**
 * Change the literal right-hand value of an `operator_equals` whose LEFT operand (OPERAND1) is an
 * `item (index) of <list>` reporter for a named list — the list-item analogue of
 * changeVarEqualsOperand, for a `item(...) of <list> == N` guard that shares its literal with unrelated
 * equals on the same target. Used to break the Logram single-shot fire guard
 * (`item(slot index) of (slot fire timer) == 12`) without touching the `slot type == 12` /
 * `walk type == 12` type-code checks that share the literal 12. `animate_step()` is built twice (the
 * reporter-single-parent-steal fix), so BOTH copies of the fire guard are patched — exactly the intent:
 * the Logram then never fires at full-open.
 */
export function changeListItemEqualsOperand(project, spriteName, listName, fromValue, toValue, withinProc = null) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode !== 'operator_equals' || !b.inputs.OPERAND1 || !b.inputs.OPERAND2) continue;
    if (withinProc !== null && procOf(t, id) !== withinProc) continue;
    const lhsId = b.inputs.OPERAND1[1];
    const lhs = typeof lhsId === 'string' ? t.blocks[lhsId] : null;
    const isListItem =
      lhs && lhs.opcode === 'data_itemoflist' && lhs.fields && lhs.fields.LIST && lhs.fields.LIST[0] === listName;
    const rhs = b.inputs.OPERAND2[1];
    const matchesLiteral = Array.isArray(rhs) && String(rhs[1]) === String(fromValue);
    if (isListItem && matchesLiteral) {
      b.inputs.OPERAND2 = [1, [10, String(toValue)]];
      patched += 1;
    }
  }
  if (!patched) {
    throw new Error(`mutate: no 'operator_equals item of ${listName} == ${fromValue}' on ${spriteName}`);
  }
}

/**
 * Change the literal divisor (NUM2) of every `operator_divide` on a sprite. Used to put the hit boxes'
 * lateral shadow back on half-pixel units (`slot y / 32` → `/ 16`, the pre-PRES-01 misread that halved
 * the bomb box) — the severing negative for the bomb-between-a-pair scenario.
 */
/**
 * Insert `broadcast <message>` immediately before every `transition to <destination>` call on `spriteName`
 * — restores a broadcast-then-transition ordering a fix removed (e.g. the pre-fix death path's
 * `craft changed` ahead of `transition to player-dead`).
 */
export function insertBroadcastBeforeTransition(project, spriteName, destination, message) {
  const t = target(project, spriteName);
  const stage = project.targets.find((x) => x.isStage);
  const broadcastId = Object.keys(stage.broadcasts || {}).find((id) => stage.broadcasts[id] === message);
  if (!broadcastId) throw new Error(`mutate: no broadcast '${message}'`);
  const calls = Object.keys(t.blocks).filter((id) => {
    const b = t.blocks[id];
    if (!b || b.opcode !== 'procedures_call') return false;
    if (!String(b.mutation?.proccode || '').startsWith('transition to')) return false;
    return Object.values(b.inputs || {}).some(
      (v) => Array.isArray(v) && Array.isArray(v[1]) && String(v[1][1]) === destination,
    );
  });
  if (!calls.length) throw new Error(`mutate: no 'transition to ${destination}' on ${spriteName}`);
  calls.forEach((callId, n) => {
    const call = t.blocks[callId];
    const newId = `mut-bcast-${n}`;
    const parentId = call.parent;
    t.blocks[newId] = {
      opcode: 'event_broadcast',
      next: callId,
      parent: parentId,
      inputs: { BROADCAST_INPUT: [1, [11, message, broadcastId]] },
      fields: {},
      shadow: false,
      topLevel: false,
    };
    const par = t.blocks[parentId];
    if (par.next === callId) {
      par.next = newId;
    } else {
      for (const [k, v] of Object.entries(par.inputs || {})) {
        if (Array.isArray(v) && v[1] === callId) par.inputs[k] = [v[0], newId];
      }
    }
    call.parent = newId;
  });
}

/**
 * Rewrite the literal right-hand side of every `operator_lt` whose OPERAND2 is `fromValue` on a sprite. Used to
 * move the HUD's "always shown" digit places (`hud place < 2`) so the leading zeros come back.
 */
export function changeLessThanLiteral(project, spriteName, fromValue, toValue) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode !== 'operator_lt' || !b.inputs.OPERAND2) continue;
    const shadow = b.inputs.OPERAND2[1];
    if (Array.isArray(shadow) && shadow[0] !== 12 && String(shadow[1]) === String(fromValue)) {
      b.inputs.OPERAND2 = [1, [4, String(toValue)]];
      patched += 1;
    }
  }
  if (!patched) throw new Error(`mutate: no 'operator_lt < ${fromValue}' on ${spriteName}`);
}

export function changeDivideLiteral(project, spriteName, fromValue, toValue) {
  const t = target(project, spriteName);
  let patched = 0;
  for (const id of Object.keys(t.blocks)) {
    const b = t.blocks[id];
    if (b.opcode !== 'operator_divide') continue;
    const input = b.inputs.NUM2;
    if (Array.isArray(input) && Array.isArray(input[1]) && String(input[1][1]) === String(fromValue)) {
      b.inputs.NUM2 = [1, [4, String(toValue)]];
      patched += 1;
    }
  }
  if (!patched) throw new Error(`mutate: no 'operator_divide / ${fromValue}' on ${spriteName}`);
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

/**
 * Delete the ground renderer's per-tick `clear graphic effects` block(s) on a sprite, re-introducing the
 * colour-effect residue bug (BOSS-01 finding #1): graphic effects PERSIST on the 16 reused ground clones,
 * so a clone that drew a boss part (which sets the `color` effect) keeps that tint on the next normal
 * ground object it draws. The block is spliced out of its chain (parent rewired past it, its `next`'s
 * parent repointed), so the loop still runs — it just no longer clears effects. The severing negative for
 * the `non-boss-clone-has-no-residual-tint` scenario.
 */
export function removeClearGraphicEffects(project, spriteName) {
  const t = target(project, spriteName);
  const ids = Object.keys(t.blocks).filter(
    (id) => t.blocks[id].opcode === 'looks_cleargraphiceffects',
  );
  if (!ids.length) throw new Error(`mutate: no looks_cleargraphiceffects on ${spriteName}`);
  for (const id of ids) {
    const b = t.blocks[id];
    const nextId = b.next || null;
    const parentId = b.parent;
    if (parentId && t.blocks[parentId]) {
      const par = t.blocks[parentId];
      if (par.next === id) {
        par.next = nextId;
      } else if (par.inputs) {
        for (const k of Object.keys(par.inputs)) {
          const inp = par.inputs[k];
          if (Array.isArray(inp) && inp.some((e) => e === id)) {
            if (nextId) par.inputs[k] = inp.map((e) => (e === id ? nextId : e));
            else delete par.inputs[k];
          }
        }
      }
    }
    if (nextId && t.blocks[nextId]) t.blocks[nextId].parent = parentId;
    delete t.blocks[id];
  }
}

/**
 * Delete every `delete this clone` block on a sprite, so its clones never retire. On start_screen this
 * severs the attract-display clone lifecycle: the `common_stop(clones=True)` director-stop handler (which
 * deletes each clone on the next state transition) and the digit/prompt role loops' own tail-deletes all
 * stop firing, so the credit line, digits, prompt, and best-five clones ACCUMULATE across every
 * title -> demo -> best-five -> demo -> title cycle instead of being retired. The severing negative for
 * `attract-clone-no-leak`: with the deletes gone the per-cycle clone count climbs instead of returning to
 * baseline. Each block is spliced out of its chain like removeClearGraphicEffects (it is a cap block, so
 * its `next` is null and the parent simply loses its tail).
 */
export function removeDeleteThisClone(project, spriteName) {
  const t = target(project, spriteName);
  const ids = Object.keys(t.blocks).filter(
    (id) => t.blocks[id].opcode === 'control_delete_this_clone',
  );
  if (!ids.length) throw new Error(`mutate: no control_delete_this_clone on ${spriteName}`);
  for (const id of ids) {
    const b = t.blocks[id];
    const nextId = b.next || null;
    const parentId = b.parent;
    if (parentId && t.blocks[parentId]) {
      const par = t.blocks[parentId];
      if (par.next === id) {
        par.next = nextId;
      } else if (par.inputs) {
        for (const k of Object.keys(par.inputs)) {
          const inp = par.inputs[k];
          if (Array.isArray(inp) && inp.some((e) => e === id)) {
            if (nextId) par.inputs[k] = inp.map((e) => (e === id ? nextId : e));
            else delete par.inputs[k];
          }
        }
      }
    }
    if (nextId && t.blocks[nextId]) t.blocks[nextId].parent = parentId;
    delete t.blocks[id];
  }
}

/**
 * Splice a `set <varName> = <constValue>` block onto the FRONT of a proc's body. Used to bite an
 * omission-based invariant: some contracts are realized by NOT writing a variable (the Sheonite is inert
 * because `update sheonite` writes no `player hit`), so there is no existing block to neutralize — the
 * negative must GRAFT the forbidden write, which then makes the inertness assertion go red.
 */
export function graftVariableSetOnProc(project, spriteName, proccode, varName, constValue) {
  const t = target(project, spriteName);
  const varId = variableId(t, varName);
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
      const graftId = `graft_${proccode.replace(/\s+/g, '_')}_${varName.replace(/\s+/g, '_')}`;
      t.blocks[graftId] = {
        opcode: 'data_setvariableto',
        next: b.next,
        parent: id,
        inputs: { VALUE: [1, [4, String(constValue)]] },
        fields: { VARIABLE: [varName, varId] },
        shadow: false,
        topLevel: false,
      };
      if (b.next && t.blocks[b.next]) t.blocks[b.next].parent = graftId;
      b.next = graftId;
      return;
    }
  }
  throw new Error(`mutate: no procedures_definition for '${proccode}' on ${spriteName}`);
}
