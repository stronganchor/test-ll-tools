"""
Language-Learner-Tools flashcard smoke-tests.

Headless (default) ........ pytest -v
Visible browser ............ set LLTOOLS_HEADLESS=0  &&  pytest -v
"""
from contextlib import contextmanager
import os
import time

import pytest
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
    ElementClickInterceptedException,
)
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ───────── CONFIG ──────────────────────────────────────────────────────────

QUIZ_PAGES = [
    "https://starter-english-test.local/home/",           # dev site first
    "https://www.turkishtextbook.com/vocab-lessons/",
    "https://starterenglish.com/",
    "https://wordboat.com/biblical-hebrew/",
]

MAX_CATEGORY_TESTS = 5            # test first 4 categories per site
ROUNDS_PER_RUN = 3
MAX_PAGE_LOAD_SEC = 20
MAX_ROUND_SEC = 25               # per round
ROUND_START_DELAY = 2            # seconds after audio≥0.4 s

# ───────── BROWSER ─────────────────────────────────────────────────────────


@contextmanager
def browser(headless: bool | None = None):
    if headless is None:
        headless = os.getenv("LLTOOLS_HEADLESS", "1") != "0"

    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")

    # Silence Chrome console spam
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-gpu")

    drv = Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        yield drv
    finally:
        drv.quit()

# ───────── HELPER FUNCTIONS ────────────────────────────────────────────────


def fail(url, cat_idx, round_no, msg):
    pytest.fail(f"{url}  [cat #{cat_idx}]  round {round_no}: {msg}", pytrace=False)


def wait_click(driver, by, value, timeout=MAX_PAGE_LOAD_SEC):
    elm = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center',inline:'center'});", elm
    )
    elm.click()
    return elm


def flashcards_visible(driver, min_cards: int = 2) -> bool:
    cards = driver.find_elements(By.CSS_SELECTOR, ".flashcard-container")
    return len([c for c in cards if c.is_displayed()]) >= min_cards


def wait_pointer_events_enabled(driver, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        style = driver.execute_script(
            "return window.getComputedStyle("
            "document.querySelector('#ll-tools-flashcard')).pointerEvents;"
        )
        if style != "none":
            return
        time.sleep(0.1)


def wait_audio_played(driver, min_time=0.4, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        audio_time = driver.execute_script(
            "const a=document.querySelector('#ll-tools-flashcard audio');"
            "return a ? a.currentTime : null;"
        )
        if audio_time is not None and audio_time >= min_time:
            return
        time.sleep(0.1)


def choose_single_category(driver, idx: int):
    checkboxes = driver.find_elements(
        By.CSS_SELECTOR,
        "#ll-tools-category-checkboxes input[type='checkbox']",
    )
    if not checkboxes:
        pytest.skip("Page auto-starts without category picker")

    if idx >= len(checkboxes):
        pytest.skip(f"Site shows only {len(checkboxes)} categories (wanted #{idx})")

    for cb in checkboxes:                  # uncheck all
        if cb.is_selected():
            cb.click()

    checkboxes[idx].click()

    start_btn = driver.find_element(By.ID, "ll-tools-start-selected-quiz")
    if not start_btn.is_enabled():
        pytest.skip("Start button disabled – cannot launch quiz")
    start_btn.click()


def early_click_first_card(driver):
    cards = driver.find_elements(By.CSS_SELECTOR, ".flashcard-container")
    for c in cards:
        try:
            if c.is_displayed():
                c.click()
                return
        except (StaleElementReferenceException, ElementClickInterceptedException):
            continue

# ───────── PARAMETRISED TESTS ──────────────────────────────────────────────

test_matrix = [
    (url, cat_idx)
    for url in QUIZ_PAGES
    for cat_idx in range(MAX_CATEGORY_TESTS)
]


@pytest.mark.parametrize("url, category_index", test_matrix)
def test_flashcard_quiz(url, category_index):
    with browser() as d:
        d.get(url)

        try:
            wait_click(d, By.ID, "ll-tools-start-flashcard")
        except TimeoutException:
            pass  # some embeds open immediately

        choose_single_category(d, category_index)

        for round_no in range(1, ROUNDS_PER_RUN + 1):
            if not WebDriverWait(d, MAX_ROUND_SEC).until(flashcards_visible):
                fail(url, category_index, round_no, "flashcards never appeared")

            early_click_first_card(d)           # simulate race click
            wait_pointer_events_enabled(d)
            wait_audio_played(d, 0.4)
            time.sleep(ROUND_START_DELAY)

            start_ts = time.time()
            while time.time() - start_ts < MAX_ROUND_SEC:
                cards = d.find_elements(By.CSS_SELECTOR, ".flashcard-container")
                nothing_clicked = True

                for card in cards:
                    try:
                        if card.is_displayed():
                            card.click()
                            nothing_clicked = False
                            time.sleep(0.35)
                    except StaleElementReferenceException:
                        continue

                # DOM refreshed → next round
                if nothing_clicked or not flashcards_visible(d):
                    break
            else:
                fail(url, category_index, round_no, "round never completed")
