"""
Sendero — Daily Learning App (Android / Kivy port)

Reuses the exact same lesson JSON format as the original tkinter desktop app,
so the data/*.json files can be swapped in directly. Progress is stored in
this app's private Android storage (App.user_data_dir) rather than a folder
next to the script, since Android apps don't get to write next to their code.

TTS: calls Android's built-in TextToSpeech engine directly via pyjnius
(no Piper, no bundled voices, no internet, no plyer — plyer's own Android
TTS facade does exactly this internally, so we do it directly instead of
pulling in plyer's PyPI package, which drags in an unrelated `requests`
dependency chain that currently breaks python-for-android's build).
"""
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import datetime as dt

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.widget import Widget
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.progressbar import ProgressBar
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.core.window import Window
from kivy.properties import StringProperty, ObjectProperty
from kivy.utils import get_color_from_hex as hx

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def app_data_dir():
    """Directory this file lives in — where the bundled data/ folder ships."""
    return os.path.dirname(os.path.abspath(__file__))


def writable_data_dir():
    """Where subject files actually live once the app can edit them.

    The data/ folder shipped inside the app is read-only (it's part of the
    installed APK). Every launch, any bundled starter subject that's
    missing from the app's own private, writable storage gets copied back
    in — this only ever fills a gap, never overwrites or removes a file
    that's already there, so edits, added lessons, and deletions you made
    on purpose are always left alone. Every read and write goes through
    this writable copy, never the read-only original."""
    target = os.path.join(App.get_running_app().user_data_dir, "data")
    os.makedirs(target, exist_ok=True)
    bundled = os.path.join(app_data_dir(), "data")
    if os.path.isdir(bundled):
        for fname in os.listdir(bundled):
            if fname.endswith(".json") and fname != "progress_seed.json":
                dest_path = os.path.join(target, fname)
                if not os.path.exists(dest_path):
                    shutil.copy2(os.path.join(bundled, fname), dest_path)
    return target


