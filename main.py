"""
Sendero — Daily Learning App (Android / Kivy port)

Reuses the exact same lesson JSON format as the original tkinter desktop app,
so the data/*.json files can be swapped in directly. Progress is stored in
this app's private Android storage (App.user_data_dir) rather than a folder
next to the script, since Android apps don't get to write next to their code.

TTS: uses plyer.tts, which calls Android's built-in TextToSpeech engine on
device. No Piper, no bundled voices, no internet required.
"""
import json
import os
import datetime as dt

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.progressbar import ProgressBar
from kivy.uix.popup import Popup
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


def load_subjects():
    """Load every data/*.json file into a dict keyed by subject name."""
    subjects = {}
    data_dir = os.path.join(app_data_dir(), "data")
    if not os.path.isdir(data_dir):
        return subjects
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
# Text to speech (Android native engine via plyer)
# ---------------------------------------------------------------------------

def speak(text):
    try:
        from plyer import tts
        tts.speak(message=text)
    except NotImplementedError:
        _toast("Text-to-speech isn't available on this device.")
    except Exception:
        _toast("Couldn't start text-to-speech.")


def _toast(msg):
    try:
        from plyer import notification
        notification.notify(title="Sendero", message=msg, timeout=3)
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

Window.clearcolor = BG


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
        root.add_widget(TopBar("Sendero"))

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
            card = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(84),
                              padding=dp(10), spacing=dp(4))
            with card.canvas.before:
                from kivy.graphics import Color, RoundedRectangle
                Color(*CARD)
                card._bg = RoundedRectangle(pos=card.pos, size=card.size, radius=[dp(10)])
            card.bind(pos=lambda w, *_: setattr(w._bg, "pos", w.pos),
                      size=lambda w, *_: setattr(w._bg, "size", w.size))

            head = BoxLayout(size_hint_y=None, height=dp(28))
            head.add_widget(wrapped_label(name, bold=True, font_size="16sp"))
            head.add_widget(wrapped_label(f"{done}/{total}", color=MUTED, halign="right",
                                           size_hint_x=None, width=dp(60)))
            card.add_widget(head)

            pb = ProgressBar(max=max(total, 1), value=done, size_hint_y=None, height=dp(10))
            card.add_widget(pb)

            btn = Button(text="Continue", size_hint_y=None, height=dp(28),
                         background_color=ACCENT, color=hx("#0b1f0c"), font_size="12sp")
            btn.bind(on_release=lambda b, n=name: app.open_subject(n))
            card.add_widget(btn)

            # Make whole card tappable too
            card.bind(on_touch_down=lambda w, touch, n=name: (
                app.open_subject(n) if w.collide_point(*touch.pos) else None))

            col.add_widget(card)

        if not app.subjects:
            col.add_widget(wrapped_label(
                "No subjects found. Make sure the data/ folder shipped with this app.",
                color=MUTED))

        scroll.add_widget(col)
        root.add_widget(scroll)
        self.add_widget(root)


# ---------------------------------------------------------------------------
# Subject (lesson list) screen
# ---------------------------------------------------------------------------

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
                col.add_widget(section_label(cat.upper()))
            for lesson in by_cat[cat]:
                mark = "[x]" if lesson["id"] in done_ids else "[ ]"
                row = Button(text=f"{mark}  {lesson['title']}", size_hint_y=None, height=dp(48),
                             background_color=CARD, color=TEXT, halign="left", valign="middle",
                             font_size="14sp")
                row.bind(size=lambda w, *_: setattr(w, "text_size", (w.width - dp(20), None)))
                row.bind(on_release=lambda b, lid=lesson["id"]: app.open_lesson(
                    self.subject_name, lid))
                col.add_widget(row)

        scroll.add_widget(col)
        root.add_widget(scroll)
        self.add_widget(root)


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
        full_text = lesson["title"] + ". " + lesson.get("summary", "") + " " + lesson.get("body", "")
        listen_btn.bind(on_release=lambda b: speak(full_text))
        listen_row.add_widget(listen_btn)
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

    def open_subject(self, name):
        self.sm.get_screen("subject").subject_name = name
        self.sm.transition.direction = "left"
        self.sm.current = "subject"

    def open_lesson(self, subject_name, lesson_id):
        screen = self.sm.get_screen("lesson")
        screen.subject_name = subject_name
        screen.lesson_id = lesson_id
        self.sm.transition.direction = "left"
        self.sm.current = "lesson"

    def open_combined(self, subject_name):
        self.sm.get_screen("combined").subject_name = subject_name
        self.sm.transition.direction = "left"
        self.sm.current = "combined"


if __name__ == "__main__":
    SenderoApp().run()
