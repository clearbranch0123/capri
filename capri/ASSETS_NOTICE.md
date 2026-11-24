# Assets Provenance & Noncommercial Notice

## Upstream Required Notices (`lachydotmcg/capri-desktop`)

The bundled fan-made character sprites and companion visual assets derive in part from [`lachydotmcg/capri-desktop`](https://github.com/lachydotmcg/capri-desktop), licensed under the **PolyForm Noncommercial License 1.0.0** (<https://polyformproject.org/licenses/noncommercial/1.0.0>). Pursuant to the `Notices` clause of that license, the upstream `Required Notice` lines are reproduced verbatim below:

```text
Required Notice: Copyright 2026 Lachy (https://github.com/lachydotmcg)
Required Notice: Anyone who uses Capri's code, art, animations or any of its other assets must credit the Capri project (https://github.com/lachydotmcg/capri-desktop). Capri's art and animations are by her artist, who prefers to stay private. Parts made by others keep their own licences, listed in its README.
```

Additional upstream provenance preserved from `capri-desktop` (`THIRD-PARTY-NOTICES.md`): original husk-mode inspiration credits Mochiiwuff (<https://github.com/mochiiwuff/capri-desktop>), and upstream click animation credits **mixmix312352**.

---

## Separation of Code License and Asset Terms

The **Capri Linux** source code (Python modules under `capri/`, shell scripts, tests, and documentation) is licensed under the **MIT License** (see [`LICENSE`](LICENSE)).

**The MIT License does NOT extend to artwork, character sprites, 3D avatar files, voice reference audio, or fan-derived media under `capri/assets/`.** Those assets are distributed strictly for personal, noncommercial use under the terms and notices above (<https://polyformproject.org/licenses/noncommercial/1.0.0>).

## Inventory & Provenance (`capri/assets/`)

| Path / Group | Description | Origin & Usage Terms |
|---|---|---|
| `capri/assets/sprites/*`, `capri/assets/sprites_v2/*` | 2D desktop companion sprites (`idle`, `happy`, `thinking`, `speaking`, `sleep`, `perch`, etc.) | Character art and animations credited to the Capri project ([`lachydotmcg/capri-desktop`](https://github.com/lachydotmcg/capri-desktop)) under the **PolyForm Noncommercial License 1.0.0** (<https://polyformproject.org/licenses/noncommercial/1.0.0>); strictly noncommercial use only with the verbatim `Required Notice` lines above preserved. |
| `capri/assets/3d/*` | 3D QML scene, meshes, and textures (`Avatar3D.qml`, `meshes/`, `textures/`) | Fan-derived noncommercial companion avatar assets. Not covered by the MIT code license; noncommercial use only. |
| `capri/assets/avatar.png`, `chip.png`, `chip-source.png`, `capri_face_*.png`, `capri_reset_*.png` | Relic biochip artwork, avatar portraits, and reset dialog artwork | Fan-made / derivative visual assets for Capri Linux. Strictly noncommercial use only. |
| `capri/assets/capri_voice_clean.wav`, `capri_voice_reference.wav` | Optional local voice reference samples for voice synthesis / cloning experiments | Noncommercial / personal reference clips only; **not** covered by the MIT license. Third-party dubbing clips are intentionally excluded from public distribution. Do not redistribute commercially or misrepresent voice identity. |
| `capri/assets/ui_click.wav`, `ui_glitch.wav`, `wake_chirp.wav` | Short synthesized UI audio cues | Generated for Capri Linux UI feedback; bundled for noncommercial/personal use with the app. |
| `capri/assets/capri-cyberdeck.desktop` | FreeDesktop `.desktop` launcher template | Authored for Capri Linux (MIT). |
| `docs/screenshots/*.png`, `docs/screenshots/capri-demo.webp` | Application UI screenshots and animated demo rendered from Capri Cyberdeck, Relic Dashboard, and Desktop Avatar | Project documentation media; displays the noncommercial artwork above and remains subject to the same noncommercial terms for depicted character art. |

## Third-Party Trademarks & Unofficial Fan-Project Disclaimer

- *Cyberpunk: Edgerunners* and related names/concepts belong to their respective rights holders (**CD PROJEKT RED** / **Studio TRIGGER** / **Netflix**). Capri Linux is an independent, unofficial, noncommercial fan companion project and is **not affiliated with, endorsed by, or associated with CD PROJEKT RED, Studio TRIGGER, or Netflix**.
- Upstream desktop companion attribution: [`lachydotmcg/capri-desktop`](https://github.com/lachydotmcg/capri-desktop) (**PolyForm Noncommercial License 1.0.0**, <https://polyformproject.org/licenses/noncommercial/1.0.0>).

## Downstream Forks & Packaging

If you redistribute Capri Linux or its wheel/archive with `capri/assets/` included, you **must** retain this `ASSETS_NOTICE.md` file (including both verbatim `Required Notice:` lines and the PolyForm Noncommercial License 1.0.0 URL above) and use it strictly for noncommercial purposes. For any commercial fork or redistribution, you **must remove and replace** all noncommercial artwork and audio assets under `capri/assets/` with your own properly licensed assets.
