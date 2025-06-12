#!/usr/bin/env python
"""
flashcard_smoketest.py
──────────────────────
Quick functional smoke-test for Language-Learner-Tools flash-card quizzes.

• Runs each target page up to N categories (first categories shown)
• Plays a few rounds, simulating one “too-early” click to expose race-conditions
• Prints coloured PASS / FAIL / SKIP lines plus a summary table
• 0 exit-code  → all passed / skipped
  non-zero     → at least one hard failure

Usage
─────
$ python flashcard_smoketest.py            # uses headless browser
$ LLTOOLS_HEADLESS=0 python flashcard_smoketest.py  # shows Chrome window
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse

# ─── third-party ────────────────────────────────────────────────────────────
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

try:
    from colorama import Fore, Style, init as colorama_init

    colorama_init()
    C_PASS, C_FAIL, C_SKIP, C_RESET = (
        Fore.GREEN,
        Fore.RED,
        Fore.YELLOW,
        Style.RESET_ALL,
    )
except ModuleNotFoundError:  # colour is optional
    C_PASS = C_FAIL = C_SKIP = C_RESET = ""


# ─── CONFIG ─────────────────────────────────────────────────────────────────

QUIZ_PAGES = [
    "https://starter-english-test.local/home/",          # local dev first
    "https://www.turkishtextbook.com/vocab-lessons/",
    "https://starterenglish.com/",
    "https://wordboat.com/biblical-hebrew/",
]

MAX_CATEGORY_TESTS = 4  # first N categories per site
ROUNDS_PER_RUN = 3
MAX_PAGE_LOAD_SEC = 20
MAX_ROUND_SEC = 25
ROUND_START_DELAY = 0.2         # seconds after audio ≥0.4 s

# ─── INTERNAL TYPES ────────────────────────────────────────────────────────


class SkipTest(Exception):
    """Raised when a particular (url, category) cannot be run but is not a fail."""


@dataclass
class Result:
    url: str
    category: int
    verdict: str   # "PASS" | "FAIL" | "SKIP"
    detail: str
    elapsed: float


# ─── BROWSER HANDLING ──────────────────────────────────────────────────────


@contextmanager
def browser(headless: bool | None = None):
    if headless is None:
        headless = os.getenv("LLTOOLS_HEADLESS", "1") != "0"

    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    drv = Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        yield drv
    finally:
        drv.quit()


# ─── UTILS ─────────────────────────────────────────────────────────────────


def wait_click(drv, by, value, timeout=MAX_PAGE_LOAD_SEC):
    elm = WebDriverWait(drv, timeout).until(
        EC.element_to_be_clickable((by, value))
    )
    drv.execute_script(
        "arguments[0].scrollIntoView({block:'center',inline:'center'});", elm
    )
    elm.click()
    return elm


def flashcards_visible(drv, min_cards=2) -> bool:
    cards = drv.find_elements(By.CSS_SELECTOR, ".flashcard-container")
    return len([c for c in cards if c.is_displayed()]) >= min_cards


def wait_pointer_enabled(drv, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        style = drv.execute_script(
            "var c=document.querySelector('#ll-tools-flashcard');"
            "return c?getComputedStyle(c).pointerEvents:null;"
        )
        if style and style != "none":
            return
        time.sleep(0.1)
    raise TimeoutException("pointer-events never enabled")


def wait_audio_played(drv, min_time=0.4, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        ct = drv.execute_script(
            "var a=document.querySelector('#ll-tools-flashcard audio');"
            "return a?a.currentTime:null;"
        )
        if ct and ct >= min_time:
            return
        time.sleep(0.1)
    raise TimeoutException("audio never reached {:.1f}s".format(min_time))


def choose_single_category(drv, idx: int):
    checkboxes = drv.find_elements(
        By.CSS_SELECTOR,
        "#ll-tools-category-checkboxes input[type='checkbox']",
    )
    if not checkboxes:
        raise SkipTest("page auto-starts without category picker")

    if idx >= len(checkboxes):
        raise SkipTest(f"only {len(checkboxes)} categories on page")

    for cb in checkboxes:
        if cb.is_selected():
            cb.click()
    checkboxes[idx].click()

    start_btn = drv.find_element(By.ID, "ll-tools-start-selected-quiz")
    if not start_btn.is_enabled():
        raise SkipTest("Start button disabled after category change")
    start_btn.click()


def early_click_first_card(drv):
    for c in drv.find_elements(By.CSS_SELECTOR, ".flashcard-container"):
        try:
            if c.is_displayed():
                c.click()
                return
        except (StaleElementReferenceException, ElementClickInterceptedException):
            continue


# ─── CORE SINGLE-RUN FUNCTION ─────────────────────────────────────────────


def run_quiz(url: str, cat_idx: int) -> None:
    """
    Raises
    ------
    SkipTest
        the combination cannot be executed (not an error)
    Exception
        any hard failure
    """
    with browser() as d:
        d.get(url)

        try:
            wait_click(d, By.ID, "ll-tools-start-flashcard")
        except TimeoutException:
            pass  # some embeds show popup instantly

        choose_single_category(d, cat_idx)

        for round_no in range(1, ROUNDS_PER_RUN + 1):
            if not WebDriverWait(d, MAX_ROUND_SEC).until(flashcards_visible):
                raise RuntimeError(f"round {round_no}: flashcards never appeared")

            # provoke race condition
            early_click_first_card(d)

            wait_pointer_enabled(d)
            wait_audio_played(d)
            time.sleep(ROUND_START_DELAY)

            start_ts = time.time()
            while time.time() - start_ts < MAX_ROUND_SEC:
                cards = d.find_elements(By.CSS_SELECTOR, ".flashcard-container")
                progress = False
                for c in cards:
                    try:
                        if c.is_displayed():
                            c.click()
                            progress = True
                            time.sleep(0.35)
                    except StaleElementReferenceException:
                        continue

                if not progress or not flashcards_visible(d):
                    break
            else:
                raise RuntimeError(f"round {round_no}: never completed")


# ─── RUNNER ────────────────────────────────────────────────────────────────


def param_matrix() -> List[Tuple[str, int]]:
    return [
        (u, ci)
        for u in QUIZ_PAGES
        for ci in range(MAX_CATEGORY_TESTS)
    ]


def host_short(url: str) -> str:
    h = urlparse(url).hostname or "site"
    h = h.replace("www.", "").split(".")[0]
    return h


def main() -> None:
    results: List[Result] = []
    total_start = time.time()

    for url, cat_idx in param_matrix():
        label = f"{host_short(url)}-cat{cat_idx}"
        sys.stdout.write(f"{label:<25} … "); sys.stdout.flush()
        start = time.time()
        try:
            run_quiz(url, cat_idx)
        except SkipTest as ex:
            verdict, colour = "SKIP", C_SKIP
            detail = str(ex)
        except Exception as ex:  # hard fail
            verdict, colour = "FAIL", C_FAIL
            detail = str(ex)
        else:
            verdict, colour = "PASS", C_PASS
            detail = ""
        elapsed = time.time() - start
        print(f"{colour}{verdict}{C_RESET}  ({elapsed:5.1f}s) {detail}")
        results.append(Result(url, cat_idx, verdict, detail, elapsed))

    # ─ summary ─
    print("\n──── Summary ─────────────────────────────────────────────")
    total_time = time.time() - total_start
    for r in results:
        if r.verdict != "PASS":
            print(f"{r.verdict:<4}  {host_short(r.url)} cat#{r.category}: {r.detail}")

    passes = sum(r.verdict == "PASS" for r in results)
    fails = sum(r.verdict == "FAIL" for r in results)
    skips = sum(r.verdict == "SKIP" for r in results)
    print(
        f"\n{C_PASS}{passes} passed{C_RESET}, "
        f"{C_FAIL}{fails} failed{C_RESET}, "
        f"{C_SKIP}{skips} skipped{C_RESET}  •  total {total_time:0.1f}s"
    )
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
