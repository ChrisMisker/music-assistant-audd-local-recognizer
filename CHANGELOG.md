# Changelog

## 0.7.0 (unreleased)

### Improved

- **Faster local recognition of replays.** Weak but genuine matches now add up over consecutive
  lookups for the same track (at least 2 votes per window, 6 in total from 2 or more windows
  within ~24 s). On real vinyl a replay repeats only 2–4 votes per window, so a track that just
  missed the threshold window after window is now recognized sooner, often without an AudD
  call. Accidental matches (1 vote in every real test so far) never count.
- **The database no longer grows without limit.** Each track keeps at most 60 references
  (about two plays); older plays are removed whole, oldest first. Suggested by the Music
  Assistant maintainers.
- **Updates can never stop the provider because of the database.** An incompatible or damaged
  database file is moved aside (`fingerprints.sqlite3.incompatible`) and a new one is started,
  with a warning in the log.
- **Removing the provider also removes its fingerprint database.**
- Clearer logs: every identification logs how and after how many seconds
  (`Identified … via local after 31s`), and local candidates show their accumulated votes.

### Developer

- `scripts/vote-report.sh`: per track play, how and when it was identified, AudD calls and local
  votes, plus the vote distribution of real versus accidental matches.

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
