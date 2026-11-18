# Changelog

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