def load_subjects():
    """Load every data/*.json file into a dict keyed by subject name."""
    subjects = {}
    data_dir = writable_data_dir()
    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith(".json") or fname == "progress_seed.json":
            continue
        path = os.path.join(data_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if "subject" not in payload or "lessons" not in payload:
            continue
        subjects[payload["subject"]] = {
            "icon": payload.get("icon", "📘"),
            "lessons": payload["lessons"],
            "file": fname,
        }
    return subjects


from shared_utils import (
    slugify, sanitize_imported_text, fetch_mymemory_translation, translate_text_to_english,
)


def save_subject_file(name, subj):
    """Write a subject's lesson list back to its JSON file."""
    path = os.path.join(writable_data_dir(), subj["file"])
    payload = {"subject": name, "icon": subj.get("icon", "📘"), "lessons": subj["lessons"]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def delete_subject_file(subj):
    path = os.path.join(writable_data_dir(), subj["file"])
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# run_in_thread stays here (not shared_utils) since it uses Kivy's Clock
# ---------------------------------------------------------------------------

def run_in_thread(work, on_done):
    """Run `work()` off the UI thread; deliver (result, error) back on the
    UI thread via `on_done`, since Kivy widgets can only be touched safely
    from the main thread."""
    def target():
        try:
            result = work()
            Clock.schedule_once(lambda _dt: on_done(result, None))
        except Exception as e:  # noqa: BLE001 — surfaced to the user, not swallowed
            Clock.schedule_once(lambda _dt: on_done(None, e))
    threading.Thread(target=target, daemon=True).start()


# ---------------------------------------------------------------------------
# Progress persistence (Android-safe storage location)
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, path):
        self.path = path
        self.data = {"completed": {}, "last_active": None, "streak": 0}
        self.load()
        self._touch_streak()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        else:
            # Seed from the desktop app's progress file on first run, if bundled.
            seed = os.path.join(app_data_dir(), "data", "progress_seed.json")
            if os.path.exists(seed):
                try:
                    with open(seed, "r", encoding="utf-8") as f:
                        self.data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass

    def _touch_streak(self):
        today = dt.date.today().isoformat()
        last = self.data.get("last_active")
        if last == today:
            pass
        elif last is None:
            self.data["streak"] = 1
        else:
            try:
                last_date = dt.date.fromisoformat(last)
                delta = (dt.date.today() - last_date).days
            except ValueError:
                delta = 99
            if delta == 1:
                self.data["streak"] = self.data.get("streak", 0) + 1
            elif delta > 1:
                self.data["streak"] = 1
        self.data["last_active"] = today
        self.save()

    def completed_ids(self, subject):
        return set(self.data.get("completed", {}).get(subject, []))

    def mark_complete(self, subject, lesson_id):
        completed = self.data.setdefault("completed", {})
        lst = completed.setdefault(subject, [])
        if lesson_id not in lst:
            lst.append(lesson_id)
            self.save()

    def mark_incomplete(self, subject, lesson_id):
        completed = self.data.get("completed", {}).get(subject)
        if completed and lesson_id in completed:
            completed.remove(lesson_id)
            self.save()

    def is_complete(self, subject, lesson_id):
        return lesson_id in self.completed_ids(subject)


# ---------------------------------------------------------------------------
# Text to speech (Android native engine via pyjnius — no plyer dependency)
# ---------------------------------------------------------------------------

_tts_engine = None
_tts_listener = None
_tts_ready = False


def _init_tts():
    """Lazily create a single TextToSpeech engine, cached for the app's life."""
    global _tts_engine, _tts_listener, _tts_ready
    if _tts_engine is not None:
        return _tts_engine

    from jnius import autoclass, PythonJavaClass, java_method

    class _InitListener(PythonJavaClass):
        __javainterfaces__ = ['android/speech/tts/TextToSpeech$OnInitListener']
        __javacontext__ = 'app'

        @java_method('(I)V')
        def onInit(self, status):
            global _tts_ready
            _tts_ready = (status == 0)  # TextToSpeech.SUCCESS == 0

    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    TextToSpeech = autoclass('android.speech.tts.TextToSpeech')
    activity = PythonActivity.mActivity

    _tts_listener = _InitListener()
    _tts_engine = TextToSpeech(activity, _tts_listener)
    return _tts_engine


def speak(text):
    try:
        from jnius import autoclass
        Bundle = autoclass("android.os.Bundle")
        engine = _init_tts()
        # QUEUE_FLUSH = 0: stop whatever's playing and speak this instead.
        # The 4-arg speak() overload needs a real Bundle and a real String —
        # passing None for either makes pyjnius unable to tell it apart from
        # the older 3-arg overload, which fails with a JavaException.
        engine.speak(text, 0, Bundle(), "sendero_tts")
    except Exception as e:
        import traceback
        traceback.print_exc()  # goes to logcat under the "python" tag
        _toast(f"Text-to-speech error: {e}")


def stop_speaking():
    try:
        if _tts_engine is not None:
            _tts_engine.stop()
    except Exception:
        pass


def _toast(msg):
    """Lightweight on-screen message — pure Kivy, no platform dependency."""
    try:
        from kivy.uix.label import Label as _Label
        from kivy.clock import Clock as _Clock
        popup = Popup(title="Sendero", content=_Label(text=msg),
                      size_hint=(0.8, 0.25), auto_dismiss=True)
        popup.open()
        _Clock.schedule_once(lambda dt_: popup.dismiss(), 3)
    except Exception:
        print("[Sendero]", msg)


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

BG = hx("#1e2124")
CARD = hx("#2a2e33")
ACCENT = hx("#6fbf73")
TEXT = hx("#eceff1")
MUTED = hx("#9aa0a6")

# One accent color per subject, assigned deterministically by name so it's
# stable across sessions and works for subjects added later too — no need
# to store a color choice anywhere.
SUBJECT_PALETTE = [
    hx("#e0954f"),  # warm orange
    hx("#5fa8d3"),  # sky blue
    hx("#c96a6a"),  # deep red/rose
    hx("#6fbf73"),  # forest green
    hx("#a67fd6"),  # purple
    hx("#d6b656"),  # gold
    hx("#4fb8a8"),  # teal
    hx("#d67fb0"),  # pink
]


def subject_color(name):
    return SUBJECT_PALETTE[sum(ord(c) for c in name) % len(SUBJECT_PALETTE)]


Window.clearcolor = BG


def attach_rounded_bg(widget, color, radius=dp(10)):
    """Give any widget a rounded-rect background in `color`, kept in sync
    with its position/size. Returns the widget for chaining."""
    from kivy.graphics import Color, RoundedRectangle
    with widget.canvas.before:
        Color(*color)
        widget._bg_rect = RoundedRectangle(pos=widget.pos, size=widget.size, radius=[radius])
    widget.bind(pos=lambda w, *_: setattr(w._bg_rect, "pos", w.pos),
                size=lambda w, *_: setattr(w._bg_rect, "size", w.size))
    return widget


def category_chip(text, color):
    """A small pill-style label used for category headers, tinted with the
    subject's accent color instead of plain gray caption text."""
    chip = BoxLayout(size_hint=(None, None), height=dp(26), padding=(dp(10), dp(4)))
    chip.width = dp(14) + len(text) * dp(7)
    tinted = (color[0], color[1], color[2], 0.18)
    attach_rounded_bg(chip, tinted, radius=dp(13))
    lbl = Label(text=text.upper(), color=color, font_size="11sp", bold=True)
    chip.add_widget(lbl)
    wrap = BoxLayout(size_hint_y=None, height=dp(34), padding=(0, dp(4)))
    wrap.add_widget(chip)
    wrap.add_widget(Widget())  # left-align the chip, don't stretch it
    return wrap


class ProgressRing(BoxLayout):
    """Small circular progress indicator with a centered percentage label,
    used on the dashboard cards in place of a plain linear bar."""
    def __init__(self, value, maximum, color, diameter=dp(46), **kw):
        super().__init__(size_hint=(None, None), size=(diameter, diameter), **kw)
        self._value = value
        self._maximum = max(maximum, 1)
        self._color = color
        pct = int(round(100 * value / self._maximum))
        self.label = Label(text=f"{pct}%", font_size="11sp", bold=True, color=TEXT)
        self.add_widget(self.label)
        self.bind(pos=self._redraw, size=self._redraw)
        self._redraw()

    def _redraw(self, *a):
        from kivy.graphics import Color, Line
        self.canvas.before.clear()
        r = min(self.width, self.height) / 2 - dp(3)
        cx, cy = self.center_x, self.center_y
        pct = self._value / self._maximum
        with self.canvas.before:
            Color(*CARD)
            Line(circle=(cx, cy, r), width=dp(3))
            if pct > 0:
                Color(*self._color)
                Line(circle=(cx, cy, r, 90 - 360 * pct, 90), width=dp(3), cap="round")


class TopBar(BoxLayout):
    def __init__(self, title, on_back=None, right_widget=None, **kw):
        super().__init__(orientation="horizontal", size_hint_y=None, height=dp(52),
                          padding=(dp(8), 0), spacing=dp(8), **kw)
        if on_back:
            back = Button(text="< Back", size_hint=(None, None), size=(dp(72), dp(44)),
                           background_color=CARD, color=TEXT)
            back.bind(on_release=on_back)
            self.add_widget(back)
        lbl = Label(text=title, bold=True, font_size="18sp", color=TEXT, halign="left",
                     valign="middle")
        lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        self.add_widget(lbl)
        if right_widget:
            self.add_widget(right_widget)


def section_label(text, **kw):
    lbl = Label(text=text, color=MUTED, font_size="13sp", size_hint_y=None,
                height=dp(28), halign="left", valign="middle", bold=True, **kw)
    lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
    return lbl


def wrapped_label(text, **kw):
    kw.setdefault("color", TEXT)
    kw.setdefault("halign", "left")
    kw.setdefault("valign", "top")
    lbl = Label(text=text, size_hint_y=None, **kw)
    lbl.bind(width=lambda w, *_: setattr(w, "text_size", (w.width, None)))
    lbl.bind(texture_size=lambda w, *_: setattr(w, "height", w.texture_size[1]))
    return lbl


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardScreen(Screen):
    def on_pre_enter(self, *a):
        self.build()

    def build(self):
        app = App.get_running_app()
        self.clear_widgets()
        root = BoxLayout(orientation="vertical")
        new_subj_btn = Button(text="+ New Subject", size_hint=(None, None), size=(dp(128), dp(44)),
                               background_color=ACCENT, color=hx("#0b1f0c"), font_size="12sp")
        new_subj_btn.bind(on_release=lambda b: ImportContentPopup(
            on_done=lambda name: self.build()).open())
        root.add_widget(TopBar("Sendero", right_widget=new_subj_btn))

        scroll = ScrollView()
        col = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(12), spacing=dp(12))
        col.bind(minimum_height=col.setter("height"))

        streak = app.progress.data.get("streak", 0)
        streak_row = BoxLayout(size_hint_y=None, height=dp(60), padding=dp(8))
        streak_row.add_widget(wrapped_label(
            f"{streak} day streak", font_size="20sp", bold=True))
        col.add_widget(streak_row)

        col.add_widget(section_label("YOUR SUBJECTS"))

        for name, subj in app.subjects.items():
            total = len(subj["lessons"])
            done = len(app.progress.completed_ids(name))
            color = subject_color(name)
            card = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(92),
                              padding=dp(10), spacing=dp(4))
            attach_rounded_bg(card, CARD)
            # thin accent stripe down the left edge, tinted to the subject
            from kivy.graphics import Color as _Color, Rectangle as _Rectangle
            with card.canvas.before:
                _Color(*color)
                card._stripe = _Rectangle(pos=card.pos, size=(dp(4), card.height))
            card.bind(pos=lambda w, *_: setattr(w._stripe, "pos", w.pos),
                      size=lambda w, *_: setattr(w._stripe, "size", (dp(4), w.height)))

            body = BoxLayout(spacing=dp(10))
            ring = ProgressRing(done, total, color)
            body.add_widget(ring)

            info = BoxLayout(orientation="vertical", spacing=dp(4))
            head = BoxLayout(size_hint_y=None, height=dp(24))
            head.add_widget(wrapped_label(name, bold=True, font_size="16sp"))
            head.add_widget(wrapped_label(f"{done}/{total}", color=MUTED, halign="right",
                                           size_hint_x=None, width=dp(60)))
            info.add_widget(head)

            btn = Button(text="Continue", size_hint_y=None, height=dp(32),
                         background_color=color, color=hx("#101010"), font_size="12sp")
            btn.bind(on_release=lambda b, n=name: app.open_subject(n))
            info.add_widget(btn)

            body.add_widget(info)
            card.add_widget(body)

            # Make whole card tappable too
            card.bind(on_touch_down=lambda w, touch, n=name: (
                app.open_subject(n) if w.collide_point(*touch.pos) else None))

            col.add_widget(card)

        if not app.subjects:
            col.add_widget(wrapped_label(
                "No subjects yet. Add lessons to get started.",
                color=MUTED))

        scroll.add_widget(col)
        root.add_widget(scroll)
        self.add_widget(root)


