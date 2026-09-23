# Arcade sound cues (the real gameplay SFX)

- Mechanic: Six of the arcade's gameplay sound effects now play at the exact events the arcade plays them,
  replacing the earlier placeholders and silence. A flying enemy destroyed by a shot plays the flying-enemy-hit
  cue; a ground target destroyed plays the ground-explosion cue; a Zakato or Brag Zakato teleporting in plays
  the teleport cue; a Garu Zakato detonating plays the Garu-Zakato cue; a player shot bouncing off a Bacura slab
  plays the Bacura-hit cue; and the right half of a Sheonite escort pair peeling off to retreat plays the
  Sheonite cue (the left half still vanishes silently). The two pieces of base music, the game-start jingle, the
  player-death sound, the bomb drop/explosion, the blaster fire and the extend/1UP cue are unchanged.
- Derived behavior: Each cue maps a numbered arcade sound to a single verified play point. In the arcade the
  sounds are fired by `osd_sound_start` with the sound id in `d0`: `FLYING_ENEMY_HIT_SND` (0x05) on a scored
  flying kill, `GROUND_EXPLOSION_SND` (0x11) on a scored ground kill, `TELEPORT_SND` (0x09) on the Zakato /
  Brag-Zakato teleport-in, `GARU_ZAKATO_SND` (0x06) on the Garu detonation, `BACURA_HIT_SND` (0x0a) on the shot
  that bounces off a Bacura, and `SHEONITE_SND` (0x08) on the right Sheonite's retreat only. There is no arcade
  sound for the left Sheonite's removal, so it stays silent, and the teleport cue is deliberately shared by the
  base Zakato and both Brag-Zakato variants because they route through the one `init_teleport` routine.
- Reference provenance: jotd666/xevious@71473685a8c7856c8401c8519276cd97a38d4183; the sound-id equates are `src/xevious.inc` 95–107, and the play points are `src/xevious_main.68k` `check_flying_enemies_shot` 2516–2537 (FLYING_ENEMY_HIT_SND), `handle_bombed_obj_and_award_points` 2597–2615 (GROUND_EXPLOSION_SND), `init_teleport` 3994–4005 (TELEPORT_SND), `garu_zakato_explode` 4031–4036 (GARU_ZAKATO_SND), `deactivate_shot` 2557–2559 (BACURA_HIT_SND), and `r_sheonite_retreat` 4120–4138 (SHEONITE_SND).
- Transfer class: Behavioral port (the play-point control flow and the sound-id-to-event mapping are
  instruction-derived; no source text, ROM, or media is copied from the reference). The audio itself is
  operator-supplied arcade sound-effect media, committed under `assets/game-sounds/` and recorded in
  `src/xevious/assets/provenance.json`; it is not derived from the reference.
- Scratch interpretation: The six wavs are committed byte-for-byte under `assets/game-sounds/` with a
  provenance manifest (`assets/game-sounds/manifest.json`), and `tools/hud_glyphs.py` — already the single
  writer of `project.json` and the overlay provenance, and the owner of the Stage's added `extend` sound —
  attaches all six as additional Stage sounds under their content-hash filenames (see `render_game_sounds`).
  They are named `air_destroy`, `ground_destroy`, `zakato`, `garu_zakato`, `bacura` and `sheonite`. Five of the
  six play from Stage-thread procs directly (`play_sound`), because the walk and hit-detector procs that own
  those seams run on the Stage: `install_check_air_hit` plays `air_destroy`, `install_check_ground_hit` plays
  `ground_destroy`, `install_init_zakato` and `install_init_brag_zakato` play `zakato` on the teleport stamp,
  `install_garu_zakato_detonate` plays `garu_zakato`, and `install_update_sheonite` plays `sheonite` in the
  right half's retreat branch only. The sixth, `bacura`, is special: the shot×Bacura bounce runs on a blaster
  CLONE, which cannot play a Stage-owned sound directly, so `blaster_blocks` broadcasts `sfx bacura` and a Stage
  receiver plays the cue — the same broadcast-to-a-non-cloning-host pattern the bomb renderer already uses.
- Scratch evidence: the `assets/game-sounds/` wavs plus `manifest.json`; the `render_game_sounds` /
  `load_game_sounds_manifest` / `_overlay_game_sound_record` path in `tools/hud_glyphs.py` and its attachment of
  the six sounds to the Stage in `expected_project`; the six `play_sound` / broadcast seams in
  `tools/game_director.py` (the air- and ground-hit detectors, the two Zakato teleport stamps, the Garu
  detonation, the Sheonite right retreat, and the `sfx bacura` broadcast from the blaster bounce with its Stage
  receiver); the `_audio_failures` structural guard with `test_audio_cues_present` and
  `test_audio_cues_negative_fixtures` in `tests/test_scratch_project.py` (each cue must be played, the five
  Stage-thread cues on the Stage, and `bacura` routed via broadcast and never played on the cloning blaster);
  the Stage-sound list and provenance assertions in `tests/test_hud_glyphs.py`; and the bumped asset count and
  build hash in `tests/test_scratch_project.py`.
- Acceptance criteria: In play (and via the debug spawns) each event is audible with the correct cue — a flying
  kill, a ground kill, a Zakato teleport-in, a Garu detonation, a shot bouncing off a Bacura, and the right
  Sheonite's retreat — while the left Sheonite leaves silently and the base music, start jingle, death, bomb,
  blaster and extend sounds are unchanged. The operator playtest confirms the cues fire on the right events and
  nothing regressed in the existing audio.
- Fidelity status: Each of the six play points was read at the pin this slice (`osd_sound_start` with the sound
  id in `d0` at the cited lines); the sound-to-event mapping matches the reference within the recorded
  deviations below. The audio media itself is not reference-derived and carries no arcade-confirmed claim.
- License status: The reference states no reusable license; only the instruction-derived play-point mapping is
  transferred, and no source text is reproduced. The six sound files are operator-supplied Xevious (Namco)
  arcade sound effects carrying the same rights status as the other ripped assets in this repository (no
  reusable license specified; Namco copyrighted material); they are recorded in
  `src/xevious/assets/provenance.json` and `assets/game-sounds/manifest.json`. Recording provenance is not a
  claim of permission, and no ownership of the Xevious audio is claimed.
- Known deviations or uncertainty: (1) **Base sounds kept.** Several staged wavs duplicate sounds already in the
  base project (music, game-start, player-death, blaster fire); those are intentionally left on the working base
  sounds and not re-committed. (2) **Unbuilt-behavior cues deferred.** Staged sounds whose behavior is not yet
  built (Andor Genesis, the bonus flag, name-entry and credit jingles) are not committed or wired this slice;
  they belong to their own future slices, per the per-slice audio-commit convention. (3) **Source page not
  individually recorded.** The exact upstream page for each wav was not recorded; provenance names the plausible
  family (the Sounds Spriters Resource Xevious audio) without asserting an unverified per-file URL. (4)
  **Fire-and-forget playback.** Each cue is a `play_sound` (start without waiting), so overlapping events layer
  their sounds, matching the arcade's independent one-shot channels closely enough for the port.
- [x] No assembly or other source code was copied into the Scratch project.
- [x] No arcade ROM files were acquired, opened, extracted, or distributed.
- [x] Any transferred graphics or audio are recorded in `src/xevious/assets/provenance.json`.
