# Assets Provenance & Noncommercial Notice

## Separation of Code License and Asset Terms

The **Capri Linux** source code (Python modules under `capri/`, shell scripts, tests, and documentation) is licensed under the **MIT License** (see [`LICENSE`](LICENSE)).

**The MIT License does NOT automatically extend to artwork, character sprites, 3D avatar files, voice reference audio, or fan-derived media under `capri/assets/`.** Those assets are distributed strictly for personal, noncommercial use with attribution to their respective origins.

## Inventory & Provenance (`capri/assets/`)

| Path / Group | Description | Origin & Usage Terms |
|---|---|---|
| `capri/assets/sprites/*`, `capri/assets/sprites_v2/*` | 2D desktop companion sprites (`idle`, `happy`, `thinking`, `speaking`, `sleep`, `perch`, etc.) | Fan-made character art inspired by *Cyberpunk: Edgerunners 2* and the open-source companion project [`lachydotmcg/capri-desktop`](https://github.com/lachydotmcg/capri-desktop) (**PolyForm Noncommercial License 1.0.0**). Strictly noncommercial use only. |
| `capri/assets/3d/*` | 3D QML scene, meshes, and textures (`Avatar3D.qml`, `meshes/`, `textures/`) | Fan-derived noncommercial companion avatar assets. Not covered by the MIT code license; noncommercial use only. |
| `capri/assets/avatar.png`, `chip.png`, `chip-source.png`, `capri_face_*.png`, `capri_reset_*.png` | Relic biochip artwork, avatar portraits, and reset dialog artwork | Fan-made / derivative visual assets for Capri Linux. Strictly noncommercial use only. |
| `capri/assets/capri_voice_clean.wav`, `capri_voice_reference.wav` | Optional local voice reference samples for voice synthesis / cloning experiments | Noncommercial / personal reference clips only; **not** covered by the MIT license. Third-party dubbing clips are intentionally excluded from public distribution. Do not redistribute commercially or misrepresent voice identity. |
| `capri/assets/ui_click.wav`, `ui_glitch.wav`, `wake_chirp.wav` | Short synthesized UI audio cues | Generated for Capri Linux UI feedback; bundled for noncommercial/personal use with the app. |
| `capri/assets/capri-cyberdeck.desktop` | FreeDesktop `.desktop` launcher template | Authored for Capri Linux (MIT). |
| `docs/screenshots/*.png`, `docs/screenshots/capri-demo.webp` | Application UI screenshots and animated demo rendered from Capri Cyberdeck, Relic Dashboard, and Desktop Avatar | Project documentation media; may display noncommercial artwork above. |

## Third-Party Trademarks & Attribution

- *Cyberpunk: Edgerunners* and related names/concepts belong to their respective rights holders (CD PROJEKT RED / Studio TRIGGER / Netflix). Capri Linux is an independent, unofficial, noncommercial fan companion project and is not affiliated with, endorsed by, or associated with CD PROJEKT RED, Studio TRIGGER, or Netflix.
- Inspiration and desktop companion concept credit: [`lachydotmcg/capri-desktop`](https://github.com/lachydotmcg/capri-desktop) (PolyForm Noncommercial License 1.0.0).

## Downstream Forks & Packaging

If you fork Capri Linux for commercial redistribution, you **must replace** all noncommercial artwork and audio assets under `capri/assets/` with your own properly licensed assets.
