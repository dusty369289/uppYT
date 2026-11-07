"""What people actually do, in a real browser, with real clicks and keys."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.support import NOW, feed_xml
from tests.ui.conftest import Watcher, open_context
from tests.ui.scenes import CHANNELS
from uppyt.domain.feeds import TOGGLES, FeedKind

pytestmark = [pytest.mark.ui, pytest.mark.timeout(45)]


@pytest.fixture
def board(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser, width=1280)
    page = context.new_page()
    watcher = Watcher(page)
    page.goto(scene.url + "/", wait_until="networkidle")
    yield page, scene, watcher
    assert not watcher.problems, watcher.problems
    context.close()


def cards(page):
    return page.locator(".card")


def opacity(locator) -> str:
    return locator.evaluate("element => getComputedStyle(element).opacity")


def test_marking_seen_removes_the_card_and_offers_undo(board):
    page, scene, _ = board
    total = cards(page).count()
    first = cards(page).first
    video = first.get_attribute("data-video")
    first.get_by_role("button").click()
    expect(cards(page)).to_have_count(total - 1)
    expect(page.locator(f'[data-video="{video}"]')).to_have_count(0)
    expect(page.locator(".toast")).to_contain_text("Marked as seen")
    expect(page.locator(".tools__count")).to_contain_text(str(scene.services().board.view().total))
    assert video in scene.services().store.seen()
    page.locator(".toast button").click()
    expect(page.locator(f'[data-video="{video}"]')).to_have_count(1)
    expect(page.locator(".toast")).to_have_count(0)
    assert video not in scene.services().store.seen()


def test_the_toast_region_stays_put_and_still_fades(board):
    """Screen readers watch a live region for changes. Replacing the region itself may never be announced."""
    page, _, _ = board
    page.mouse.move(0, 0)
    page.evaluate("window.region = document.getElementById('toasts'); window.region.dataset.fadeAfter = '300'")
    cards(page).first.get_by_role("button").click()
    expect(page.locator(".toast")).to_contain_text("Marked as seen")
    assert page.evaluate("window.region === document.getElementById('toasts')")
    assert page.evaluate("window.region.getAttribute('aria-live')") == "polite"
    expect(page.locator(".toast")).to_have_count(0)


def test_opening_a_video_leaves_the_card_alone(board):
    page, scene, _ = board
    card = cards(page).first
    assert card.locator(".card__picture").get_attribute("target") == "_blank"
    video = card.get_attribute("data-video")
    assert f"v={video}" in card.locator(".card__picture").get_attribute("href")
    assert scene.services().store.seen() == {}


def test_keyboard_shortcuts_move_mark_and_undo(board):
    page, scene, _ = board
    total = cards(page).count()
    page.keyboard.press("j")
    expect(cards(page).nth(0)).to_have_attribute("data-current", "true")
    page.keyboard.press("j")
    expect(cards(page).nth(1)).to_have_attribute("data-current", "true")
    page.keyboard.press("k")
    expect(cards(page).nth(0)).to_have_attribute("data-current", "true")
    video = cards(page).nth(0).get_attribute("data-video")
    page.keyboard.press("s")
    expect(cards(page)).to_have_count(total - 1)
    assert video in scene.services().store.seen()
    page.keyboard.press("u")
    expect(cards(page)).to_have_count(total)
    assert video not in scene.services().store.seen()


def test_shortcuts_dont_fire_while_typing(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser)
    page = context.new_page()
    page.goto(scene.url + f"/channels/{scene.channel_id}")
    page.locator("#tags").click()
    page.keyboard.type("s")
    assert scene.services().store.seen() == {}
    context.close()


def test_filters_apply_at_once_and_land_in_the_address(board):
    page, _, _ = board
    before = cards(page).count()
    page.get_by_label("How far back").select_option("7")
    expect(page).to_have_url(re.compile(r"window=7"))
    expect(page.get_by_label("How far back")).to_have_value("7")
    assert cards(page).count() < before
    page.get_by_label("Tag").select_option("food")
    expect(page).to_have_url(re.compile(r"tag=food"))
    expect(page.locator(".card")).not_to_have_count(0)


def test_changing_filters_quickly_loses_nothing(board):
    """CI found this: a change made while the last was loading was dropped, so controls and board disagreed."""
    page, scene, _ = board
    page.get_by_label("How far back").select_option("30")
    page.get_by_label("Tag").select_option("food")
    page.get_by_label("Shorts").uncheck()
    page.get_by_label("How far back").select_option("7")
    expect(page).to_have_url(re.compile(r"window=7.*tag=food"))
    expect(page.get_by_label("Tag")).to_have_value("food")
    expect(page.get_by_label("How far back")).to_have_value("7")
    kinds = [k for k in TOGGLES if k is not FeedKind.SHORTS]
    expected = scene.services().board.view(window_days=7, tag="food", kinds=kinds)
    expect(cards(page)).to_have_count(len(expected.cards))
    expect(page.locator(".tools__count")).to_have_text(f"{expected.total} unseen")


def test_the_filter_form_is_never_replaced(board):
    """Replacing it is what left a moment when a change had nothing listening for it."""
    page, _, _ = board
    page.evaluate("document.querySelector('.tools').dataset.original = 'yes'")
    page.get_by_label("How far back").select_option("7")
    expect(page).to_have_url(re.compile(r"window=7"))
    assert page.evaluate("document.querySelector('.tools').dataset.original") == "yes"
    page.get_by_label("Tag").select_option("food")
    expect(page).to_have_url(re.compile(r"tag=food"))
    assert page.evaluate("document.querySelector('.tools').dataset.original") == "yes"


def test_check_now_updates_the_board_it_reports_on(board):
    """The toast said '1 new' while the cards and 'Last checked' stayed stale until a reload."""
    page, scene, _ = board
    channel = CHANNELS["alpha"][0]
    fresh = feed_xml(channel, "Harbour Science", [("fresh000xxx", "Brand new upload", NOW)])
    scene.transport.serve_feed(channel, FeedKind.VIDEOS, fresh)
    scene.runtime.feeds._cache.clear()
    scene.clock.advance(hours=2)
    page.get_by_role("button", name="Check now").click()
    expect(page.locator(".toast")).to_contain_text("new")
    expect(page.locator('[data-video="fresh000xxx"]')).to_have_count(1)


BUSY_PROBE = """() => {
    window.busy = [];
    document.body.addEventListener("htmx:beforeSend", () => {
        const results = document.getElementById("results");
        const check = [...document.querySelectorAll("button")].find(b => b.textContent === "Check now");
        window.busy.push({ opacity: getComputedStyle(results).opacity, checkDisabled: check.disabled });
    });
}"""


def test_the_board_dims_while_it_loads(board):
    """Nothing changed on screen during a slow check, and a second click asked YouTube for every feed again."""
    page, _, _ = board
    page.evaluate(BUSY_PROBE)
    page.get_by_label("How far back").select_option("7")
    expect(page).to_have_url(re.compile(r"window=7"))
    assert page.evaluate("window.busy")[0]["opacity"] == "0.6"
    expect(page.locator("#results")).to_have_css("opacity", "1")
    refreshes = []
    page.on("request", lambda request: refreshes.append(request) if request.url.endswith("/refresh") else None)
    page.evaluate("window.busy = []")
    page.evaluate("""() => {
        const check = [...document.querySelectorAll("button")].find(b => b.textContent === "Check now");
        check.click();
        check.click();
    }""")
    expect(page.locator(".toast")).to_be_visible()
    first = page.evaluate("window.busy")[0]
    assert first == {"opacity": "0.6", "checkDisabled": True}
    expect(page.locator("#results")).to_have_css("opacity", "1")
    expect(page.get_by_role("button", name="Check now")).to_be_enabled()
    assert len(refreshes) == 1


def test_unticking_every_kind_says_why_nothing_shows(board):
    page, _, _ = board
    for label in ("Videos", "Live", "Shorts", "Members"):
        page.get_by_label(label).uncheck()
    expect(page.get_by_role("heading", name="Nothing unseen here")).to_be_visible()
    page.get_by_label("Videos").check()
    expect(cards(page).first).to_be_visible()


def test_the_words_clear_on_hover_leaving_only_the_picture(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser, reduced_motion="no-preference")
    page = context.new_page()
    page.goto(scene.url + "/", wait_until="networkidle")
    card = cards(page).first
    assert opacity(card.locator(".card__words")) == "1"
    card.locator(".card__picture").hover()
    page.wait_for_timeout(500)
    assert opacity(card.locator(".card__words")) == "0"
    assert opacity(card.locator(".card__seen")) == "0"
    assert opacity(card.locator(".card__tag")) == "0"
    card.locator(".card__seen button").hover()
    page.wait_for_timeout(350)
    assert opacity(card.locator(".card__seen")) == "1"
    page.mouse.move(0, 0)
    page.wait_for_timeout(500)
    assert opacity(card.locator(".card__words")) == "1"
    context.close()


def test_touch_screens_keep_the_words(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser, width=390, touch=True)
    page = context.new_page()
    page.goto(scene.url + "/", wait_until="networkidle")
    assert not page.evaluate("matchMedia('(hover: hover)').matches")
    assert opacity(cards(page).first.locator(".card__words")) == "1"
    context.close()


def test_keyboard_focus_on_a_card_doesnt_hide_its_words(board):
    page, _, _ = board
    page.mouse.move(0, 0)
    cards(page).first.locator(".card__picture").focus()
    assert opacity(cards(page).first.locator(".card__words")) == "1"


def test_tabbing_to_a_card_draws_a_visible_ring(board):
    """The card clipped the ring drawn outside its link, so outlineStyle said solid while nothing showed."""
    page, _, _ = board
    page.mouse.move(0, 0)
    card = cards(page).first
    before = card.screenshot(animations="disabled")
    page.get_by_label("Tag").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "card__picture"
    assert page.evaluate("document.activeElement.closest('.card') === document.querySelector('.card')")
    assert card.screenshot(animations="disabled") != before, "the focused card looks exactly like the unfocused one"


def focused_video(page):
    return page.evaluate("document.activeElement.closest('.card')?.dataset.video")


def test_keyboard_focus_survives_seen_and_undo(board):
    """The control that had focus was swapped away with its card or toast, so the next Tab began at the top."""
    page, _, _ = board
    first, second = (cards(page).nth(i).get_attribute("data-video") for i in (0, 1))
    cards(page).first.locator(".card__seen button").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f'[data-video="{first}"]')).to_have_count(0)
    expect(page.locator(".card__picture").first).to_be_focused()
    assert focused_video(page) == second
    page.locator(".toast button").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f'[data-video="{first}"] .card__picture')).to_be_focused()


def test_keyboard_focus_lands_on_the_first_new_card_after_show_more(browser, fresh_scene):
    scene = fresh_scene("populated")
    scene.services().settings.update({"page_size": 6})
    context = open_context(browser)
    page = context.new_page()
    page.goto(scene.url + "/", wait_until="networkidle")
    page.locator("#more-link").focus()
    page.keyboard.press("Enter")
    expect(cards(page)).to_have_count(12)
    expect(cards(page).nth(6).locator(".card__picture")).to_be_focused()
    context.close()


def test_keyboard_focus_comes_back_to_check_now(board):
    page, _, _ = board
    page.get_by_role("button", name="Check now").focus()
    page.keyboard.press("Enter")
    expect(page.locator(".toast")).to_be_visible()
    expect(page.get_by_role("button", name="Check now")).to_be_focused()


def test_tab_reaches_everything_in_a_sensible_order(board):
    page, _, _ = board
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "skip-link"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "topbar__logo"
    for _ in range(12):
        page.keyboard.press("Tab")
    assert page.evaluate("!!document.activeElement.closest('.card, .tools')")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"


def test_show_more_never_repeats_a_card_after_marking_seen(browser, fresh_scene):
    scene = fresh_scene("populated")
    scene.services().settings.update({"page_size": 6})
    context = open_context(browser)
    page = context.new_page()
    watcher = Watcher(page)
    page.goto(scene.url + "/", wait_until="networkidle")
    assert cards(page).count() == 6
    cards(page).first.get_by_role("button").click()
    expect(cards(page)).to_have_count(5)
    page.locator("#more-link").click()
    expect(cards(page)).to_have_count(11)
    expect(page).to_have_url(re.compile(r"show=11"))
    ids = [c.get_attribute("data-video") for c in cards(page).all()]
    assert len(ids) == len(set(ids))
    expected = [c.video.id for c in scene.services().board.view(limit=11).cards]
    assert ids == expected
    assert not watcher.problems
    context.close()


def test_first_run_to_a_full_board(browser, fresh_scene):
    scene = fresh_scene("blank")
    context = open_context(browser)
    page = context.new_page()
    watcher = Watcher(page)
    page.goto(scene.url + "/")
    expect(page.get_by_role("heading", name="Welcome to uppYT")).to_be_visible()
    page.get_by_label("Your name").fill("dusty")
    page.get_by_role("button", name="Create profile").click()
    expect(page.get_by_role("heading", name="Add your first channel")).to_be_visible()
    page.get_by_label("Channel link").fill("https://www.youtube.com/@alpha")
    page.get_by_role("button", name="Find channel").click()
    expect(page.get_by_role("heading", name="Harbour Science")).to_be_visible()
    page.get_by_label("Priority").select_option("high")
    page.get_by_label("Tags").fill("science, tides")
    page.get_by_role("button", name="Follow Harbour Science").click()
    expect(page.get_by_role("heading", name="Harbour Science")).to_be_visible()
    page.get_by_role("link", name="Board").click()
    expect(cards(page).first).to_be_visible()
    expect(cards(page).first.locator(".card__tag")).to_have_text("High")
    assert not watcher.problems
    context.close()


def test_a_bad_link_keeps_what_was_typed(browser, fresh_scene):
    scene = fresh_scene("blank_profile")
    context = open_context(browser)
    page = context.new_page()
    page.goto(scene.url + "/channels/add")
    page.get_by_label("Channel link").fill("https://www.youtube.com/watch?v=abc")
    page.get_by_role("button", name="Find channel").click()
    expect(page.get_by_role("alert")).to_contain_text("link to a video")
    expect(page.get_by_label("Channel link")).to_have_value("https://www.youtube.com/watch?v=abc")
    context.close()


def test_changing_a_channel_then_stopping_following(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser)
    page = context.new_page()
    page.goto(scene.url + f"/channels/{scene.channel_id}")
    page.get_by_label("Priority").select_option("low")
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_label("Priority")).to_have_value("low")
    expect(page.locator("main")).to_contain_text("counts as 0 days newer")
    page.get_by_role("link", name="Stop following").click()
    expect(page.get_by_role("heading", name=re.compile("Stop following"))).to_be_visible()
    page.get_by_role("button", name=re.compile("Stop following")).click()
    expect(page.get_by_role("heading", name="Channels")).to_be_visible()
    assert scene.channel_id not in scene.services().store.subscriptions()
    context.close()


def test_appearance_setting_overrides_the_device(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser, mode="dark")
    page = context.new_page()
    page.goto(scene.url + "/settings")
    dark = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.get_by_label("Appearance").select_option("light")
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_text("Settings saved")).to_be_visible()
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != dark
    context.close()


def test_checking_from_settings_says_what_it_found(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser)
    page = context.new_page()
    page.goto(scene.url + "/settings")
    page.get_by_role("button", name="Check for new videos").click()
    expect(page.locator(".toast")).to_contain_text(re.compile("Checked|checked"))
    expect(page).to_have_url(scene.url + "/settings")
    context.close()


def test_import_and_export_in_the_browser(browser, fresh_scene, tmp_path):
    scene = fresh_scene("populated")
    context = open_context(browser, accept_downloads=True)
    page = context.new_page()
    page.goto(scene.url + "/settings")
    with page.expect_download() as download:
        page.get_by_role("link", name="Export your channels").click()
    saved = tmp_path / "channels.opml"
    download.value.save_as(saved)
    assert "Harbour Science" in saved.read_text()
    scene.runtime.create_profile("nat")
    page.goto(scene.url + "/profiles")
    page.get_by_role("button", name="Switch to nat").click()
    page.goto(scene.url + "/settings")
    page.get_by_label("OPML file").set_input_files(saved)
    page.get_by_role("button", name="Import channels").click()
    expect(page.locator("main")).to_contain_text("Added 4 channels", timeout=8000)
    context.close()


def test_everything_important_works_without_javascript(browser, fresh_scene):
    scene = fresh_scene("populated")
    context = open_context(browser, java_script_enabled=False)
    page = context.new_page()
    page.goto(scene.url + "/")
    apply = page.get_by_role("button", name="Apply")
    expect(apply).to_be_visible()
    page.get_by_label("How far back").select_option("7")
    apply.click()
    expect(page).to_have_url(re.compile(r"window=7"))
    total = cards(page).count()
    cards(page).first.get_by_role("button").click()
    expect(cards(page)).to_have_count(total - 1)
    page.goto(scene.url + "/seen")
    page.get_by_role("button", name=re.compile("Put back")).first.click()
    assert scene.services().store.seen() == {}
    context.close()


def test_the_policy_blocks_injected_script(board):
    page, _, watcher = board
    blocked = page.evaluate(
        """() => new Promise(resolve => {
            document.addEventListener('securitypolicyviolation', () => resolve(true), { once: true });
            const script = document.createElement('script');
            script.textContent = 'window.pwned = true';
            document.head.append(script);
            setTimeout(() => resolve(false), 200);
        })"""
    )
    assert blocked
    assert page.evaluate("window.pwned") is None
    watcher.problems.clear()  # the browser reports the block, which is what was being tested
