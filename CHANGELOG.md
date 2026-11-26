# Changelog

## 0.10.0 (2026-11-26)

More reliable and lighter: fewer wrong titles, no more hiccups in the music caused by the
recognizer, a track that survives a Wi-Fi drop, and a database that is upgraded in place.

### ⚠️ Worth knowing

- **The fingerprint database is upgraded on the first start** (a new column; takes a fraction of
  a second, nothing is lost; the log says `Fingerprint database upgraded from schema version 1 to
  2`). **Going back to 0.9 afterwards starts with an empty database:** 0.9 cannot read the new
  format and sets the file aside as `fingerprints.sqlite3.incompatible` in the provider's data
  folder (kept, not deleted). Stay on 0.10 or later to keep what was learned.
- **A source must allow analysis.** In the source list, a source whose own setting *Allow
  analysis of the audio input* is off is now marked *(analysis not allowed)* and greyed out.
  Turn that setting on in the source's own settings (the Sendspin player with the audio input,
  or the VBAN Receiver) to choose it.
- **AudD requests:** usually unchanged. A few new checks can each cost one extra request, and all
  of them count within the existing limit of 20 requests per track, never closer than 30 seconds
  apart (see *Changed* below).

### Improved

- **No more hiccups in the music caused by the recognizer.** Music Assistant runs every provider
  in one process; when the recognizer kept it busy too long, the live input got short gaps of
  silence (about 0.1 s) that you could hear on your speakers. Fingerprinting and adding up the
  evidence now take a fraction of the work (identical fingerprints), heavy work is kept off
  Music Assistant's main loop, and Python's memory clean-up is done in the silence between
  tracks. In tests: before, 46 audible hiccups in 40 minutes of a live record; now usually none
  in the music on a whole side.
- **Fewer wrong titles.** A match that adds up over several checks now also needs enough
  fingerprints of the ordinary kind (not only the extra ones stored for the first 30 seconds).
  Replayed over all test recordings: 7 wrong titles in 318 known tracks before, 0 now; a few
  tracks are recognized via AudD a little later instead (~15 s).
- **A Wi-Fi drop no longer ends the track.** When the audio input reconnects, Music Assistant
  delivers silence for a while (in a test, Wi-Fi off for 30 seconds gave 45 seconds of
  silence). An identified track now continues through up to 60 seconds of such digital silence:
  the title stays and nothing is asked again. Longer still ends it as before.
- **AudD gets cleaner samples.** When the stream has many short dropouts (for example on weak
  Wi-Fi), the provider waits once for a cleaner moment before sending a sample.
- **Needle drops on records without gaps:** when AudD's position in the song says the track you
  dropped the needle into has already ended, AudD is asked once more at that moment (up to 3
  tries), so the next track gets its own title instead of running on under the old one.
- **A title with an implausible position** (about 4 % of AudD answers, e.g. a hidden track that
  shares an intro) is checked once more a little later.
- **Track lengths on gapless records:** the length of a track is also measured when the next
  track starts without a gap, so the end of the next play is found more precisely.

### Changed

- Every setting now has a help text (the "?" in the settings).
- **Troubleshooting** (advanced): *Maximum recording* now goes up to 240 minutes (was 1440).
  With *Record the input stream* on, the provider also keeps one extra copy of the fingerprint
  database in its data folder (about the size of the database), so that the next recording can
  start without copying it again; and it logs short timing lines about its own work.
- Fixed: the README said no audio of known tracks is sent to AudD; a known track without a stored
  length is sent once (0.9 behaviour, now documented).

## 0.9.0 (2026-11-21)

Records without silence between tracks, live records, long pieces and needle drops now work much
better; fewer wrong titles and duplicates; and the provider shows where you are in the song.

### New

- **Where you are in the song:** when the provider knows where the song started and how long it
  is, Music Assistant shows the position and length (a progress bar), adjusted to your turntable's
  speed correction.
