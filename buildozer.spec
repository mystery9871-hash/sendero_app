[app]
title = Sendero
package.name = sendero
package.domain = org.inkandacceptance

source.dir = .
source.include_exts = py,json,png,jpg,kv,atlas
source.include_patterns = data/*.json

version = 1.0

requirements = python3,kivy==2.3.1,plyer,pyjnius

# Portrait phone app
orientation = portrait
fullscreen = 0

# Icon/presplash can be added later by dropping icon.png / presplash.png
# in this folder and uncommenting:
# icon.filename = %(source.dir)s/icon.png
# presplash.filename = %(source.dir)s/presplash.png

android.permissions = INTERNET

# Reasonable modern defaults; buildozer-action keeps SDK/NDK versions current.
android.api = 34
android.minapi = 23
android.archs = arm64-v8a, armeabi-v7a

android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1
