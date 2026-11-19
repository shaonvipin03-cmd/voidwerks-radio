# Voidwerks Radio

Self-hosted internet radio on NixOS: Icecast + Liquidsoap serve a music library as a live MP3 stream.
Plain-HTML player (no framework) with a visualizer, progress bar, artist picker and an optional TV tab.

- **Station picker:** restrict the shared stream to chosen artists (`radio-control.py`).
- **Private side (tailnet only):** download audio from YouTube links with yt-dlp, play it privately,
  or push a track onto the public station (`radio-private.py`).
- **Responsive:** phones to 4K monitors, dark/light theme, lock-screen controls.
- **Exposure:** public page via Tailscale Funnel, private page via Tailscale Serve.

## Use it
Copy this folder into `/etc/nixos/modules/`, import `radio.nix`, and edit the placeholders at the top
(passwords, hostname, Tailscale IP, music folder). Use only music and content you have the right to broadcast.