- **Records without silence between tracks** (live albums, continuous sides): the provider uses
  the track length to work out when a track should end, stops learning just before it and starts
  listening for the next track even without a gap (log: `Track boundary (no gap)`). When the next
  track is recognized, it corrects where the previous one really ended and keeps what was learned
  for the right track.
- **Track lengths measured on your record:** the length of a track is measured at its end (a
  silence or the next track); a length measured twice replaces AudD's, which is often that of
  another edit. A track recognized locally that has no length yet gets one AudD request, once, to
  learn it.
- **Long pieces are learned whole** (up to 30 minutes when the start and length are known; until
  now learning stopped after 10 minutes, so the second half of an 18-minute suite was never
  learned).
- **Needle drops:** when you put the needle down in the middle of a track, it is recognized as
  before; the provider now also works out *where* in the song you are, usually 10–20 seconds
  later, and Music Assistant jumps to that time. Dropping the needle mid-track no longer pushes
  earlier, complete plays out of the database.
- **AudD finds more of your records:** AudD often holds several uploads of one recording that
  differ by 1–2 % in speed. When AudD misses a track, the next tries alternate between your speed
  correction, 1 % slower and 1 % faster, at no extra requests. On a long unknown stretch AudD is
  now asked up to 20 times (was 8); the new advanced setting *Ask AudD more often (live and
  gapless records)* asks every 30 seconds instead of 90.
- **No stale title:** when a new track is not recognized at the first check, Music Assistant shows
  your source again instead of the previous track; also after 30 seconds of silence (the record
  has ended).
- **Merging duplicates** (advanced): `scripts/merge-tracks.sh` merges two tracks AudD named in two
  ways; the other name is remembered.
- **Troubleshooting** (advanced): *Maximum recording (minutes)* (default 60, up to 1440 = 24
  hours); every recording gets the recognizer's log of that session; a silence of 30 seconds or
  more starts a new recording; recordings are **deleted automatically 3 days after they started**.
  *Save AudD samples for debugging* keeps the last 250 samples (was 40).

### Changed and fixed

- **Fewer wrong titles:** a single local check needs 6 matching fingerprints (was 5) to show a
  title at once, two weak checks must point at the same moment of an earlier play, and an unknown
  track needs more evidence the longer it plays without AudD. (In tests one song of another record
  reached 5 by chance, and a Kurt Vile track was briefly shown as Barry White.)
