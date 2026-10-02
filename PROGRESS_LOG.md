# Progress log: lyrics tool (`mt lyrics`)

Last updated: 2026-10-01 (session 3, end). Newest information is at the top of each section.

## Session 3: bug hunt against the real library, faster tag reads, a menu gap closed

Asked to "fix bugs, make it faster, add features, make lyric finding better if possible" with no
narrower scope. The Transcend drive was mounted and readable this session (unlike some earlier
ones), so this was done by **probing the real, live lrclib.net API read-only** (no writes to the
library: `--dry-run`/`--list-missing` only, plus small standalone scripts calling `lyrics_fetch`
directly) against a random sample of the 912 songs `lyrics_checked.json` has recorded as
`notfound`, instead of guessing at what might help.

- **Confirmed by live sampling:** the large majority of "not found" songs are genuinely not on
  lrclib (instrumental game/anime OST tracks - many literally marked `instrumental: true` on
  lrclib itself, e.g. Ogryzek's catalogue -, niche SoundCloud/electronic producers, freestyles,
  commercials). This matches session 2's suspicion in `FUTURE_EDITS.md` and is now checked rather
  than assumed.
- **One real bug found and fixed this way**: `lyrics_fetch._tidy()` (the "-strip `(feat. X)` /
  `[Official Video]`" cleanup used to build search queries) stripped a title down to `""` whenever
  the *entire* title was one bracketed group, e.g. `"(Dadadadadaru / Amala ft.Miku VS Teto)"` - a
  real Vocaloid-style filename in the library. Confirmed live that lrclib has `雨良 - Dadadadadaru`
  at a duration 1.4s from the file's - a record this bug made unsearchable. Fixed so the brackets
  are only removed when something is left outside them.
- **Tried and rejected, with evidence**: wiring the file's Album tag into lrclib's exact "get"
  lookup (the parameter already existed in `fetch()` but nothing ever populated it). Tested live
  against 20 real songs that already have lyrics: 0 misses turned into hits, 1 hit turned into a
  miss (a compilation album spelled differently on lrclib). Net negative for a "right lyrics or
  none" tool - left out. Recorded here so a future session doesn't re-try the same idea blind.
- **lrclib.net started answering HTTP 503 ("busy") partway through a larger (250-song), unthrottled
  probe script** - a reminder that `lyrics_fetch.fetch()`'s own retry/backoff exists for a reason;
  a flat sequential loop without it will get rate-limited by their free service. Don't repeat that
  probe at that volume without pacing it.
- **Performance**: `find_lyrics.py` was opening every song's file twice with mutagen - once in
  `read_tags()` for artist/title, once more in `duration_of()` just for the length. `read_tags()`
  now returns the duration too (one read covers both); measured ~16x faster for that step on a
  300-file sample of the real library (dwarfed by network latency in a normal run, but it's real
  waste, and it's what `--offline` mode was paying for on every song).
- **Menu gap closed**: the interactive "Folders" screen only ever edited `album_art.folders` -
  there was no menu path to change `artist_art.folders` or `lyrics.folders` at all (only
  `mt set artist_art.folders "..."` reached them). Generalized to one `edit_folders(section, label)`
  screen used for all three.
- **New `--report FILE`** for `mt lyrics`: saves the not-found list as plain text (path + reason),
  answering a "should do" from session 2's list.
- Claude's `max_tokens` raised 8192 -> 16384 (a robustness fix, not verified live - no
  `ANTHROPIC_API_KEY` in this environment either, same as sessions 1 and 2).
- Tests: **128 pass** (121 from session 2 + 7 new: the bracket-title bug, the new query variant,
  `--report`, and three for the generalized Folders menu).
- Not done: the library itself wasn't touched (no real `mt lyrics` run this session, by design -
  everything above came from read-only probes). The 1,029/912-ish "not found" count from session 2
  is still mostly unchanged; this session's fixes will only show up in the counts the next time
  `mt lyrics` is actually run for real.

## Where things stand

