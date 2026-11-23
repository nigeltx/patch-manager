Patch Manager 0.4.2 - milestones 1-3 (Library, Audition, Backups and factory presets)  (22 Nov 2026)
Spec: Patch_Manager_Spec_v1.8.pdf - M1 (sections 4, 5.1), M2 (sections 3, 6), M3 (sections 4.7, 4.8, 5.3, 5.5, 7.5).

To install
  Run PatchManager-Setup-x.y.z.exe (from Drive: Studio > Software > Patch Manager, or the GitHub
  Releases page). It installs for your user only - no admin prompt, no Python needed - and puts
  Patch Manager in the Start menu. A newer Setup file updates it in place.
  Close the bench test apps first - only one program should talk to the synths.

Patch session (M2)
  1. The synths' USB cables go to the hub on the PC; they stay there (playing goes over DIN).
  2. The pills in the toolbar show Take 5, Sub 37, Syntakt and Deluge. Green = connected and checked.
     Each synth is checked on arrival (device inquiry, SysEx over USB); click a pill for details.
  3. Select a Take 5 or Sub 37 patch and press Enter (or Load to edit buffer). It plays on the
     synth at once and nothing is stored. Tick "Load as I move through the list" to hear each
     patch as you arrow down.
  4. P plays a test phrase (chosen from the patch's role tag; change it in the list), Esc stops
     with all notes off. Hold for arp sends MIDI clock at the tempo shown.
  5. Tick "Play it from the KeyLab" to play the auditioned synth from the KeyLab.
  The status bar warns while an audition is loaded: pressing WRITE (Take 5) or saving (Sub 37)
  on the panel would store the auditioned sound. Put back returns the Take 5 to the sound it had
  when the session started; closing the app does this too.
  Test phrases use channel 16 for the Take 5 (read from the synth) and 15 for the Sub 37.

Library (M1)
  - Lists every patch in Studio > Patches; nothing there is moved, renamed or changed.
  - The same sound in several files is one line; Copies shows how many files it is in.
  - Ratings (keys 1-5, 0 clears), tags (grey = from the file or name; click to confirm), notes,
    crates (right-click > Add to crate), smart crates (Save as smart crate), Problems view.
  - Search with filters - press Help. Space plays a pack preview recording.

Synths menu (M3) - for each synth: Initialize factory presets, Back up, Refresh what is on it
  - Initialize factory presets (do this first, once per synth): reads the synth, keeps the read as a
    dated backup in Studio > Patches > _Patch Manager > Backups, then lists every slot. Slots in the
    factory banks (Take 5 F1-F16, Sub 37 1-9, Syntakt A-G - change them in the dialog) and sounds that
    match a maker's file in a Factory folder are ticked; untick anything that is yours, then Apply.
    Blank init sounds ("Init Preset") are never factory.
  - Factory presets show an amber F in the list. A green row is a sound that is on a synth now;
    Details says which slot and when it was read. Search: factory, onsynth, blank.
  - Take 5: about 80 s, all 256 user and 256 factory programs. Sub 37: press SEND on ALL PRESETS
    (SYSEX page) when the dialog asks - about 3 minutes. Syntakt: banks A-H over USB (close
    Elektron Transfer). Deluge: every preset in SYNTHS over its own USB cable.
  - When an initialized Take 5 or Syntakt connects, the app reads it again in the background so the
    green rows stay current. The Sub 37 updates when you back it up.
  - Nothing is written to any synth by these actions.

Not yet
  Send to slot and the factory lock (M4), Deluge audition over USB.

Where things are kept
  %LOCALAPPDATA%\PatchManager\library.db (the database) and crash log.txt if ever needed.
  Ratings, tags, notes and crates are stored against each sound, so they survive rescans.

Code
  Private GitHub repo nigeltx/patch-manager. Every change is built into a new installer there.
  patchmgr\  the app: library\ (readers, scanner, database, search), midi\ (ports, phrases),
             drivers\ (Take 5, Sub 37, Syntakt, Deluge), ui\ (windows)
  tests\     automated tests (pip install pytest, then: python -m pytest tests)
