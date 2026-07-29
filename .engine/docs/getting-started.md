---
title: Getting started with your Engine
---

## What this covers

This is for the person whose project now has an Engine and who directs it in plain language rather than by writing code. By the end you will know what the Engine is, how you tell it what to do, and how to find out everything you can ask it for.

## What you need to know

Your project has an **Engine**: a system that helps an AI assistant work on your project reliably, session after session, without you having to hold all the details in your head.

An AI assistant starts each conversation fresh — it does not remember what happened last time. On its own, that means re-explaining where things stand every time, and hoping it does not quietly forget a decision you made last week. The Engine fixes that. It keeps your project's running state, its memory, the decisions you have made, and a set of safety rules, all written down in your project where the assistant reads them at the start of every session. So instead of starting from nothing, the assistant starts already oriented — it knows where the project stands and what was decided before.

That memory lives on your own computer. When you first set your project up, the Engine offers to keep a private, off-computer **backup** of it — a copy of the notes it saves about your work (never your code) — so nothing is lost if anything happens to your machine. It asks you first, shows you exactly where the backup would live and that it must stay private, and creates nothing until you say yes; you can pick one shared backup for all your projects or a separate one just for this project, or decline and set one up later. If the Engine ever can't read your saved memory at the start of a session, it tells you plainly and points you to restore it from that backup.

You work with the Engine entirely through your AI assistant, in plain conversation. There is nothing to install. You point the assistant at what you want done; the Engine is what keeps it honest, grounded, and consistent while it does it.

## How you direct the engine

You direct the Engine the same way you would direct a capable assistant: you tell it, in ordinary words, what you want.

You might say *"I want to start working on the booking page"* or *"remind me what we decided about pricing"* or *"check whether this is safe to ship."* You do not need special phrasing. The Engine keeps track of your project for you between sessions, so you can pick up a conversation days later and it already knows the context — you do not have to re-explain.

When you make a significant decision about how your Engine itself is set up — turning a feature on, adjusting one of its safety rules — you can ask it to record that decision (*"note that we decided to…"*). It keeps that as one of your own engine decisions, kept apart from the Engine's own built-in ones and held onto even when the Engine is later updated, so your reasoning is not lost.

If your project builds its own safety-critical piece — say a script that guards something important — you can ask the Engine, in plain words, to protect it (*"treat this scanner as protected"*), or name a folder to cover everything inside it. The Engine keeps a short list of these for you and watches them alongside its own protections, so any change to one asks for your deliberate sign-off. Taking something back off that list later asks you to confirm too — removing a protection is a deliberate step, the same as adding one. The list is kept as yours and preserved across updates.

When you ask for something that changes your project, the Engine works in careful steps and shows you what it plans before it does it, so nothing significant happens without your say-so.

## Finding the commands

Alongside plain conversation, the Engine gives you a set of shortcuts — short typed commands that start with **`engine-`** so you can tell them apart from the other commands your assistant offers. The opening keystroke depends on where you're working: in **Claude Code** you type them as `/engine-…`; in **Codex** the same commands are `$engine-…`. There are three easy ways to see what's available:

- Run **`/engine-help`** (in Codex: **`$engine-help`**) — it lists the Engine's commands in your runtime's own form and explains, in plain language, what each one is for. This is the simplest place to start.
- Type the opening keystroke and **`engine`** in the message box (`/engine` in Claude Code, `$engine` in Codex) — the menu narrows to just the Engine's commands.
- Or simply ask, in plain words — *"what can the Engine do?"*

You never have to memorize anything; the list is always a few keystrokes or one question away.

One question worth knowing you can ask: **"what is my engine made of?"** — its version, the parts it is built from, and how they fit together. Ask it in plain words, or type **`/engine-parts`** for the same plain readout. It only reads; it never changes anything.

## When the engine suggests removing a part of itself

From time to time the Engine checks over its own parts and may suggest **retiring** something it no longer needs — a rule, a document, or a piece of its own machinery.

This is routine upkeep: keeping only what still earns its place is part of how the Engine stays lean and trustworthy. Treat such a suggestion like any other proposed change — it is yours to approve or decline, and nothing is removed without your agreement.

## Where to go next

The simplest way to begin is to tell the Engine what you want to work on, in your own words. At the start of each session it will orient you — showing where your project stands — and from there you just keep the conversation going. When you want to know what else you can ask for, run `/engine-help`.

If you're starting something new and would like it written down clearly before any building begins, the Engine can help you describe it first — laid out a piece at a time, in plain language, and checked so every part is present and well-formed (it checks the shape, never whether the idea is right — that stays your call). Just say so, or run `/engine-design`. It's optional, and on a fresh project the Engine will offer it at the start until you've either done it or told it you'd rather work without a written description.