# ---------------------------------------------------------------------------
# Subject (lesson list) screen
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Editing: add/delete lesson, delete/merge subject, translate
# ---------------------------------------------------------------------------

def form_label(text):
    return wrapped_label(text, color=MUTED, font_size="12sp", height=dp(20))


def form_input(multiline=False, height=dp(40), hint_text=""):
    ti = TextInput(multiline=multiline, size_hint_y=None, height=height,
                    background_color=CARD, foreground_color=TEXT,
                    cursor_color=ACCENT, hint_text=hint_text, hint_text_color=MUTED,
                    padding=(dp(8), dp(8)))
    return ti


class ConfirmPopup(Popup):
    def __init__(self, message, on_confirm, title="Confirm", **kw):
        col = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(12))
        col.add_widget(wrapped_label(message, height=dp(70)))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        yes_btn = Button(text="Delete", background_color=hx("#b23b3b"), color=TEXT)
        no_btn = Button(text="Cancel", background_color=CARD, color=TEXT)
        row.add_widget(yes_btn)
        row.add_widget(no_btn)
        col.add_widget(row)
        super().__init__(title=title, content=col, size_hint=(0.85, 0.4), **kw)
        yes_btn.bind(on_release=lambda b: (self.dismiss(), on_confirm()))
        no_btn.bind(on_release=lambda b: self.dismiss())


