# Cutout

A Windows app that removes image backgrounds on your own PC. No uploads, no account.

- **Drop images in** from Explorer, a browser, or paste a link (Ctrl+V).
- **Review before saving.** Each cutout waits in the preview; save the ones you like, or **Retry** with another model or refined edges.
- **Frame the export.** Crop by hand (free or locked to a ratio), or centre the subject in 1:1, 4:5, 9:16, 16:9 and more, at full size or a set long edge.
- **Any background.** Keep it transparent, or fill with any colour: picker, hex/RGB, presets, or an eyedropper that samples the screen.
- **Five models**, downloaded on first use and managed in the app (download, check for updates, delete).
- Dark and light themes, HEIC/AVIF/WebP/PNG/JPEG input, and optional deletion of originals once saved (Recycle Bin or permanent).

## Install

Download `Cutout-Setup-<version>.exe` from [Releases](https://github.com/rayokosaagh/cutout/releases) and run it. It installs for your user account without admin rights.

The installer isn't code-signed yet, so Windows SmartScreen may say "Windows protected your PC". Choose **More info**, then **Run anyway**.

The first cut downloads the chosen model (170 MB for the default) to `%USERPROFILE%\.u2net`.

## Run from source

Requires Windows and Python 3.14.

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python cutout_glass.py
```

`Cutout.exe --check` (or `python cutout_glass.py --check`) loads the engine, cuts a generated test image and reports whether everything works.

## Build the installer

Needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`).

```
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

This builds the app with PyInstaller, runs the self-check on the built app, and writes `build\installer\Cutout-Setup-<version>.exe`. The version lives in three places: `APP_VERSION` in `cutout_glass.py`, `packaging\version.txt` and `packaging\cutout.iss`.

To change the logo, replace `assets\logo-source.png` and run `python make_logo_assets.py`.

## Credits

Background removal by [rembg](https://github.com/danielgatis/rembg) with the U²-Net, IS-Net and BiRefNet models. See `packaging\THIRD-PARTY-NOTICES.txt` for all libraries and their licences. The Inter typeface is under the SIL Open Font License (`fonts\OFL.txt`).

## License

MIT