- **No false track ends:** AudD's position from another edit of a recording (e.g. a 7:18 version
  of a 5:13 single) no longer ends the track mid-song; a quiet moment inside a song is recognized
  as such (the song goes on, and it is not taken for the track's length); a song that repeats
  itself can no longer put its end minutes too early.
- **Fewer duplicate tracks:** edition suffixes such as *(Album Version)*, *(Original Album
  Version)* or *(2016 Remaster)*, commas and other punctuation, and square brackets no longer make
  a separate track. Existing duplicates are merged when the provider starts. *(Live)*, *(Remix)*
  or *(Radio Edit)* still count as different recordings.
- **Stream dropouts** (a second or two of digital silence from the audio input) no longer split a
  track; longer digital silence (over 10 seconds) still ends it.
- When a passage started on the tail of the previous track, that tail is no longer learned under
  the new song; a measured length no longer depends on the speed correction setting.
- *Recognize all live audio sources* is now an advanced setting, after *Audio sources to
  recognize*: choosing your turntable is the normal way. If you had it on, it stays on.

### ⚠️ Worth knowing

- **Music Assistant moved the Sendspin input** from the *Sendspin Source* plugin into the Sendspin
  provider (Nightly of 2026-11-19 and later, and 2.11.0b10). A turntable selected under the old
  plugin keeps being recognized; re-select it once under *Audio sources to recognize* to make that
  permanent. *Allow analysis of the audio input* (on by default) is a setting of the Sendspin
  player that has the input.
- Tracks learned before this version have no stored length yet: expect about one AudD request
  per known track, once, the first time you play it again.

## 0.8.0 (2026-11-18)

Records you have played before are now recognized locally much more often and faster, so fewer
AudD requests are needed. Tested on a real turntable (two LP sides, 11 tracks played before):
**8 of 11 recognized locally, on average after ~16 seconds**, no wrong titles. On the same Barry
White side, 0.7 recognized 1 of 6 tracks locally; 0.8 recognized 5 of 6.

### ⚠️ Worth knowing

- **Disk space:** the new fast start (on by default) stores the first 30 seconds of every track
  with more fingerprints: about **35 % more disk space** for the fingerprint database (roughly
  0.7 MB per play of a 3-minute track instead of 0.5 MB). Turn off "Faster recognition at the
  start of known tracks" in the provider settings if you prefer a smaller database.
- Nothing else to do: your existing database is kept, and tracks you learned before also benefit
  (fully from the next time you play them).

### New

- **Faster recognition at the start of known tracks** (setting, on by default). The first 30
  seconds of every track are stored with more fingerprints, so a record you play again from the
  start is usually recognized locally within **12–18 seconds** instead of ~25 s, and without an
  AudD request. Measured offline on a real recording (Barry White, played twice): on the second
  play 5 of 6 tracks were recognized locally (before: 1 of 6), AudD requests 5 → 1, no wrong
  titles. Costs about **35 % more disk space**; turn it off for a very large collection. Tracks
  you learned before benefit from the next time you play them.

### Improved

- **Known tracks are recognized more reliably, also on a worn or slightly different play.** On
  vinyl only a few fingerprints repeat exactly on every play, so the provider now adds up the
  evidence of a whole track, but only fingerprints that line up *in time* with an earlier play
  (a real replay keeps the same timing; coincidental matches don't). Measured offline on three
  real recordings: replays recognized locally after **18–24 seconds**, including tracks that
  previously needed AudD or were not recognized at all, and **no wrong titles** in ten unknown
  tracks of 3–5 minutes each. This replaces the 0.7 rule that summed votes over the last four
  lookups without checking the timing.
- When AudD is due but local recognition is already close to a known track, the provider waits
  one more moment for local confirmation instead of spending an AudD request.


### Fixed

- **More missing album art.** Some ordinary-looking Deezer cover links turn out to redirect to
  Deezer's empty "no cover" image. The provider now checks a Deezer cover once when AudD provides
  it (a single small request to Deezer's image server) and ignores it when it is empty, so a real
  cover can fill it in later. Covers stored earlier are checked once at startup.
- No more harmless "invalid escape sequence" warnings from a dependency (pydub) in the Music
  Assistant log on the first start after installing or updating.

## 0.7.0 (2026-11-18)

Much better recognition on real vinyl, a database that stays small, and tools to find out why a
track isn't recognized. Tested on a real turntable: with the new speed correction **10 of 10
tracks were identified** (before: about 6 in 10), with **0.9 AudD requests per track** (before:
about 3) and in **~27 seconds** (before: ~56 s); known tracks were recognized locally in ~20–25 s.

### ⚠️ Action needed: choose your audio source

- **"Recognize all live audio sources" is now off by default**, following the Music Assistant
  maintainers' advice: you choose which live sources (e.g. your turntable) are analysed.
  **If you never changed this setting, recognition stops after the update until you open the
  provider's settings and select your source** under "Audio sources to recognize" (or turn
  "Recognize all live audio sources" back on). The log tells you when a source is skipped, and
  warns at startup when no source is selected.

### New

- **Speed correction for AudD (%)** (advanced setting, default 0). AudD is sensitive to a
  turntable's speed: a turntable running slightly fast made AudD miss about 4 in 10 tracks,
  including well-known ones. Samples sent to AudD can now be played a little slower or faster;
  local recognition is not affected. **For a turntable that runs fast, start with -2.** The README
  explains how to measure your turntable with a phone app.
- **Debugging aids** (advanced settings, off by default): *Save AudD samples for debugging* keeps
  the last 40 samples sent to AudD with their outcome and measurements; *Record the input stream
  for debugging* records the live input (up to 60 minutes). A no-match now logs AudD's reason.

### Improved

- **Faster local recognition of replays.** Weak but genuine matches now add up over consecutive
  lookups of the same track, so a replay is often recognized without asking AudD.
- **The database stays small.** Each track keeps the fingerprints of its **two most recent plays**;
  older plays are removed automatically (suggested by the Music Assistant maintainers).
- **Updates can never stop the provider because of the database.** An incompatible or damaged
  database file is moved aside (`fingerprints.sqlite3.incompatible`) and a new one is started.
- **Removing the provider also removes its fingerprint database.**
- Clearer logs: every identification says how and when (`Identified … via local after 22s`).

### Fixed

- **Repetitive audio could look like a strong local match.** In music with a repeating pattern
  (a drum machine, a held note) one fingerprint repeated in a sample counted as several votes, so
  one accidental match could look strong enough to show a wrong title. Each distinct fingerprint
  now counts once. (Found in a test with *In the Air Tonight*; no wrong title was shown.)
- **The album no longer jumps between plays.** AudD often names a different compilation album on
  every identification; the first stored metadata is now kept and later results only fill in what
  is missing. The ISRC (recording code) is stored when available.
- **Missing album art on some tracks.** Deezer's empty "no cover" image was stored as artwork. It is
  now ignored, a real cover can replace it, and stored placeholders are cleaned up at startup.
- Quiet passages no longer log a harmless "divide by zero" warning.

### Hardening

- A learned window with an abnormal number of fingerprints is not stored, so a noisy or broken
  source can't blow up the database.

## 0.6.0 (2026-11-17): first public release

First release of **Audio Recognizer: AudD + Local Fingerprints** as a Music Assistant community
provider. It recognizes the music playing on a live audio source, such as a turntable, and
shows title, artist, album and artwork on the player. It learns your music as you play it, so
records you've played before are recognized locally, without an external service.

### Requirements

- **Music Assistant Nightly** (2.11 development builds from November 2026 or later). For now this
  is required: live audio source analysis is not in a stable or beta release yet.
- A live audio source that allows audio analysis, such as *Sendspin Source* or *VBAN Receiver*.
- An **AudD API token** to identify music that hasn't been learned yet. AudD is a paid service with
  a free trial (300 requests in 14 days); see [audd.io/pricing](https://audd.io/pricing/). Without
  a token, only already-learned tracks are recognized.

### Features

- Recognizes music on live audio sources through Music Assistant's official audio-analysis API.
  No changes to Music Assistant are needed.
- Local recognition first: Panako fingerprints (via audalign), stored at a deterministic 6.25%
  density in a local SQLite database that survives restarts.
- AudD as discovery service for unknown tracks. Requests are throttled per track and per
  stream, and back off only on real errors.
- Learns every track it identifies, including its start, for future local recognition.
- Track changes are detected from the silence between tracks. A track never "recognizes
  itself" from fingerprints learned seconds earlier.
- Works whether Music Assistant plays the source at 44.1 or 48 kHz (fingerprints are
  normalized to one rate).
- Artwork from Apple Music, Spotify or Deezer when AudD provides it.
- Settings: AudD token, which sources to recognize, and (advanced) silence detection
  thresholds. English and Dutch texts.

### Known limitations

- **Records without silence between tracks** (DJ mixes, live albums, continuous sides,
  crossfades) **are not supported yet.** The previous title stays on screen, and the next track
  may be learned under the previous track's name. Turn the provider off for such records.
  Support is planned.
- The album shown is AudD's, often a digital compilation rather than the LP you are playing.
- Recognition can take 30–60 seconds into a track. A replayed track is recognized locally,
  sometimes only after ~45 seconds; faster local recognition is planned for the next version.
- Learned tracks can't be viewed or removed individually yet.
- The last track stays visible during silence until the next one is recognized. The time shown
  on the player is the time since the source started, not the position in the song.

### Privacy

Only ~12-second audio samples of music that isn't recognized locally are sent to AudD,
together with your API token. Local fingerprints are computed by this provider, stay on your
Music Assistant server, and are never uploaded.