- **The real library has now actually been run for real, and you chose to keep the result.** This is no
  longer a read-only project: `/Volumes/Transcend/Music/Music` now has real `.lrc`/`.html` files the tool
  wrote, and real `.lrc.bak` backups of your originals. See "Real run against the whole library" below for
  exactly what happened and the current counts.
- The code itself is **still uncommitted** in the working tree, on top of `20a90c7` (branch `main`) — nothing
  has been committed this session; see "Do next" in `FUTURE_EDITS.md`. This is independent of the library
  having been written to: the library changes came from running the already-working tool, not from
  uncommitted code doing anything unusual.
  Changed: `.gitignore`, `CHANGELOG.md`, `README.md`, `common.py`, `config.example.toml`, `find_artist_art.py`,
  `find_lyrics.py`, `interactive.py`, `lyrics_fetch.py`, `lyrics_render.py`, `lyrics_romanize.py`,
  `lyrics_translate.py`, `lyrics_ui.py`, `music-tools`, `requirements.txt`, `tests/test_core.py`.
  New: `lyrics_lang.py`, `lyrics_local.py`, `lyrics_unromanize.py`, `tests/test_lyrics.py`, this file, `FUTURE_EDITS.md`.
- Tests: **121 pass** (`./setup.sh`, then `.venv/bin/python -m unittest discover -s tests`; 118 from session 1
  + 3 new for session 2's `backup_dir` / `--restore-lrc` work).
- No `ANTHROPIC_API_KEY` or `DEEPL_API_KEY` in this environment, so "Try a Claude key" is still not done.

## Real run against the whole library (session 2, via the interactive menu)

- You drove the lyrics menu by hand (the thing flagged as untested in session 1). First run:
  `--list-missing --dry-run` (both "List songs without lyrics only" and "Preview only" checked) — fully
  read-only, just listed the ~3,846 songs without lyrics yet.
- Second run: went back in, unchecked list-missing, **but this time "Preview only" was off too** — so it was
  a real, bare `mt lyrics` (no folder scope, no `--dry-run`) against the whole library. It hit the "705 songs
  already have a `.lrc` of your own — add the translation?" prompt on your real terminal and proceeded, so you
  answered yes to that at the time.
- **Result:** 832 songs saved (original + romanization + English), 1,985 already-English songs correctly
  skipped, 1,029 failed (not on lrclib.net, or it was 503-busy — these just retry next run) and **zero write
  errors**. Checked the log line by line for anything besides "not found" / "busy" / one "couldn't translate"
  (which correctly fell back to partial: original + romanization saved, English to come next run).
- **Integrity spot-check:** `Ado/Ado - Adoの歌ってみたアルバム/01 Ado - Dried Flowers.lrc` — original intact
  in the matching `.lrc.bak`, new `.lrc` correctly formatted (one original/romaji/English triplet per
  timestamp), no duplication.
- **You said to keep it.** No `--restore-lrc` was run. Current real-drive counts: 1,410 `.lrc.bak` files,
  832 `.html` pages, 2,414 `.lrc` files (tool-written and your own combined).
- **One loose thread, not chased down:** about half the `.bak` files (705 of 1,410) turned out to predate
  this session by hours — real file-creation time ~00:32 that same day, before this session started — meaning
  a real (non-preview) run had already happened earlier, contradicting session 1's log entry that claimed
  every run against the real drive was read-only. Couldn't find a matching log in `logs/` for that time,
  so it's unclear whether that was an unlogged run, a run from outside a Claude session, or something else.
  Session 2's run picked up where it left off (finished the missing `.html` pages, processed the rest).
  Not investigated further since nothing was lost either way — flagging it here in case it matters later
  (e.g. if `logs/` is ever expected to be a complete record of every real run).
- **Takeaway for next time:** the interactive menu's "Preview only" checkbox defaults to **off**. Nothing
  stops a real run from happening if you don't explicitly check it, and the only other guard is the one
  y/N prompt for files you already own — everything else (lrclib lookups, new `.lrc`/`.html` for songs with
  none yet) proceeds without asking, by design. That's the intended behavior, not a bug, but worth remembering
  when driving the menu by hand.

## What was asked, in order, and what was done (session 2)

| Request | Result |
| --- | --- |
| Continue from the log, then write back to the log when done | Worked through "Do next" in `FUTURE_EDITS.md`: installed + retested, staged real dry-runs, decided and built the `.bak` policy |
| (follow-up) What to do with the interactive menu checklist | Walked through it live: a safe list-missing+preview run, then talked through the checkboxes for a proper fetch+translate preview |
| "ran it, here's the output" (pasted a real, non-preview full-library run) | Verified the run's integrity end to end (log review, file spot-check, current drive counts) instead of assuming either success or damage |
| "keep it" | No further changes to the library; this file updated to record the real state |

## Verified live (real services / real library, session 2)

- `--dry-run --limit 20 --show`, whole library: 15/20 English (skipped), 5/20 not found on lrclib (plausible
  reasons), 0 written.
- `--dry-run --show "Kenshi Yonezu"`: 8 songs, original+romaji+English printed correctly.
- `find_artist_art.py --list-missing`, whole library (3,851 songs): ran clean, 147 artists without a picture,
  no regressions from the `folder_problem()` fix.
- **The real, non-preview whole-library run** — see above. This is by far the most thorough live verification
  this tool has had: 3,846 real songs, real lrclib/Google/MyMemory traffic, real writes, real backups, and a
  real "update my own .lrc" prompt answered for real. Nothing broke.
- `backup_path_for()` sanity-checked directly (temp dir): no-`backup_dir` keeps `<name>.bak` next to the file;
  `backup_dir` mirrors the folder; a file outside `music_dir` falls back to a flat name instead of crashing.
  (Not exercised on the real library this session since `lyrics.backup_dir` isn't set in `config.toml`.)

## Bugs found and fixed (session 2)

| Bug | Fix |
| --- | --- |
| `lyrics.claude_model` defaulted to `"claude-sonnet-5-5"`, not a real model id — every Claude-backend call would fail | Changed default to `"claude-sonnet-5"` in `common.py`, `config.example.toml`, `lyrics_translate.py`. Still untested live (no key here). |
| `find_artist_art.py` said "drive unplugged?" for any unreachable folder, including a macOS privacy block | Now uses `common.folder_problem()`, same as `find_lyrics.py`. |

Session 1's bug table (dots in filenames, wrong-artist matches, credit lines, bilingual `.lrc`, etc.) is
unchanged and still fixed; see the previous revision of this file.

## Behaviour changes to be aware of (new this session)

- **New setting `lyrics.backup_dir`** (default `""`, unchanged behaviour): collects every `<song>.lrc.bak`
  into one mirrored folder instead of scattering them next to each song. Not set in this library yet — the
  1,410 `.bak` files from tonight's run are all sitting next to their songs, the old way.
- **New flag `--restore-lrc`**: puts a `.lrc.bak` back over its `.lrc` and removes the backup; no lookups,
  respects `--dry-run`. Available now if you ever want to undo tonight's `.lrc` translations (the new `.lrc`/
  `.html` files for songs that had *no* prior `.lrc` aren't covered by this — those would need deleting by
  hand if ever unwanted, since there's nothing to "restore" them to).
- Both wired into the interactive menu (`mt` → Lyrics) as two more checkboxes.
- All prior behaviour changes from session 1 are unchanged.

## Not verified / caveats

- The **Claude and DeepL backends were only tested against fakes**; still no live key in this environment.
  The `claude_model` bug fix means it's worth trying once you have a key.
- **DeepL** itself: never live-tested (`DEEPL_API_KEY`), same as session 1.
- The **1,029 "not done" songs** from tonight's run haven't been individually checked for whether lrclib
  simply doesn't have them (expected, nothing to do) vs. a title/artist parsing issue worth fixing; the failure
  reasons in the log (mostly "no lyrics found on lrclib.net", some 503s) suggest the former, but it's from a
  quick read, not a full audit.
- Everything from session 1's caveats list not superseded above still applies (romaji losing kanji
  distinctions, Google's translate endpoint being unofficial, etc.).
