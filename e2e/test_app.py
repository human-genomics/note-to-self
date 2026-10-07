"""End-to-end: what a person does in the window, checked against the files on disk."""

from __future__ import annotations

import datetime as dt
import os

from playwright.sync_api import expect

from e2e.harness import E2ECase, key_combo, png_bytes
from nts.store import folder_settings


class TestOpening(E2ECase):
    def test_without_the_key_the_notes_stay_locked(self):
        self.page.goto(self.app.base)
        expect(self.page.get_by_text("Open Note to Self with its link")).to_be_visible()
        expect(self.page.locator(".chat-row")).to_have_count(0)

    def test_the_launcher_link_opens_the_app_and_hides_the_key(self):
        self.open()
        self.assertNotIn("key=", self.page.url)
        self.assertNotIn(self.app.key, self.page.evaluate("document.cookie"))  # HttpOnly
        self.page.goto(self.app.base)  # the browser remembers the key
        expect(self.page.locator(".chat-row").first).to_be_visible()
        expect(self.page.locator(".gate")).to_have_count(0)


class TestNotes(E2ECase):
    def test_a_sent_note_is_saved_to_disk(self):
        self.open()
        self.send("hello from the end-to-end test")
        self.wait_for_file(self.app.today(), "hello from the end-to-end test")
        expect(self.page.locator(".chat-row").first).to_contain_text("hello from the end-to-end test")

    def test_shift_enter_starts_a_new_line(self):
        self.open()
        self.composer.click()
        self.page.keyboard.type("line one")
        self.page.keyboard.press("Shift+Enter")
        self.page.keyboard.type("line two")
        self.page.keyboard.press("Enter")
        expect(self.row("line two").locator(".status.read")).to_be_visible()
        self.wait_for_file(self.app.today(), "line one\nline two")

    def test_arrow_up_edits_the_last_note(self):
        self.open()
        self.send("first try")
        self.composer.click()
        self.page.keyboard.press("ArrowUp")
        expect(self.composer).to_have_value("first try")
        self.composer.fill("second try")
        self.composer.press("Enter")
        expect(self.row("second try").locator(".edited")).to_be_visible()
        self.wait_for_file(self.app.today(), "(edited)")
        self.assertNotIn("first try", self.app.read(self.app.today()))
        self.wait_for_file(self.app.path(".trash", "Note to Self", dt.date.today().isoformat() + ".md"),
                           "first try")  # the old version is kept in the trash

    def test_deleting_a_note_moves_it_to_the_trash(self):
        self.open()
        self.send("keep this")
        row = self.send("delete this")
        self.menu(row, "Delete")
        self.dialog_button("Delete")
        expect(self.page.locator(".row .text", has_text="delete this")).to_have_count(0)
        self.wait_until(lambda: "delete this" not in self.app.read(self.app.today()), "the note is gone")
        self.assertIn("keep this", self.app.read(self.app.today()))
        trash = self.app.path(".trash", "Note to Self", dt.date.today().isoformat() + ".md")
        self.assertIn("delete this", self.app.read(trash))

    def test_select_several_notes_and_delete_them(self):
        self.open()
        for text in ("one", "two", "three"):
            self.send("note " + text)
        self.menu(self.row("note one"), "Select")
        self.row("note three").click(modifiers=["Shift"])  # selects the range
        expect(self.page.locator(".selectbar")).to_contain_text("3 selected")
        self.page.get_by_role("button", name="Delete selected messages").click()
        expect(self.page.locator("dialog[open]").last).to_contain_text("Delete selected 3 messages?")
        self.dialog_button("Delete")
        expect(self.page.locator(".row .text", has_text="note ")).to_have_count(0)
        self.wait_until(lambda: not os.path.exists(self.app.today()), "the emptied day file is removed")

    def test_a_draft_is_saved_in_the_notes_folder_and_survives_a_reload(self):
        self.open()
        self.composer.fill("an unsent thought")
        draft = self.app.path("Note to Self", ".draft.md")
        self.wait_for_file(draft, "an unsent thought")
        self.page.reload()
        expect(self.composer).to_have_value("an unsent thought")
        expect(self.page.locator(".chat-row").first).to_contain_text("Draft:")
        self.composer.fill("")
        self.wait_until(lambda: not os.path.exists(draft), "the cleared draft is removed")
        self.assertEqual(self.page.evaluate("Object.keys(localStorage).filter(k => k.includes('draft'))"), [])

    def test_notes_written_in_another_editor_appear(self):
        self.open()
        os.makedirs(os.path.dirname(self.app.today()), exist_ok=True)
        with open(self.app.today(), "a", encoding="utf-8") as f:
            f.write("## %s\n\nwritten in another editor\n" % dt.datetime.now().strftime("%H:%M:%S"))
        expect(self.row("written in another editor")).to_be_visible()

    def test_a_failed_send_can_be_retried(self):
        self.open()
        self.app.stop()
        self.composer.fill("sent while the server was down")
        self.composer.press("Enter")
        failed = self.page.locator(".row.failed").filter(has_text="sent while the server was down")
        expect(failed).to_contain_text("Send failed")
        self.app.start()  # same folder, same port, same key
        failed.locator(".bubble").click(button="right")
        self.page.locator(".menu-item", has_text="Retry Send").click()
        expect(self.row("sent while the server was down").locator(".status.read")).to_be_visible()
        self.wait_for_file(self.app.today(), "sent while the server was down")