class AddLessonPopup(Popup):
    def __init__(self, subject_name, on_saved, **kw):
        self.subject_name = subject_name
        self.on_saved = on_saved
        app = App.get_running_app()

        outer = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(8))
        scroll = ScrollView()
        col = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        col.bind(minimum_height=col.setter("height"))

        col.add_widget(form_label("Category (optional — groups lessons together)"))
        self.category_in = form_input(hint_text="e.g. Grammar Foundations")
        col.add_widget(self.category_in)

        col.add_widget(form_label("Title"))
        self.title_in = form_input(hint_text="Lesson title")
        col.add_widget(self.title_in)

        col.add_widget(form_label("Summary (one line)"))
        self.summary_in = form_input(hint_text="Short one-line summary")
        col.add_widget(self.summary_in)

        col.add_widget(form_label("Lesson content"))
        self.body_in = form_input(multiline=True, height=dp(160), hint_text="Full lesson text")
        col.add_widget(self.body_in)

        col.add_widget(form_label("Vocabulary — optional, one per line as: term - translation"))
        self.vocab_in = form_input(multiline=True, height=dp(90),
                                    hint_text="Buenos días - Good morning")
        col.add_widget(self.vocab_in)

        self.error_lbl = wrapped_label("", color=hx("#e07a7a"), height=dp(20))
        col.add_widget(self.error_lbl)

        scroll.add_widget(col)
        outer.add_widget(scroll)

        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        save_btn = Button(text="Save Lesson", background_color=ACCENT, color=hx("#0b1f0c"))
        cancel_btn = Button(text="Cancel", background_color=CARD, color=TEXT)
        btn_row.add_widget(save_btn)
        btn_row.add_widget(cancel_btn)
        outer.add_widget(btn_row)

        super().__init__(title=f"Add Lesson to {subject_name}", content=outer,
                          size_hint=(0.92, 0.9), **kw)
        save_btn.bind(on_release=lambda b: self._save())
        cancel_btn.bind(on_release=lambda b: self.dismiss())

    def _save(self):
        app = App.get_running_app()
        title = self.title_in.text.strip()
        body = self.body_in.text.strip()
        if not title or not body:
            self.error_lbl.text = "Title and lesson content are required."
            return
        subj = app.subjects[self.subject_name]
        new_id = max((l["id"] for l in subj["lessons"]), default=0) + 1
        lesson = {
            "id": new_id,
            "title": sanitize_imported_text(title),
            "summary": sanitize_imported_text(self.summary_in.text.strip()),
            "body": sanitize_imported_text(body),
        }
        category = self.category_in.text.strip()
        if category:
            lesson["category"] = category
        vocab = []
        for line in self.vocab_in.text.splitlines():
            if " - " in line:
                term, _, translation = line.partition(" - ")
                term, translation = term.strip(), translation.strip()
                if term and translation:
                    vocab.append({"term": term, "translation": translation})
        if vocab:
            lesson["vocab"] = vocab
        subj["lessons"].append(lesson)
        save_subject_file(self.subject_name, subj)
        self.dismiss()
        self.on_saved()


class MergeSubjectsPopup(Popup):
    def __init__(self, source_name, on_merged, **kw):
        app = App.get_running_app()
        others = [n for n in app.subjects if n != source_name]

        col = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(10))
        col.add_widget(wrapped_label(
            f"Move every lesson from '{source_name}' into another subject, then delete "
            f"'{source_name}'. This can't be undone.", height=dp(70)))

        if not others:
            col.add_widget(wrapped_label("No other subjects to merge into."))
            close_btn = Button(text="Close", size_hint_y=None, height=dp(48),
                                background_color=CARD, color=TEXT)
            col.add_widget(close_btn)
            super().__init__(title="Merge Subject", content=col, size_hint=(0.85, 0.5), **kw)
            close_btn.bind(on_release=lambda b: self.dismiss())
            return

        col.add_widget(form_label("Merge into"))
        spinner = Spinner(text=others[0], values=others, size_hint_y=None, height=dp(44),
                           background_color=CARD, color=TEXT)
        col.add_widget(spinner)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        merge_btn = Button(text="Merge", background_color=ACCENT, color=hx("#0b1f0c"))
        cancel_btn = Button(text="Cancel", background_color=CARD, color=TEXT)
        row.add_widget(merge_btn)
        row.add_widget(cancel_btn)
        col.add_widget(row)

        super().__init__(title="Merge Subject", content=col, size_hint=(0.88, 0.55), **kw)
        merge_btn.bind(on_release=lambda b: self._merge(source_name, spinner.text, on_merged))
        cancel_btn.bind(on_release=lambda b: self.dismiss())

    def _merge(self, source_name, dest_name, on_merged):
        app = App.get_running_app()
        source = app.subjects[source_name]
        dest = app.subjects[dest_name]
        next_id = max((l["id"] for l in dest["lessons"]), default=0) + 1

        id_remap = {}
        for lesson in source["lessons"]:
            old_id = lesson["id"]
            lesson = dict(lesson)
            id_remap[old_id] = next_id
            lesson["id"] = next_id
            lesson.setdefault("category", source_name)
            dest["lessons"].append(lesson)
            next_id += 1
        save_subject_file(dest_name, dest)

        source_completed = app.progress.data.get("completed", {}).pop(source_name, [])
        if source_completed:
            remapped = {id_remap[i] for i in source_completed if i in id_remap}
            existing = set(app.progress.data.get("completed", {}).get(dest_name, []))
            app.progress.data.setdefault("completed", {})[dest_name] = sorted(existing | remapped)
        app.progress.save()

        delete_subject_file(source)
        del app.subjects[source_name]

        self.dismiss()
        on_merged()


