# Sendero — Android app

This is a from-scratch Android port of the Sendero desktop app, rebuilt with
[Kivy](https://kivy.org) (Python's cross-platform native-UI toolkit) instead
of tkinter. It ships with your Philosophy, Spanish, History, Puerto Rico
History, Python, and Nature lessons already bundled in `data/`.

It covers the core learning experience: dashboard with per-subject progress
and streak, subject lesson lists grouped by category, lesson detail view with
Mark Complete and Prev/Next, a combined whole-subject view, and Listen to
Lesson / per-word audio using **your phone's built-in text-to-speech engine**
(Piper doesn't run on Android, so this swaps in Android's native TTS instead
— still fully offline, no API key, no per-use cost).

Not yet ported: Import Content, Import PDF, Merge Subjects, and Translate.
Those all involve either network calls to external services or desktop file
dialogs that don't map cleanly to a phone. Worth doing as a second pass once
you've confirmed the core app feels right on your device — say the word.

## Getting a real .apk onto your phone

Building an Android app requires the Android SDK/NDK toolchain — multiple
gigabytes, and not something that can be compiled from this chat. Instead,
this repo includes a GitHub Actions workflow that does the build for you, for
free, on GitHub's own servers. You just need a (free) GitHub account.

### One-time setup

1. Go to [github.com/new](https://github.com/new) and create a new repository
   (public or private both work) — call it whatever you like, e.g. `sendero`.
2. On your computer, in this folder, run:
   ```bash
   git init
   git add .
   git commit -m "Sendero Android app"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```
3. On GitHub, open your repo's **Actions** tab. You should see a "Build
   Android APK" run start automatically (pushing to `main` triggers it). If
   it doesn't start on its own, click **Build Android APK** in the sidebar,
   then **Run workflow**.
4. The first build takes roughly **15–25 minutes** — it's downloading and
   setting up the entire Android SDK/NDK from scratch. Every build after that
   is faster since GitHub caches most of it.
5. When it finishes (green checkmark), open that workflow run and scroll to
   **Artifacts** at the bottom. Download `sendero-apk` — it's a zip
   containing your `.apk` file.

### Installing on your S22

1. Unzip the download and copy the `.apk` file onto your phone (email it to
   yourself, use a USB cable, Google Drive, whatever's easiest).
2. Tap the `.apk` file on your phone. Android will warn about installing from
   an unknown source the first time — go to **Settings → Apps → Special
   access → Install unknown apps**, and allow it for whichever app you used
   to open the file (Files, Chrome, Gmail, etc.).
3. Tap **Install**. It'll show up as **Sendero** in your app drawer like any
   other app — full screen, no browser, own icon.

This is a **debug-signed APK**, which is completely fine for installing on
your own phone. It just means it isn't signed for Play Store distribution —
irrelevant for personal use like this.

### Making changes later

Any time you (or I) edit `main.py`, the JSON lesson files, or anything else
in this repo, just commit and push again:
```bash
git add .
git commit -m "describe what changed"
git push
```
That kicks off a new build automatically. Download the new APK from
Artifacts the same way, and reinstall over the old one — your progress isn't
stored in the APK, so re-installing doesn't erase it.

## Where your progress is stored

Progress (completed lessons, streak) is saved to the app's own private
storage on your phone (Android's standard per-app data directory), separate
from the `data/*.json` lesson files. Uninstalling the app clears progress,
same as any Android app. On first launch it seeds from the `progress.json`
your desktop app had, if you kept it in `data/progress_seed.json` in this
repo (already included) — so your existing streak carries over on first run.

## Testing changes on your computer first (optional)

You don't need Android to try changes — this same code runs as a normal
desktop window too, since it's just Kivy:
```bash
pip install kivy plyer --break-system-packages
python3 main.py
```
(Text-to-speech will silently no-op on desktop since it calls Android's
native TTS API — that part only works once installed as an APK.)
