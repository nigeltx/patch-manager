Patch Manager 0.2 - milestones 1 and 2 (Library, Audition)  (19 Nov 2026)
Spec: Patch_Manager_Spec_v1.5.pdf - M1 (sections 4, 5.1) and M2 (sections 3, 6.1, 6.2, 6.5, 6.6).

To install
  Run PatchManager-Setup-x.y.z.exe (from Drive: Studio > Software > Patch Manager, or the GitHub
  Releases page). It installs for your user only - no admin prompt, no Python needed - and puts
  Patch Manager in the Start menu. A newer Setup file updates it in place.
  Close the bench test apps first - only one program should talk to the synths.

Patch session (M2)
  1. Give the PC the synths: the hub cable to the Alienware now, the sharing switch's button later.
  2. The pills in the toolbar show Take 5, Sub 37 and KeyLab. Green = connected and checked.
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

Not yet
  Deluge audition over USB, Send to slot, backups (M3-M5).

Where things are kept
  %LOCALAPPDATA%\PatchManager\library.db (the database) and crash log.txt if ever needed.
  Ratings, tags, notes and crates are stored against each sound, so they survive rescans.

Code
  Private GitHub repo nigeltx/patch-manager. Every change is built into a new installer there.
  patchmgr\  the app: library\ (readers, scanner, database, search), midi\ (ports, phrases),
             drivers\ (Take 5, Sub 37), ui\ (windows)
  tests\     automated tests (pip install pytest, then: python -m pytest tests)