class TranslatePopup(Popup):
    def __init__(self, subject_name, lesson_id, on_translated, **kw):
        self.subject_name = subject_name
        self.lesson_id = lesson_id
        self.on_translated = on_translated

        col = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(10))
        col.add_widget(wrapped_label(
            "Machine-translates this lesson's title, summary, and body to English via "
            "MyMemory's free translation API. Expect some rough wording.",
            height=dp(70)))
        col.add_widget(form_label("Source language code (e.g. fr, de, es, it, la)"))
        self.lang_in = form_input(hint_text="es")
        col.add_widget(self.lang_in)

        self.status_lbl = wrapped_label("", color=MUTED, height=dp(40))
        col.add_widget(self.status_lbl)

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.translate_btn = Button(text="Translate", background_color=ACCENT, color=hx("#0b1f0c"))
        cancel_btn = Button(text="Cancel", background_color=CARD, color=TEXT)
        row.add_widget(self.translate_btn)
        row.add_widget(cancel_btn)
        col.add_widget(row)

        super().__init__(title="Translate to English", content=col, size_hint=(0.88, 0.55), **kw)
        self.translate_btn.bind(on_release=lambda b: self._start())
        cancel_btn.bind(on_release=lambda b: self.dismiss())

    def _start(self):
        lang = self.lang_in.text.strip().lower()
        if not lang:
            self.status_lbl.text = "Enter a source language code first."
            return
        self.translate_btn.disabled = True
        self.status_lbl.text = "Translating — this can take a little while..."

        app = App.get_running_app()
        subj = app.subjects[self.subject_name]
        lesson = next(l for l in subj["lessons"] if l["id"] == self.lesson_id)

        def work():
            title, summary, body = lesson["title"], lesson.get("summary", ""), lesson.get("body", "")
            try:
                title = fetch_mymemory_translation(title, lang)
            except ValueError:
                pass  # keep original title if just the title fails
            if summary:
                summary = fetch_mymemory_translation(summary, lang)
            body = translate_text_to_english(body, lang)
            return title, summary, body

        def done(result, error):
            if error is not None:
                self.status_lbl.text = str(error)
                self.translate_btn.disabled = False
                return
            title, summary, body = result
            lesson["title"], lesson["summary"], lesson["body"] = title, summary, body
            save_subject_file(self.subject_name, subj)
            self.dismiss()
            self.on_translated()

        run_in_thread(work, done)


