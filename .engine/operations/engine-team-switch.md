---
title: Switch to team mode — a separate identity makes changes, and your approval is required
---

## Purpose

Move this repository from **on-your-own (solo)** mode to **team** mode, and back. It is a one-time,
bigger setup than the everyday safety gate — walk it slowly.

In solo mode the engine makes changes as **you**, and your merge is the only gate. That gate can't be
weakened without your say-so, but because the engine holds your own credentials, a weakening *could*
still happen with your consent. **Team mode closes that structurally:** the engine commits under a
**separate, non-admin account**, so your approval becomes *required* before anything merges — and
because that account holds no admin power, it **cannot change your safety rules at all**. Even the
engine can't weaken them; only you can. That is the reason a solo owner might still want team mode:
not because you have teammates, but because you want the stronger guarantee.

The engine can't create that second account for you, so this is **guide + check + record + turn on**:
you do the account setup, and the engine records it, wires it up, checks it genuinely protects you,
and turns on the team safety rules.

## Steps

1. **Read where you stand.** Run `uv run --directory .engine -- python tools/team_switch.py status` — it
   tells you which mode you're in and, once you name the account, the single next thing to do.
2. **Create the engine's separate account.** On GitHub, create a **second account** (a "machine user")
   that will make the engine's changes — for example `your-project-engine`. This is the account whose
   name will appear on the engine's commits and pull requests.
3. **Add it to this repository as a *non-admin* collaborator.** Give it **Write** access, never Admin.
   Write lets it open pull requests; withholding Admin is what makes it unable to weaken your safety
   rules — the whole point of team mode.
4. **Give that account a token for this repository.** Signed in as the second account, create a
   **fine-grained personal access token** scoped to *this repository only*, with permission to read/write
   code and pull requests. Keep it somewhere safe (your password manager or `gh auth login` as that
   account) — the engine never sees or stores it.
5. **Make sure your CODEOWNERS names you.** Team mode requires *a code-owner* to approve. Confirm your
   own account is listed as an owner in `.github/CODEOWNERS` for the paths you want to review (at least
   the engine's). `status` will tell you if this is missing.
6. **Turn it on.** Run `uv run --directory .engine -- python tools/team_switch.py apply --login <the
   second account's name>`, signed in as **yourself** (turning on the rules needs your admin access). The
   engine checks the account is set up correctly, records it, points the engine's commits at it, and
   turns on the team safety rules. If anything isn't ready it stops first and tells you the one thing to
   fix — it won't start a half-finished switch. And if it's ever interrupted partway (your machine sleeps,
   the network drops), just run it again: it's safe to re-run and picks up from wherever it left off.
7. **Commit the recorded change**, then **run your future build sessions signed in as the second
   account** (so the engine's pull requests come from it and wait for your approval). You stay signed in
   as yourself to review and merge.

## Done when

`team_switch.py status` reports you are in team mode, and your **next change opens as a pull request
authored by the second account that you must approve before it can merge** — you'll see it waiting for
your review, which is the whole point.

## Notes

- **Going back:** `team_switch.py reverse` returns you to solo. Because that removes the required
  approval, the change it records will ask you to apply the `guardrail-ack` label — a deliberate
  confirmation that you mean to give up that protection.
- **If the engine can't make changes anymore** (the second account's token expired, or it was removed as
  a collaborator): the engine can't author commits until it's fixed. Re-issue the token (step 4) and
  sign back in as that account, or run `reverse` to go back to making changes as yourself.
- Turning on the rules needs **your** admin access; making everyday changes uses the **second account's**
  token — two different sign-ins, on purpose.