class TestAttachments(E2ECase):
    def test_an_attached_image_is_saved_next_to_the_notes(self):
        self.open()
        self.page.locator("input[name=attachments]").set_input_files(
            files=[{"name": "dot.png", "mimeType": "image/png", "buffer": png_bytes()}])
        expect(self.page.locator(".staging .tile")).to_have_count(1)
        self.composer.fill("a tiny picture")
        self.composer.press("Enter")
        row = self.row("a tiny picture")
        expect(row.locator(".status.read")).to_be_visible()
        expect(row.locator("img")).to_be_visible()
        files = os.listdir(self.app.path("Note to Self", "attachments"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("_dot.png"), files)
        self.wait_for_file(self.app.today(), "![dot.png](attachments/%s)" % files[0])


class TestSearch(E2ECase):
    seed = True

    def test_search_finds_a_note_and_jumps_to_it(self):
        self.open()
        self.page.keyboard.press(key_combo("f"))
        expect(self.page.get_by_placeholder("Search")).to_be_focused()
        self.page.keyboard.type("garden")
        result = self.page.locator(".chat-row[data-result]").filter(has_text="garden").first
        expect(result).to_be_visible()
        result.click()
        expect(self.row("Ideas for the garden")).to_be_in_viewport()


class TestChats(E2ECase):
    def test_create_rename_and_delete_a_chat(self):
        self.open()
        self.page.locator(".lp-head").get_by_role("button", name="New chat").click()
        self.page.get_by_placeholder("Chat name").fill("Groceries")
        self.dialog_button("Create")
        expect(self.page.locator(".cv-title")).to_have_text("Groceries")
        self.send("oat milk")
        self.wait_for_file(self.app.today("Groceries"), "oat milk")

        self.page.locator(".cv-head").get_by_role("button", name="More options").click()
        self.page.locator(".menu-item", has_text="Rename chat").click()
        self.page.locator("dialog[open] input").fill("Shopping")
        self.dialog_button("Save")
        expect(self.page.locator(".chat-row[data-name='Shopping']")).to_be_visible()
        self.wait_until(lambda: os.path.isdir(self.app.path("Shopping")), "the folder is renamed")
        self.assertFalse(os.path.exists(self.app.path("Groceries")))

        self.page.locator(".cv-head").get_by_role("button", name="More options").click()
        self.page.locator(".menu-item", has_text="Delete chat").click()
        self.dialog_button("Delete")
        expect(self.page.locator(".chat-row[data-name='Shopping']")).to_have_count(0)
        self.wait_until(lambda: not os.path.exists(self.app.path("Shopping")), "the folder is moved")
        trashed = os.listdir(self.app.path(".trash", "chats"))
        self.assertTrue(any(name.startswith("Shopping ") for name in trashed), trashed)


class TestSettings(E2ECase):
    def test_settings_show_the_folder_and_empty_the_trash(self):
        self.open()
        row = self.send("throw me away")
        self.menu(row, "Delete")
        self.dialog_button("Delete")
        self.wait_until(lambda: os.path.isdir(self.app.path(".trash")), "the trash has something")

        self.page.keyboard.press(key_combo("Comma"))
        settings = self.page.locator("dialog.settings")
        expect(settings).to_be_visible()
        expect(settings.locator(".set-path code")).to_have_text(self.app.root)
        settings.get_by_role("button", name="Empty trash…").click()
        expect(self.page.locator("dialog[open]").last).to_contain_text("Empty the trash?")
        self.dialog_button("Empty trash")
        expect(self.page.locator(".toast")).to_have_text("Trash emptied")
        self.assertFalse(os.path.exists(self.app.path(".trash")))
        expect(settings.locator(".info-rows")).to_contain_text("Empty")

    def test_choose_the_browser_in_settings(self):
        self.open()
        self.page.keyboard.press(key_combo("Comma"))
        settings = self.page.locator("dialog.settings")
        browser = settings.locator("select[name=browser]")
        expect(browser).to_have_value("auto")
        browser.select_option("default")
        expect(self.page.locator(".toast")).to_contain_text("Saved")
        self.wait_until(lambda: folder_settings(self.app.root).get("browser") == "default",
                        "the choice is saved in the notes folder")
        browser.select_option("auto")
        self.wait_until(lambda: "browser" not in folder_settings(self.app.root), "it's automatic again")
        expect(settings.get_by_role("button", name="Open a window there now")).to_be_visible()

    def test_quit_from_the_window_stops_the_app_and_keeps_the_notes(self):
        self.open()
        self.send("still here after quitting")
        self.page.get_by_role("button", name="Menu").click()
        self.page.locator(".menu-item", has_text="Quit Note to Self").click()
        expect(self.page.get_by_text("Note to Self has stopped")).to_be_visible()
        self.assertTrue(self.app.exited(), "notetoself stops when you quit")
        self.assertEqual(self.app.proc.returncode, 0)
        self.assertIn("was quit", self.app.read(self.app.log))
        self.assertIn("still here after quitting", self.app.read(self.app.today()))

    def test_delete_all_data_removes_the_whole_folder(self):
        self.open()
        self.send("a secret")
        self.composer.fill("an unsent secret")
        self.page.get_by_role("button", name="Menu").click()
        self.page.locator(".menu-item", has_text="Settings").click()
        self.page.locator("dialog.settings").get_by_role("button", name="Delete all data…").click()
        confirm = self.page.locator("dialog[open]").last
        expect(confirm).to_contain_text("Delete all data?")
        expect(confirm.get_by_role("button", name="Cancel")).to_be_focused()  # Enter won't delete
        self.dialog_button("Delete everything")
        expect(self.page.get_by_text("All data deleted")).to_be_visible()
        self.assertFalse(os.path.exists(self.app.root))
        self.assertTrue(self.app.exited(), "notetoself stops after deleting everything")
        self.assertEqual(self.app.proc.returncode, 0)
        self.assertIn("All data was deleted", self.app.read(self.app.log))
        self.assertEqual(self.page.evaluate("Object.keys(localStorage).length"), 0)