class ImportContentPopup(Popup):
    """Search Wikipedia, Project Gutenberg / Open Library, the Internet
    Archive, Library of Congress newspapers, and Google News for a topic,
    and add whatever's found as new lessons — either into an existing
    subject or a brand new one. Network calls run on a background thread
    so the UI never freezes; progress is reported topic-by-topic."""

    def __init__(self, on_done, dest_subject=None, **kw):
        self.on_done = on_done
        app = App.get_running_app()
        existing_names = list(app.subjects.keys())

        outer = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(8))
        scroll = ScrollView()
        col = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        col.bind(minimum_height=col.setter("height"))

        col.add_widget(form_label("Add lessons to"))
        dest_values = ["+ New Subject"] + existing_names
        default_dest = dest_subject if dest_subject in existing_names else "+ New Subject"
        self.dest_spinner = Spinner(text=default_dest, values=dest_values, size_hint_y=None,
                                     height=dp(44), background_color=CARD, color=TEXT)
        col.add_widget(self.dest_spinner)

        self.new_subject_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                          height=dp(70), spacing=dp(4))
        self.new_subject_box.add_widget(form_label("New subject name"))
        self.name_in = form_input(hint_text="e.g. Astronomy")
        self.new_subject_box.add_widget(self.name_in)
        col.add_widget(self.new_subject_box)
        self.dest_spinner.bind(text=self._on_dest_change)
        self._on_dest_change(self.dest_spinner, self.dest_spinner.text)

        col.add_widget(form_label("Topics — one per line"))
        self.topics_in = form_input(multiline=True, height=dp(110),
                                     hint_text="Stoicism\nThe French Revolution\nPhotosynthesis")
        col.add_widget(self.topics_in)

        translate_row = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
        self.translate_check = Button(text="Translate non-English books ✓" ,
                                       background_color=CARD, color=MUTED, font_size="12sp")
        self._translate_on = False
        self.translate_check.bind(on_release=self._toggle_translate)
        translate_row.add_widget(self.translate_check)
        col.add_widget(translate_row)
        self._set_translate_label()

        self.status_lbl = wrapped_label(
            "Pulls from Wikipedia, Project Gutenberg / Open Library, the Internet Archive, "
            "Library of Congress newspapers, and Google News — whatever's found for each "
            "topic gets added.", color=MUTED, height=dp(50), font_size="12sp")
        col.add_widget(self.status_lbl)

        scroll.add_widget(col)
        outer.add_widget(scroll)

        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.fetch_btn = Button(text="Fetch Lessons", background_color=ACCENT, color=hx("#0b1f0c"))
        close_btn = Button(text="Close", background_color=CARD, color=TEXT)
        self.fetch_btn.bind(on_release=lambda b: self._start_fetch())
        close_btn.bind(on_release=lambda b: self.dismiss())
        btn_row.add_widget(self.fetch_btn)
        btn_row.add_widget(close_btn)
        outer.add_widget(btn_row)

        super().__init__(title="Import Content", content=outer, size_hint=(0.94, 0.9), **kw)

    def _on_dest_change(self, spinner, text):
        self.new_subject_box.opacity = 1 if text == "+ New Subject" else 0
        self.new_subject_box.disabled = text != "+ New Subject"
        self.new_subject_box.height = dp(70) if text == "+ New Subject" else 0

    def _toggle_translate(self, b):
        self._translate_on = not self._translate_on
        self._set_translate_label()

    def _set_translate_label(self):
        mark = "✓" if self._translate_on else " "
        self.translate_check.text = f"[{mark}] Translate non-English books to English"
        self.translate_check.color = TEXT if self._translate_on else MUTED

    def _start_fetch(self):
        import content_sources as cs
        app = App.get_running_app()

        topics = [t.strip() for t in self.topics_in.text.splitlines() if t.strip()]
        if not topics:
            self.status_lbl.text = "Add at least one topic first."
            return

        dest = self.dest_spinner.text
        creating_new = dest == "+ New Subject"
        if creating_new:
            name = self.name_in.text.strip()
            if not name:
                self.status_lbl.text = "Give the new subject a name."
                return
            if name in app.subjects:
                creating_new = False
                target_name = name
            else:
                target_name = name
                icon = "📘"
        else:
            target_name = dest

        auto_translate = self._translate_on
        self.fetch_btn.disabled = True

        def work():
            results = []
            if creating_new:
                subj = {"icon": icon, "lessons": [], "file": slugify(target_name) + ".json"}
            else:
                subj = app.subjects[target_name]

            next_id = max((l["id"] for l in subj["lessons"]), default=0) + 1
            errors_all = []
            new_lessons = []
            for i, topic in enumerate(topics, start=1):
                Clock.schedule_once(lambda _dt, i=i, topic=topic: setattr(
                    self.status_lbl, "text", f"Fetching {i}/{len(topics)}: {topic} ..."))
                try:
                    fetched, source_failures = cs.build_lessons_from_all_sources(
                        topic, start_id=next_id, auto_translate=auto_translate)
                    for l in fetched:
                        l["category"] = topic
                    new_lessons.extend(fetched)
                    next_id += len(fetched)
                except ValueError as e:
                    errors_all.append(str(e))
            return subj, new_lessons, errors_all, creating_new, target_name

        def done(result, error):
            self.fetch_btn.disabled = False
            if error is not None:
                self.status_lbl.text = f"Something went wrong: {error}"
                return
            subj, new_lessons, errors_all, is_new, name = result
            if not new_lessons:
                self.status_lbl.text = "Couldn't fetch anything:\n" + "\n".join(errors_all)
                return

            kept, skipped_dupes = cs.dedupe_new_lessons(subj["lessons"], new_lessons)
            if not kept:
                self.status_lbl.text = (
                    f"Everything found was already in '{name}' "
                    f"({skipped_dupes} duplicate lesson(s) skipped).")
                return

            subj["lessons"].extend(kept)
            save_subject_file(name, subj)
            if is_new:
                app.subjects[name] = subj

            summary = f"Added {len(kept)} lesson(s) to '{name}'."
            if skipped_dupes:
                summary += f" Skipped {skipped_dupes} duplicate(s)."
            if errors_all:
                summary += " Some topics found nothing: " + "; ".join(errors_all)
            self.status_lbl.text = summary
            self.on_done(name)

        run_in_thread(work, done)


