# Audio Recognizer: AudD + Local Fingerprints

An experimental Music Assistant audio-analysis provider that recognizes music on live
audio sources, such as a turntable on a line-in, and learns your music over time.

**AudD discovers. Local fingerprints remember. Music Assistant displays.**

1. Use a live AudioSource that allows audio analysis (for example Sendspin Source or VBAN Receiver).
2. The provider listens to that live input.
3. Tracks it has heard before are recognized **locally**, with no external service.
4. Unknown tracks are sent to [AudD](https://audd.io) for identification.
5. It then learns local fingerprints (Panako, via audalign) so the next play is recognized locally.
6. The recognized title, artist, album and artwork are shown on the Music Assistant player.

- No Music Assistant core patch is needed. Requires a Music Assistant Nightly (2.11 dev)
  build with AudioSource analysis support.
- An AudD API token is needed to identify music that hasn't been learned yet. Without one,
  only already-learned tracks are recognized.
- Local fingerprints are independent of AudD and stay on your Music Assistant server.
- Track changes are detected from the silence between tracks. Records without gaps
  (live albums, DJ mixes) are not handled well yet.

Status: experimental. See `CLAUDE.md` for development notes.