class SubjectScreen(Screen):
    subject_name = StringProperty("")

    def on_pre_enter(self, *a):
        self.build()

    def build(self):
        app = App.get_running_app()
        self.clear_widgets()
        subj = app.subjects.get(self.subject_name)
        root = BoxLayout(orientation="vertical")

        combined_btn = Button(text="Combined", size_hint=(None, None), size=(dp(96), dp(44)),
                               background_color=CARD, color=TEXT)
        combined_btn.bind(on_release=lambda b: app.open_combined(self.subject_name))
        root.add_widget(TopBar(self.subject_name,
                                on_back=lambda b: app.go_dashboard(),
                                right_widget=combined_btn))

        scroll = ScrollView()
        col = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(12), spacing=dp(6))
        col.bind(minimum_height=col.setter("height"))

        done_ids = app.progress.completed_ids(self.subject_name)
        lessons = subj["lessons"]
        color = subject_color(self.subject_name)

        if not lessons:
            col.add_widget(Widget(size_hint_y=None, height=dp(40)))
            col.add_widget(wrapped_label(
                "No lessons yet in this subject.", font_size="16sp", color=MUTED))
            col.add_widget(wrapped_label(
                "Tap \"+ Add Lesson\" below to write your first one.", color=MUTED,
                font_size="13sp"))

        # group by category, preserving first-seen order; uncategorized first (no header)
        categories = []
        by_cat = {}
        for lesson in lessons:
            cat = lesson.get("category")
            by_cat.setdefault(cat, []).append(lesson)
            if cat not in categories:
                categories.append(cat)

        for cat in categories:
            if cat is not None:
                col.add_widget(category_chip(cat, color))
            for lesson in by_cat[cat]:
                mark = "✓" if lesson["id"] in done_ids else " "
                row = BoxLayout(size_hint_y=None, height=dp(56))
                attach_rounded_bg(row, CARD, radius=dp(8))
                from kivy.graphics import Color as _Color2, Rectangle as _Rectangle2
                with row.canvas.before:
                    _Color2(*color)
                    row._stripe = _Rectangle2(pos=row.pos, size=(dp(4), row.height))
                row.bind(pos=lambda w, *_: setattr(w._stripe, "pos", w.pos),
                          size=lambda w, *_: setattr(w._stripe, "size", (dp(4), w.height)))

                check = Label(text=mark, color=color, bold=True, font_size="16sp",
                              size_hint_x=None, width=dp(34))
                title_lbl = wrapped_label(lesson["title"], font_size="14sp", height=dp(56),
                                           valign="middle")
                title_lbl.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, w.height)))
                row.add_widget(check)
                row.add_widget(title_lbl)
                row.bind(on_touch_down=lambda w, touch, lid=lesson["id"]: (
                    app.open_lesson(self.subject_name, lid) if w.collide_point(*touch.pos)
                    else None))
                col.add_widget(row)

        scroll.add_widget(col)
        root.add_widget(scroll)

        actions = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(100),
                             spacing=dp(6), padding=(dp(12), dp(6)))
        row1 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row2 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        add_btn = Button(text="+ Add Lesson", background_color=ACCENT, color=hx("#0b1f0c"))
        import_btn = Button(text="Import Content", background_color=color, color=hx("#101010"))
        merge_btn = Button(text="Merge", background_color=CARD, color=TEXT)
        delete_btn = Button(text="Delete Subject", background_color=CARD, color=hx("#e07a7a"))
        add_btn.bind(on_release=lambda b: AddLessonPopup(
            self.subject_name, on_saved=self.build).open())
        import_btn.bind(on_release=lambda b: ImportContentPopup(
            on_done=lambda name: self.build(), dest_subject=self.subject_name).open())
        merge_btn.bind(on_release=lambda b: MergeSubjectsPopup(
            self.subject_name, on_merged=lambda: app.go_dashboard()).open())
        delete_btn.bind(on_release=lambda b: ConfirmPopup(
            f"Delete the entire '{self.subject_name}' subject and all "
            f"{len(subj['lessons'])} of its lessons? This can't be undone.",
            on_confirm=self._delete_subject, title="Delete Subject").open())
        row1.add_widget(add_btn)
        row1.add_widget(import_btn)
        row2.add_widget(merge_btn)
        row2.add_widget(delete_btn)
        actions.add_widget(row1)
        actions.add_widget(row2)
        root.add_widget(actions)

        self.add_widget(root)

    def _delete_subject(self):
        app = App.get_running_app()
        subj = app.subjects.get(self.subject_name)
        if not subj:
            return
        delete_subject_file(subj)
        app.progress.data.get("completed", {}).pop(self.subject_name, None)
        app.progress.save()
        del app.subjects[self.subject_name]
        app.go_dashboard()


# ---------------------------------------------------------------------------
# Lesson detail screen
# ---------------------------------------------------------------------------

class LessonScreen(Screen):
    subject_name = StringProperty("")
    lesson_id = ObjectProperty(None)

    def on_pre_enter(self, *a):
        self.build()

    def _lesson_and_index(self, app):
        subj = app.subjects[self.subject_name]
        lessons = subj["lessons"]
        idx = next((i for i, l in enumerate(lessons) if l["id"] == self.lesson_id), 0)
        return lessons, idx

    def build(self):
        app = App.get_running_app()
        self.clear_widgets()
        lessons, idx = self._lesson_and_index(app)
        lesson = lessons[idx]

        root = BoxLayout(orientation="vertical")
        root.add_widget(TopBar("Lesson", on_back=lambda b: app.open_subject(self.subject_name)))

        scroll = ScrollView()
        col = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(14), spacing=dp(10))
        col.bind(minimum_height=col.setter("height"))

        col.add_widget(wrapped_label(lesson["title"], bold=True, font_size="20sp"))
        col.add_widget(wrapped_label(lesson.get("summary", ""), color=MUTED, italic=True,
                                      font_size="14sp"))

        listen_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        listen_btn = Button(text="Listen to Lesson", background_color=ACCENT,
                             color=hx("#0b1f0c"))
        stop_btn = Button(text="Stop", size_hint_x=None, width=dp(80),
                           background_color=CARD, color=TEXT)
        full_text = lesson["title"] + ". " + lesson.get("summary", "") + " " + lesson.get("body", "")
        listen_btn.bind(on_release=lambda b: speak(full_text))
        stop_btn.bind(on_release=lambda b: stop_speaking())
        listen_row.add_widget(listen_btn)
        listen_row.add_widget(stop_btn)
        col.add_widget(listen_row)

        col.add_widget(wrapped_label(lesson.get("body", ""), font_size="15sp"))

        vocab = lesson.get("vocab") or []
        if vocab:
            col.add_widget(section_label("VOCABULARY"))
            grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(4))
            grid.bind(minimum_height=grid.setter("height"))
            for v in vocab:
                vrow = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
                vrow.add_widget(wrapped_label(f"{v.get('term','')} - {v.get('translation','')}",
                                               font_size="14sp"))
                spk = Button(text="Play", size_hint=(None, None), size=(dp(56), dp(40)),
                             background_color=CARD, color=TEXT)
                spk.bind(on_release=lambda b, t=v.get("term", ""): speak(t))
                vrow.add_widget(spk)
                grid.add_widget(vrow)
            col.add_widget(grid)

        # attribution, if this lesson was imported
        if lesson.get("source"):
            col.add_widget(wrapped_label(f"Source: {lesson['source']}", color=MUTED,
                                          font_size="11sp"))

        # Mark complete
        is_done = app.progress.is_complete(self.subject_name, lesson["id"])
        complete_btn = Button(
            text="Marked Complete (tap to undo)" if is_done else "Mark Complete",
            size_hint_y=None, height=dp(48),
            background_color=(CARD if is_done else ACCENT),
            color=(TEXT if is_done else hx("#0b1f0c")))

        def toggle_complete(b):
            if app.progress.is_complete(self.subject_name, lesson["id"]):
                app.progress.mark_incomplete(self.subject_name, lesson["id"])
            else:
                app.progress.mark_complete(self.subject_name, lesson["id"])
            self.build()

        complete_btn.bind(on_release=toggle_complete)
        col.add_widget(complete_btn)

        # Translate / Delete
        edit_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        translate_btn = Button(text="Translate to English", background_color=CARD, color=TEXT)
        delete_lesson_btn = Button(text="Delete Lesson", background_color=CARD, color=hx("#e07a7a"))
        translate_btn.bind(on_release=lambda b: TranslatePopup(
            self.subject_name, lesson["id"], on_translated=self.build).open())
        delete_lesson_btn.bind(on_release=lambda b: ConfirmPopup(
            f"Delete the lesson '{lesson['title']}'? This can't be undone.",
            on_confirm=self._delete_lesson, title="Delete Lesson").open())
        edit_row.add_widget(translate_btn)
        edit_row.add_widget(delete_lesson_btn)
        col.add_widget(edit_row)

        # Prev / Next
        nav = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        prev_btn = Button(text="< Previous", background_color=CARD, color=TEXT,
                           disabled=(idx == 0))
        next_btn = Button(text="Next >", background_color=CARD, color=TEXT,
                           disabled=(idx == len(lessons) - 1))
        prev_btn.bind(on_release=lambda b: app.open_lesson(
            self.subject_name, lessons[idx - 1]["id"]) if idx > 0 else None)
        next_btn.bind(on_release=lambda b: app.open_lesson(
            self.subject_name, lessons[idx + 1]["id"]) if idx < len(lessons) - 1 else None)
        nav.add_widget(prev_btn)
        nav.add_widget(next_btn)
        col.add_widget(nav)

        scroll.add_widget(col)
        root.add_widget(scroll)
        self.add_widget(root)

    def _delete_lesson(self):
        app = App.get_running_app()
        subj = app.subjects[self.subject_name]
        subj["lessons"] = [l for l in subj["lessons"] if l["id"] != self.lesson_id]
        save_subject_file(self.subject_name, subj)
        app.progress.mark_incomplete(self.subject_name, self.lesson_id)
        app.open_subject(self.subject_name)


# ---------------------------------------------------------------------------
# Combined ("whole subject as one document") screen
# ---------------------------------------------------------------------------

class CombinedScreen(Screen):
    subject_name = StringProperty("")

    def on_pre_enter(self, *a):
        self.build()

    def build(self):
        app = App.get_running_app()
        self.clear_widgets()
        subj = app.subjects.get(self.subject_name, {"lessons": [], "icon": ""})
        root = BoxLayout(orientation="vertical")
        root.add_widget(TopBar(f"{self.subject_name} - Combined",
                                on_back=lambda b: app.open_subject(self.subject_name)))

        scroll = ScrollView()
        col = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(14), spacing=dp(16))
        col.bind(minimum_height=col.setter("height"))

        for lesson in subj["lessons"]:
            col.add_widget(wrapped_label(lesson["title"], bold=True, font_size="18sp"))
            col.add_widget(wrapped_label(lesson.get("summary", ""), color=MUTED, italic=True))
            col.add_widget(wrapped_label(lesson.get("body", ""), font_size="14sp"))

        scroll.add_widget(col)
        root.add_widget(scroll)
        self.add_widget(root)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SenderoApp(App):
    def build(self):
        self.title = "Sendero"
        self.subjects = load_subjects()
        progress_path = os.path.join(self.user_data_dir, "progress.json")
        self.progress = Progress(progress_path)

        self.sm = ScreenManager(transition=SlideTransition())
        self.sm.add_widget(DashboardScreen(name="dashboard"))
        self.sm.add_widget(SubjectScreen(name="subject"))
        self.sm.add_widget(LessonScreen(name="lesson"))
        self.sm.add_widget(CombinedScreen(name="combined"))
        return self.sm

    def go_dashboard(self):
        self.sm.transition.direction = "right"
        self.sm.current = "dashboard"
        self.sm.get_screen("dashboard").build()

    def open_subject(self, name):
        screen = self.sm.get_screen("subject")
        screen.subject_name = name
        self.sm.transition.direction = "left"
        self.sm.current = "subject"
        screen.build()

    def open_lesson(self, subject_name, lesson_id):
        screen = self.sm.get_screen("lesson")
        screen.subject_name = subject_name
        screen.lesson_id = lesson_id
        self.sm.transition.direction = "left"
        self.sm.current = "lesson"
        screen.build()

    def open_combined(self, subject_name):
        screen = self.sm.get_screen("combined")
        screen.subject_name = subject_name
        self.sm.transition.direction = "left"
        self.sm.current = "combined"
        screen.build()


if __name__ == "__main__":
    SenderoApp().run()
